"""DB-001 专用隔离数据库的真实注释、不变性和回滚验证。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo

from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.persistence.database import open_database_connection
from tests.persistence.table_comments_support import (
    EXPECTED_COMMENTS, assert_comments, assert_unchanged, setup_all, snapshot,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="显式启用 PostgreSQL 测试后执行"),
]


async def seed_existing(settings):
    """仅在隔离库准备九表非空 fixture 与已有注释。"""

    async with open_database_connection(settings) as connection:
        await connection.execute("""
            INSERT INTO sessions VALUES
            ('00000000-0000-0000-0000-000000000001', 'db001', '隔离测试', now(), now());
            INSERT INTO message_feedback VALUES
            ('db001', '00000000-0000-0000-0000-000000000001',
             '00000000-0000-0000-0000-000000000002', 'like', now());
            INSERT INTO runs (run_id, request_id, session_id, thread_id, run_type,
                input_message_id, response_message_id, request_fingerprint,
                status, created_at, updated_at)
            VALUES ('00000000-0000-0000-0000-000000000003',
                '00000000-0000-0000-0000-000000000004',
                '00000000-0000-0000-0000-000000000001', 'db001', 'normal',
                '00000000-0000-0000-0000-000000000005',
                '00000000-0000-0000-0000-000000000002', 'db001', 'interrupted', now(), now());
            INSERT INTO runtime_events VALUES
                ('00000000-0000-0000-0000-000000000006',
                 '00000000-0000-0000-0000-000000000003', 1, 'run.started',
                 'runtime', 'public', '{}', 1, 'durable', now());
            INSERT INTO run_interrupts (run_id, interrupt_id, status, interrupt_payload, created_at)
            VALUES ('00000000-0000-0000-0000-000000000003',
                '00000000-0000-0000-0000-000000000007', 'pending', '{}', now());
            INSERT INTO capability_tasks (task_id, session_id, capability_id,
                state_schema_version, status, last_run_id, last_invocation_id, created_at, updated_at)
            VALUES ('00000000-0000-0000-0000-000000000008',
                '00000000-0000-0000-0000-000000000001', 'general_chat', '1', 'active',
                '00000000-0000-0000-0000-000000000003',
                '00000000-0000-0000-0000-000000000009', now(), now());
            INSERT INTO capability_task_contexts VALUES
                ('00000000-0000-0000-0000-000000000001', 'general_chat',
                 '00000000-0000-0000-0000-000000000008', now());
            INSERT INTO capability_operations VALUES
                ('00000000-0000-0000-0000-000000000010',
                 '00000000-0000-0000-0000-000000000003',
                 '00000000-0000-0000-0000-000000000009',
                 '00000000-0000-0000-0000-000000000008', 'general_chat',
                 'db001-test', repeat('a', 64), 'pending', now(), now());
            INSERT INTO user_capability_permissions VALUES
                ('db001', 'general_chat', true, now(), now());
            COMMENT ON COLUMN sessions.title IS '必须保留的隔离测试列注释';
            COMMENT ON INDEX sessions_user_updated_id_idx IS '必须保留的隔离测试索引注释';
            COMMENT ON CONSTRAINT sessions_pkey ON sessions IS '必须保留的隔离测试约束注释';
        """)
        old = {
            "sessions": "保存会话本身的元数据，是整个会话数据的业务根节点",
            "message_feedback": "保存用户对 AI 回答的点赞/点踩结果",
            "runs": "某一次 Agent 执行到底发生了什么\ncan、celled",
            "runtime_events": "Run 是怎样一步一步走到这个状态的。",
            "run_interrupts": "Agent 主动暂停执行，等待人类提供额外信息后继续同一个 Run。",
        }
        for table in EXPECTED_COMMENTS:
            await connection.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(
                sql.Identifier(table), sql.Literal(old.get(table)),
            ))
        await connection.commit()


async def validate_isolated(settings, monkeypatch):
    """执行空库、既有库、故障恢复和重跑验证，返回可保存的摘要。"""

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from agent_runtime.sessions import repository as session_module

    await setup_all(settings)
    fresh = await snapshot(settings)
    assert_comments(fresh)
    async with open_database_connection(settings) as connection:
        await connection.set_autocommit(True)
        await AsyncPostgresSaver(connection).setup()
    await seed_existing(settings)
    before = await snapshot(settings)
    assert all(row["rows"] == 1 for row in before["data"].values())

    @asynccontextmanager
    async def fail_after_comment(runtime_settings):
        async with open_database_connection(runtime_settings) as connection:
            class FaultConnection:
                async def execute(self, statement):
                    result = await connection.execute(statement)
                    if statement.strip().startswith("COMMENT ON TABLE"):
                        await connection.execute("SELECT 1 / 0")
                    return result

                async def commit(self):
                    await connection.commit()
            yield FaultConnection()

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(session_module, "open_database_connection", fail_after_comment)
        from psycopg.errors import DivisionByZero
        with pytest.raises(DivisionByZero):
            await session_module.PostgresSessionRepository(settings).setup()
    rolled_back = await snapshot(settings)
    assert before == rolled_back
    await setup_all(settings)
    after = await snapshot(settings)
    assert_comments(after)
    assert_unchanged(before, after)
    await setup_all(settings)
    repeated = await snapshot(settings)
    assert_comments(repeated)
    assert after == repeated
    assert {row["table_name"] for row in after["comments"]} == set(EXPECTED_COMMENTS) | {
        "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
    }
    return {"fresh": fresh, "before": before, "rolled_back": rolled_back,
            "after": after, "repeated": repeated}


def test_new_existing_repeated_and_rollback(monkeypatch):
    database = os.getenv("DB001_TEST_DATABASE", f"db001_comments_{uuid4().hex}")
    assert database.startswith("db001_comments_") and database[15:].isalnum()
    base = Settings()
    admin_dsn = make_conninfo(base.database_connection_string, dbname="postgres")
    from pydantic import PostgresDsn
    target = urlsplit(str(base.database_url))._replace(path="/" + database)
    settings = base.model_copy(update={"database_url": PostgresDsn(urlunsplit(target))})
    created = False
    try:
        with Connection.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            created = True
        with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
            results = runner.run(validate_isolated(settings, monkeypatch))
        print("DB001_ISOLATED_PASSED", database,
              {"tables": len(EXPECTED_COMMENTS), "rollback": True, "data_rows_each": 1,
               "excluded_comments": len(results["after"]["excluded_comments"])})
        report_path = os.getenv("DB001_SNAPSHOT_PATH")
        if report_path:
            Path(report_path).write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
    finally:
        if created:
            with Connection.connect(admin_dsn, autocommit=True) as admin:
                assert database.startswith("db001_comments_")
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
