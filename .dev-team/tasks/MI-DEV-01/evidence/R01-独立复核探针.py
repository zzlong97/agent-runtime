"""MI01-R01独立复核：原慢序列化边界、锁定框架差分和准备异常。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import PostgresDsn
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot

import agent_runtime
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository, bind_execution_token

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
DATABASE = "mi_dev01_20261030_6d2f8b90"
SCHEMA = "mi01_review_r01_6d2f8b90"
OPERATION = "mi-dev01-review-r01-20261030-6d2f8b90"


async def main():
    assert Path(agent_runtime.__file__).resolve() == ROOT / "src/agent_runtime/__init__.py"
    source = Settings(_env_file=os.environ["MI01_TEST_SOURCE_ENV"])
    parts = urlsplit(str(source.database_url))
    dedicated = Settings(_env_file=None, database_url=PostgresDsn(urlunsplit((
        parts.scheme, parts.netloc, "/" + DATABASE, parts.query, "",
    ))))
    report = {
        "issue": "MI01-R01", "operation_key": OPERATION, "database": DATABASE, "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "module_file": str(Path(agent_runtime.__file__).resolve()),
        "packages": {name: importlib.metadata.version(name) for name in ("psycopg", "langgraph-checkpoint-postgres")},
        "files": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            "src/agent_runtime/persistence/guarded_checkpointer.py", "tests/runtime/test_ownership_postgres.py")},
    }
    conn = await psycopg.AsyncConnection.connect(dedicated.database_connection_string, autocommit=True, row_factory=dict_row)
    try:
        assert (await (await conn.execute("SELECT current_database() AS name")).fetchone())["name"] == DATABASE
        exists = await (await conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMA,))).fetchone()
        assert exists is None, "审核schema已存在，停止写入并先核对"
        async with conn.transaction():
            await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
            await conn.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(SCHEMA), sql.Literal(OPERATION)))
    finally:
        await conn.close()
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-c search_path={SCHEMA},public"
    settings = Settings(_env_file=None, database_url=PostgresDsn(urlunsplit((
        parts.scheme, parts.netloc, "/" + DATABASE, urlencode(query, quote_via=quote), "",
    ))))
    spec = importlib.util.spec_from_file_location("mi01_r01_audited_helpers", ROOT/"tests/runtime/test_ownership_postgres.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    await helpers.prepared(settings)
    _, run_id = await helpers.seed(settings)
    guard_entries = []
    class ObservedOwnership(PostgresRunOwnershipRepository):
        @asynccontextmanager
        async def guard(self, token):
            guard_entries.append(True)
            async with super().guard(token) as guarded:
                yield guarded
    repo = ObservedOwnership(settings.model_copy(update={"run_transaction_timeout_seconds": .3}), contract_hash="a"*64)
    token = await repo.claim(run_id, uuid4())
    assert token is not None
    entered = threading.Event()
    class SlowSerde(JsonPlusSerializer):
        def dumps_typed(self, value):
            entered.set()
            time.sleep(.45)
            return super().dumps_typed(value)
    reading = await psycopg.AsyncConnection.connect(settings.database_connection_string, autocommit=True)
    try:
        saver = GuardedAsyncPostgresSaver(reading, ownership=repo, serde=SlowSerde())
        await saver.setup()
        config = {"configurable": {"thread_id": "review-r01:slow", "checkpoint_ns": ""}}
        cp = helpers.checkpoint()
        async def write():
            with bind_execution_token(token):
                return await saver.aput(config, cp, {"source": "input", "step": 0, "parents": {}}, {"business": "1"})
        started = time.perf_counter()
        writing = asyncio.create_task(write())
        assert await asyncio.to_thread(entered.wait, 2)
        assert guard_entries == []
        async with helpers.connection(settings) as observer:
            async with observer.transaction():
                await observer.execute("SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT", (run_id,))
        result = await writing
        assert await saver.aget_tuple(result) is not None
        report["original_slow_blob_recheck"] = {
            "serialization_delay_ms": 450, "transaction_budget_ms": 300,
            "guard_not_entered_during_serialization": True, "independent_nowait_run_lock_free": True,
            "write_succeeded": True, "elapsed_ms": round((time.perf_counter()-started)*1000),
        }

        plain = AsyncPostgresSaver(reading)
        guarded = GuardedAsyncPostgresSaver(reading, ownership=repo)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"primitive": "合成文本", "number": 1.25, "null": None,
            "blob": {"nested": [1, "二"]}, "delta": _DeltaSnapshot(["合成快照"])}
        checkpoint["channel_versions"] = {key: "1" for key in checkpoint["channel_values"]}
        untouched = deepcopy(checkpoint)
        metadata = {"source": "input", "step": 0, "parents": {}, "writes": {"synthetic": 1}}
        plain_config = {"configurable": {"thread_id": "review-r01:plain", "checkpoint_ns": "child", "review_tag": "合成标签"}}
        guarded_config = {"configurable": {**plain_config["configurable"], "thread_id": "review-r01:guarded"}}
        p1 = await plain.aput(plain_config, checkpoint, metadata, checkpoint["channel_versions"])
        with bind_execution_token(token):
            g1 = await guarded.aput(guarded_config, checkpoint, metadata, checkpoint["channel_versions"])
        following = deepcopy(checkpoint)
        following["id"] = str(uuid4())
        next_metadata = {"source": "loop", "step": 1, "parents": {}}
        p2 = await plain.aput(p1, following, next_metadata, {})
        with bind_execution_token(token):
            g2 = await guarded.aput(g1, following, next_metadata, {})
        p_saved, g_saved = await plain.aget_tuple(p2), await guarded.aget_tuple(g2)
        assert p_saved.checkpoint == g_saved.checkpoint
        assert p_saved.metadata == g_saved.metadata
        assert p_saved.parent_config == p1 and g_saved.parent_config == g1
        assert checkpoint == untouched
        rows = await helpers.fetch(settings, "SELECT checkpoint,metadata FROM checkpoints WHERE thread_id IN (%s,%s) ORDER BY checkpoint_id,thread_id", ("review-r01:plain", "review-r01:guarded"))
        assert len(rows) == 4 and rows[0] == rows[1] and rows[2] == rows[3]
        assert await guarded.aget_tuple({"configurable": {"thread_id": "review-r01:guarded", "checkpoint_ns": "parent"}}) is None
        report["locked_framework_differential"] = {
            "primitive_blob_delta_snapshot_roundtrip_identical": True, "raw_checkpoint_metadata_json_identical": True,
            "parent_links_preserved": True, "input_unchanged": True, "namespace_isolation": True,
        }

        before = await helpers.counts(settings)
        entries_before = len(guard_entries)
        invalid = deepcopy(checkpoint)
        invalid["id"] = str(uuid4())
        invalid["versions_seen"] = {"synthetic": object()}
        try:
            with bind_execution_token(token):
                await guarded.aput(guarded_config, invalid, next_metadata, {})
        except TypeError:
            pass
        else:
            raise AssertionError("不可JSON编码的准备参数应失败")
        assert len(guard_entries) == entries_before
        assert await helpers.counts(settings) == before
        async with helpers.connection(settings) as observer:
            async with observer.transaction():
                await observer.execute("SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT", (run_id,))
        report["preparation_exception"] = {"guard_not_entered": True, "three_table_counts_unchanged": True, "run_lock_free": True}
    finally:
        await reading.close()
    report["probe_exit"] = 0
    (EVIDENCE/"R01-独立复核探针结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"database": DATABASE, "schema": SCHEMA, "slow_blob": report["original_slow_blob_recheck"],
        "framework_differential": report["locked_framework_differential"], "preparation_exception": report["preparation_exception"]}, ensure_ascii=False))


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(main())
