"""S2.5 异步 Run API、Coordinator 与 PostgreSQL 真实链路验收。"""

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import PrivateAttr

from ..e2e.fake_model import StageTwoBrowserFakeModel


class BlockingRuntimeFakeModel(StageTwoBrowserFakeModel):
    """在最终回复中提供可控阻塞点，用于确定性查询活动 Run。"""

    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _blocking_execution_count: int = PrivateAttr(default=0)

    async def _astream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        latest_human = next(
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        )
        if "阻塞验收" not in str(latest_human.content):
            async for chunk in super()._astream(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            ):
                yield chunk
            return
        self._blocking_execution_count += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="异步"))
        await self._release.wait()
        yield ChatGenerationChunk(message=AIMessageChunk(content="Run完成"))

    def release(self) -> None:
        """允许当前阻塞的可见回复继续完成。"""

        self._release.set()

    @property
    def blocking_execution_count(self) -> int:
        """返回阻塞回复模型实际开始执行的次数。"""

        return self._blocking_execution_count


def _run_on_psycopg_compatible_loop(coroutine) -> None:
    """在 Windows 上使用 psycopg 支持的 Selector 事件循环。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_async_run_api_persists_before_202_and_executes_after_disconnect() -> None:
    """验证 202、幂等、活动查询、持久事件和最终公共消息。"""

    from agent_runtime.chat import open_chat_service
    from agent_runtime.core.config import Settings
    from agent_runtime.main import create_app
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-async-api-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    request_id = uuid4()
    session_id: UUID | None = None
    run_id: UUID | None = None
    model = BlockingRuntimeFakeModel()

    async def exercise() -> None:
        nonlocal session_id, run_id
        try:
            async with open_chat_service(settings, model=model) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://runtime.test",
                ) as client:
                    payload = {
                        "request_id": str(request_id),
                        "message": {"content": "阻塞验收"},
                    }
                    response = await client.post(
                        "/api/v1/chat/completions",
                        json=payload,
                    )
                    assert response.status_code == 202
                    summary = response.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])
                    assert summary["status"] == "queued"

                    repository = PostgresRunRepository(settings)
                    persisted = await repository.get(run_id)
                    assert persisted.request_id == request_id
                    assert persisted.input_payload == {
                        "message": {"content": "阻塞验收"}
                    }

                    for _ in range(200):
                        started = await repository.get(run_id)
                        if started.status == "running":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("Run 未在限定时间内进入 running")

                    repeated = await client.post(
                        "/api/v1/chat/completions",
                        json=payload,
                    )
                    assert repeated.status_code == 202
                    repeated_summary = repeated.json()
                    for stable_field in (
                        "run_id",
                        "session_id",
                        "response_message_id",
                    ):
                        assert repeated_summary[stable_field] == summary[
                            stable_field
                        ]
                    assert repeated_summary["status"] == "running"
                    assert (
                        await repository.get_by_request_id(request_id)
                    ).run_id == run_id
                    async with open_database_connection(settings) as connection:
                        run_count_cursor = await connection.execute(
                            "SELECT COUNT(*) AS total FROM runs "
                            "WHERE request_id = %s",
                            (request_id,),
                        )
                        run_count = await run_count_cursor.fetchone()
                        session_count_cursor = await connection.execute(
                            "SELECT COUNT(*) AS total FROM sessions "
                            "WHERE user_id = %s",
                            (settings.local_user_id,),
                        )
                        session_count = await session_count_cursor.fetchone()
                    assert run_count["total"] == 1
                    assert session_count["total"] == 1
                    conflict = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            "request_id": str(request_id),
                            "message": {"content": "不同请求"},
                        },
                    )
                    assert conflict.status_code == 409

                    active = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/active-run"
                    )
                    assert active.status_code == 200
                    assert active.json()["run_id"] == str(run_id)
                    assert active.json()["status"] in {"queued", "running"}
                    assert "input_payload" not in active.text

                    # HTTP 请求早已结束，释放模型后 Run 仍由 Coordinator 完成。
                    model.release()
                    for _ in range(200):
                        terminal = await repository.get(run_id)
                        if terminal.status == "completed":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("Run 未在限定时间内完成")

                    assert terminal.input_payload is None
                    idle = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/active-run"
                    )
                    assert idle.status_code == 200
                    assert idle.json() is None
                    history = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                    assert history.status_code == 200
                    items = history.json()["items"]
                    assert [item["content"] for item in items] == [
                        "阻塞验收",
                        "异步Run完成",
                    ]
                    assert items[-1]["message_id"] == summary[
                        "response_message_id"
                    ]

                    events = await PostgresRuntimeEventRepository(
                        settings
                    ).list_public(run_id=run_id, after_seq=0, limit=100)
                    assert [event.event_type for event in events] == [
                        "run.started",
                        "message.started",
                        "message.finalized",
                        "run.completed",
                    ]
                    assert model.blocking_execution_count == 1

                    regenerate_request_id = uuid4()
                    regenerate_path = (
                        f"/api/v1/chat/sessions/{session_id}/messages/"
                        f"{summary['response_message_id']}/regenerate"
                    )
                    regenerate = await client.post(
                        regenerate_path,
                        json={"request_id": str(regenerate_request_id)},
                    )
                    assert regenerate.status_code == 202
                    regenerate_summary = regenerate.json()
                    regenerate_run_id = UUID(regenerate_summary["run_id"])
                    for _ in range(200):
                        regenerated = await repository.get(
                            regenerate_run_id
                        )
                        if regenerated.status == "completed":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("Regenerate Run 未在限定时间内完成")
                    assert regenerated.run_type == "regenerate"
                    assert regenerated.parent_run_id == run_id
                    assert regenerated.input_payload is None

                    repeated_regenerate = await client.post(
                        regenerate_path,
                        json={"request_id": str(regenerate_request_id)},
                    )
                    assert repeated_regenerate.status_code == 202
                    assert repeated_regenerate.json()["run_id"] == str(
                        regenerate_run_id
                    )
                    repeated_run = await repository.get(regenerate_run_id)
                    assert repeated_run.parent_run_id == run_id
                    regenerated_history = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                    regenerated_items = regenerated_history.json()["items"]
                    assert len(regenerated_items) == 2
                    assert regenerated_items[0]["role"] == "user"
                    assert regenerated_items[1]["message_id"] == (
                        regenerate_summary["response_message_id"]
                    )

                    follow_up = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            "request_id": str(uuid4()),
                            "session_id": str(session_id),
                            "message": {"content": "继续对话"},
                        },
                    )
                    assert follow_up.status_code == 202
                    follow_up_run_id = UUID(follow_up.json()["run_id"])
                    for _ in range(200):
                        follow_up_run = await repository.get(follow_up_run_id)
                        if follow_up_run.status == "completed":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("已有 Session 的普通 Run 未完成")
                    follow_up_history = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                    follow_up_items = follow_up_history.json()["items"]
                    assert [item["role"] for item in follow_up_items] == [
                        "user",
                        "assistant",
                        "user",
                        "assistant",
                    ]
                    assert follow_up_items[-2]["content"] == "继续对话"
                    assert follow_up_items[-1]["message_id"] == (
                        follow_up.json()["response_message_id"]
                    )
        finally:
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
                    await connection.execute(
                        "DELETE FROM runtime_events WHERE run_id IN "
                        "(SELECT run_id FROM runs WHERE session_id = %s)",
                        (session_id,),
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

    _run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_regenerate_stage_two_history_keeps_parent_run_id_null() -> None:
    """Stage 2 历史回答没有来源 Run 时不补建虚假父 Run。"""

    from agent_runtime.chat import open_chat_service
    from agent_runtime.core.config import Settings
    from agent_runtime.main import create_app
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.streaming.sse import stream_chat_sse

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-stage-two-history-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            async with open_chat_service(
                settings,
                model=StageTwoBrowserFakeModel(),
            ) as service:
                legacy_turn = await service.prepare_turn(
                    session_id=None,
                    content="Stage 2 历史消息",
                )
                session_id = legacy_turn.session_id
                async for _frame in stream_chat_sse(service, legacy_turn):
                    pass

                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://runtime.test",
                ) as client:
                    request_id = uuid4()
                    path = (
                        f"/api/v1/chat/sessions/{session_id}/messages/"
                        f"{legacy_turn.response_message_id}/regenerate"
                    )
                    response = await client.post(
                        path,
                        json={"request_id": str(request_id)},
                    )
                    assert response.status_code == 202
                    run_id = UUID(response.json()["run_id"])
                    repository = PostgresRunRepository(settings)
                    created = await repository.get(run_id)
                    assert created.run_type == "regenerate"
                    assert created.parent_run_id is None

                    repeated = await client.post(
                        path,
                        json={"request_id": str(request_id)},
                    )
                    assert repeated.status_code == 202
                    assert repeated.json()["run_id"] == str(run_id)
                    assert (await repository.get(run_id)).parent_run_id is None
        finally:
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
                    await connection.execute(
                        "DELETE FROM runtime_events WHERE run_id IN "
                        "(SELECT run_id FROM runs WHERE session_id = %s)",
                        (session_id,),
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

    _run_on_psycopg_compatible_loop(exercise())
