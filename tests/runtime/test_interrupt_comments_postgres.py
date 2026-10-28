"""本任务涉及的Interrupt表在真实隔离schema初始化/存量setup时维护中文列注释。"""

import os
import asyncio
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from psycopg import sql

@pytest.mark.postgres
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="显式启用隔离PostgreSQL")
def test_interrupt_column_comments_exist_after_fresh_and_existing_setup(monkeypatch):
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.interrupts import PostgresRunInterruptRepository
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.repository import PostgresSessionRepository
    original = Settings()
    base = Settings(database_url=str(original.database_url).rsplit("/", 1)[0] + "/agent_runtime_ra03_probe", _env_file=None)
    schema = "ra03_comments_" + uuid4().hex
    settings = Settings(database_url=str(base.database_url) + "?" + urlencode({"options": f"-csearch_path={schema}"}), _env_file=None)

    async def exercise():
        async with open_database_connection(base) as connection:
            target = await (await connection.execute("SELECT current_database() AS database, current_schema() AS schema")).fetchone()
            assert target == {"database": "agent_runtime_ra03_probe", "schema": "public"}
            await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            await connection.commit()
        try:
            await PostgresSessionRepository(settings).setup()
            await PostgresRunRepository(settings).setup()
            for phase in ("首次初始化", "存量setup"):
                if phase == "存量setup":
                    async with open_database_connection(settings) as connection:
                        await connection.execute("COMMENT ON COLUMN run_interrupts.interrupt_payload IS '旧审批提示'")
                        await connection.execute("COMMENT ON COLUMN run_interrupts.resume_payload IS NULL")
                        await connection.commit()
                    await PostgresRunInterruptRepository(settings).setup()
                async with open_database_connection(settings) as connection:
                    target = await (await connection.execute("SELECT current_database() AS database, current_schema() AS schema")).fetchone()
                    assert target == {"database": "agent_runtime_ra03_probe", "schema": schema}
                    columns = await (await connection.execute("SELECT attname, col_description(attrelid,attnum) AS comment FROM pg_attribute WHERE attrelid='run_interrupts'::regclass AND attnum>0 AND NOT attisdropped")).fetchall()
                    assert len(columns) == 10
                    assert all(row["comment"] and any("\u4e00" <= c <= "\u9fff" for c in row["comment"]) for row in columns), phase
                    by_name = {row["attname"]: row["comment"] for row in columns}
                    assert "文本" in by_name["interrupt_payload"] and "公开" in by_name["interrupt_payload"]
                    assert "答案" in by_name["resume_payload"] and "同一 Run" in by_name["resume_payload"]
                    print(f"RA03_META phase={phase} database={target['database']} schema={target['schema']} table=run_interrupts columns=10 chinese_comments=10 semantic_checks=passed")
        finally:
            # 随机隔离schema由本测试创建，只清理本测试对象，不触碰public或命名卷。
            assert schema.startswith("ra03_comments_") and len(schema) == 46
            async with open_database_connection(base) as connection:
                await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                await connection.commit()
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
