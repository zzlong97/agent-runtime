"""独立只读元数据核对和隔离schema内的慢序列化边界探针。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import PostgresDsn
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

import agent_runtime
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository, bind_execution_token

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
DATABASE = "mi_dev01_20261030_6d2f8b90"
SCHEMA = "mi01_review_6d2f8b90"
OPERATION = "mi-dev01-review-20261030-6d2f8b90"


async def main():
    assert Path(agent_runtime.__file__).resolve() == ROOT / "src/agent_runtime/__init__.py"
    source = Settings(_env_file=os.environ["MI01_TEST_SOURCE_ENV"])
    parts = urlsplit(str(source.database_url))
    dedicated = Settings(_env_file=None, database_url=PostgresDsn(urlunsplit((
        parts.scheme, parts.netloc, "/" + DATABASE, parts.query, "",
    ))))
    report = {
        "operation_key": OPERATION, "database": DATABASE, "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "module_file": str(Path(agent_runtime.__file__).resolve()),
        "files": {str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in [ROOT/"src/agent_runtime/runtime/ownership.py", ROOT/"src/agent_runtime/persistence/guarded_checkpointer.py"]},
    }
    expected = json.loads((EVIDENCE / "真实数据库观察.json").read_text(encoding="utf-8"))
    conn = await psycopg.AsyncConnection.connect(dedicated.database_connection_string, autocommit=True, row_factory=dict_row)
    try:
        assert (await (await conn.execute("SELECT current_database() AS name")).fetchone())["name"] == DATABASE
        await conn.execute("SET default_transaction_read_only=on")
        checked = 0
        for case in expected["cases"]:
            rows = await (await conn.execute("""SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
                col_description(c.oid,a.attnum) AS comment FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0
                AND NOT a.attisdropped AND ((c.relname='runs' AND a.attname IN ('owner_instance_id','execution_epoch','lease_expires_at'))
                OR c.relname='runtime_cluster_config' OR (c.relname='sessions' AND a.attname='deletion_requested_at'))
                ORDER BY c.relname,a.attnum""", (case["schema"],))).fetchall()
            assert rows == case["new_column_metadata"], case["test"]
            checked += 1
        report["existing_schema_metadata_matches"] = checked
        await conn.execute("SET default_transaction_read_only=off")
        exists = await (await conn.execute("SELECT obj_description(oid) AS comment FROM pg_namespace WHERE nspname=%s", (SCHEMA,))).fetchone()
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
    spec = importlib.util.spec_from_file_location("mi01_audited_test_helpers", ROOT / "tests/runtime/test_ownership_postgres.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    await helpers.prepared(settings)
    _, run_id = await helpers.seed(settings)
    captured = []

    class ObservedOwnership(PostgresRunOwnershipRepository):
        @asynccontextmanager
        async def guard(self, token):
            async with super().guard(token) as guarded:
                captured.append(guarded)
                yield guarded

    repo = ObservedOwnership(settings.model_copy(update={"run_transaction_timeout_seconds": .3}), contract_hash="a"*64)
    from uuid import uuid4
    token = await repo.claim(run_id, uuid4())
    assert token is not None
    entered = threading.Event()

    class SlowSerde(JsonPlusSerializer):
        def dumps_typed(self, value):
            entered.set()
            time.sleep(.45)
            return super().dumps_typed(value)

    reading = await psycopg.AsyncConnection.connect(settings.database_connection_string, autocommit=True)
    saver = GuardedAsyncPostgresSaver(reading, ownership=repo, serde=SlowSerde())
    await saver.setup()
    config = {"configurable": {"thread_id": "review:synthetic", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"business": {"synthetic": 1}}
    checkpoint["channel_versions"] = {"business": "1"}
    async def write():
        with bind_execution_token(token):
            await saver.aput(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {"business": "1"})
    started = time.perf_counter()
    writing = asyncio.create_task(write())
    try:
        assert await asyncio.to_thread(entered.wait, 2), "慢序列化器未执行"
        locked = False
        async with helpers.connection(settings) as observer:
            try:
                await observer.execute("SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT", (run_id,))
            except psycopg.errors.LockNotAvailable:
                locked = True
        report["serialization_observation"] = {"run_lock_held_during_serialization": locked, "artificial_serialization_delay_ms": 450, "transaction_budget_ms": 300}
        assert locked
        try:
            await writing
        except psycopg.Error as exc:
            report["serialization_observation"].update(error_type=type(exc).__name__, elapsed_ms=round((time.perf_counter()-started)*1000), failed_connection_closed=captured[0].closed)
        else:
            raise AssertionError("慢序列化未触发受保护事务超时")
        assert captured[0].closed
        assert await helpers.counts(settings) == {"checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0}
        report["serialization_observation"]["rollback_three_tables_empty"] = True
        normal = GuardedAsyncPostgresSaver(reading, ownership=repo)
        with bind_execution_token(token):
            await normal.aput(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {"business": "1"})
        assert captured[1] is not captured[0]
        assert await normal.aget_tuple(config) is not None
        report["serialization_observation"]["fresh_connection_normal_write_succeeded"] = True
    finally:
        await asyncio.gather(writing, return_exceptions=True)
        await reading.close()
    report["probe_exit"] = 0
    (EVIDENCE/"独立审核探针结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"database": DATABASE, "schema": SCHEMA, "metadata_cases": report["existing_schema_metadata_matches"], **report["serialization_observation"]}, ensure_ascii=False))


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(main())
