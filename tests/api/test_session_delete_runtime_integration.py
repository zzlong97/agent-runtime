"""S2.5-06 活动持久 Run 的 Session 硬删除真实依赖验收。"""

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis

from ..e2e.fake_model import StageTwoBrowserFakeModel


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1"
    or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 和 RUN_REDIS_TESTS=1 后运行真实依赖测试",
)
def test_delete_session_cancels_active_run_and_removes_runtime_data() -> None:
    """删除屏障等待活动 Run 取消，并清理 Event、Run、Redis，最后删除 Session。"""

    from agent_runtime.chat import open_chat_service
    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.main import create_app
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.repository import PostgresRunRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        redis_url=base_settings.redis_url,
        local_user_id=f"s25-session-delete-{uuid4()}",
        run_coordinator_scan_interval_seconds=0.02,
        run_cancel_grace_seconds=0.05,
        _env_file=None,
    )
    session_id: UUID | None = None
    run_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id, run_id
        redis = Redis.from_url(
            settings.redis_connection_string,
            encoding="utf-8",
            decode_responses=True,
        )
        try:
            async with open_chat_service(
                settings,
                model=StageTwoBrowserFakeModel(),
            ) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://runtime.test",
                ) as client:
                    submitted = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            "request_id": str(uuid4()),
                            "message": {"content": "执行长任务"},
                        },
                    )
                    assert submitted.status_code == 202
                    summary = submitted.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])
                    repository = PostgresRunRepository(settings)
                    stream_key = f"runtime:events:{run_id}"

                    for _ in range(200):
                        run = await repository.get(run_id)
                        if run.status == "running" and await redis.xlen(stream_key):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("活动 Run 或 Redis Stream 未在限定时间内就绪")

                    deleted = await client.delete(
                        f"/api/v1/chat/sessions/{session_id}"
                    )
                    assert deleted.status_code == 204

                    async with open_database_connection(settings) as connection:
                        counts = {}
                        for name, query in {
                            "sessions": (
                                "SELECT COUNT(*) AS total FROM sessions "
                                "WHERE session_id = %s"
                            ),
                            "runs": (
                                "SELECT COUNT(*) AS total FROM runs "
                                "WHERE session_id = %s"
                            ),
                            "events": (
                                "SELECT COUNT(*) AS total FROM runtime_events "
                                "WHERE run_id = %s"
                            ),
                        }.items():
                            value = session_id if name != "events" else run_id
                            cursor = await connection.execute(query, (value,))
                            row = await cursor.fetchone()
                            counts[name] = row["total"]
                    assert counts == {"sessions": 0, "runs": 0, "events": 0}
                    assert await redis.exists(stream_key) == 0
        finally:
            await redis.aclose()
            if session_id is not None:
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    for thread_id in (
                        f"{session_id}:general_chat",
                        f"{session_id}:en_to_zh",
                        str(session_id),
                    ):
                        await checkpointer.adelete_thread(thread_id)
                async with open_database_connection(settings) as connection:
                    if run_id is not None:
                        await connection.execute(
                            "DELETE FROM runtime_events WHERE run_id = %s",
                            (run_id,),
                        )
                    await connection.execute(
                        "DELETE FROM runs WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
