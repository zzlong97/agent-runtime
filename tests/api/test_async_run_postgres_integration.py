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
from ..capability_fixtures import (
    allow_builtin_capabilities,
    cleanup_capability_runtime_for_session,
)


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


class ZeroDeltaFailureFakeModel(StageTwoBrowserFakeModel):
    """仅在最终普通回复阶段于输出任何 delta 前失败。"""

    async def _astream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        system_content = str(messages[0].content)
        latest_human = next(
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        )
        if (
            "普通聊天助手" in system_content
            and "零输出失败" in str(latest_human.content)
        ):
            raise RuntimeError("确定性零输出失败")
        async for chunk in super()._astream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            yield chunk


class WhitespaceReplyFakeModel(StageTwoBrowserFakeModel):
    """返回纯空白文本，用于验证终态正文 fallback。"""

    @staticmethod
    def _visible_reply(messages):
        latest_human = next(
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        )
        if "空白回复" in str(latest_human.content):
            return "   "
        return StageTwoBrowserFakeModel._visible_reply(messages)


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
            await allow_builtin_capabilities(settings)
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
                    assert response.status_code == 202, response.text
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
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
@pytest.mark.parametrize(
    ("content", "model_factory", "terminal_status", "message_status"),
    [
        ("执行长任务", StageTwoBrowserFakeModel, "cancelled", "stopped"),
        ("零输出失败", ZeroDeltaFailureFakeModel, "failed", "incomplete"),
        ("空白回复", WhitespaceReplyFakeModel, "failed", "incomplete"),
    ],
)
def test_cancel_and_failure_persist_nonempty_terminal_message(
    content,
    model_factory,
    terminal_status,
    message_status,
) -> None:
    """显式取消与零 delta 失败都先保存非空公共消息，再提交 Run 终态。"""

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
        local_user_id=f"s25-terminal-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        run_cancel_grace_seconds=0.05,
        _env_file=None,
    )
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            await allow_builtin_capabilities(settings)
            async with open_chat_service(
                settings,
                model=model_factory(),
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
                            "message": {"content": content},
                        },
                    )
                    assert submitted.status_code == 202
                    summary = submitted.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])
                    repository = PostgresRunRepository(settings)

                    if terminal_status == "cancelled":
                        for _ in range(200):
                            running = await repository.get(run_id)
                            if running.status == "running":
                                break
                            await asyncio.sleep(0.01)
                        else:
                            raise AssertionError("待取消 Run 未进入 running")
                        cancelled = await client.post(
                            f"/api/v1/chat/runs/{run_id}/cancel"
                        )
                        assert cancelled.status_code == 202
                        assert cancelled.json()["status"] in {
                            "cancel_requested",
                            "cancelled",
                        }

                    for _ in range(300):
                        terminal = await repository.get(run_id)
                        if terminal.status == terminal_status:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("Run 未在限定时间内形成预期终态")

                    history = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                    assert history.status_code == 200
                    assistant = history.json()["items"][-1]
                    assert assistant["role"] == "assistant"
                    assert assistant["runtime_status"] == message_status
                    assert assistant["message_id"] == summary[
                        "response_message_id"
                    ]
                    assert assistant["content"].strip()

                    events = await PostgresRuntimeEventRepository(
                        settings
                    ).list_public(run_id=run_id, after_seq=0, limit=100)
                    event_types = [event.event_type for event in events]
                    terminal_event = (
                        "run.cancelled"
                        if terminal_status == "cancelled"
                        else "run.failed"
                    )
                    assert event_types.index("message.finalized") < (
                        event_types.index(terminal_event)
                    )
        finally:
            if session_id is not None:
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
def test_completion_and_cancel_publish_only_one_message_finalized() -> None:
    """Cancel 插入完成投影窗口时，公开消息终态与获胜 Run 终态保持唯一一致。"""

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
        local_user_id=f"s25-terminal-race-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            await allow_builtin_capabilities(settings)
            async with open_chat_service(
                settings,
                model=StageTwoBrowserFakeModel(),
            ) as service:
                finalized_ready = asyncio.Event()
                release_finalized = asyncio.Event()
                event_repository = service._runtime_event_repository
                assert event_repository is not None
                original_append = event_repository.append_durable

                async def append_with_barrier(event):
                    if event.event_type == "message.finalized":
                        finalized_ready.set()
                        await release_finalized.wait()
                    return await original_append(event)

                event_repository.append_durable = append_with_barrier
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
                            "message": {"content": "完成取消竞争"},
                        },
                    )
                    assert submitted.status_code == 202
                    summary = submitted.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])
                    await asyncio.wait_for(finalized_ready.wait(), timeout=2)

                    cancel_task = asyncio.create_task(
                        client.post(f"/api/v1/chat/runs/{run_id}/cancel")
                    )
                    await asyncio.sleep(0.03)
                    assert not cancel_task.done()
                    assert (
                        await PostgresRunRepository(settings).get(run_id)
                    ).status == "running"

                    release_finalized.set()
                    cancel_response = await asyncio.wait_for(
                        cancel_task,
                        timeout=2,
                    )
                    assert cancel_response.status_code == 202
                    assert cancel_response.json()["status"] == "completed"

                    events = await PostgresRuntimeEventRepository(
                        settings
                    ).list_public(run_id=run_id, after_seq=0, limit=100)
                    finalized_events = [
                        event
                        for event in events
                        if event.event_type == "message.finalized"
                    ]
                    assert len(finalized_events) == 1
                    assert finalized_events[0].payload["runtime_status"] == (
                        "completed"
                    )
                    assert events[-1].event_type == "run.completed"
        finally:
            if session_id is not None:
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
def test_cancel_after_parent_checkpoint_closes_open_invocation() -> None:
    """Parent 终态已落盘后取消仍必须关闭当前 open Invocation。"""

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
        local_user_id=f"s3-12-cancel-invocation-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            await allow_builtin_capabilities(settings)
            async with open_chat_service(
                settings,
                model=StageTwoBrowserFakeModel(),
            ) as service:
                parent_checkpoint_ready = asyncio.Event()
                release_projection = asyncio.Event()
                original_require = service._require_nonempty_persistent_message

                async def require_with_barrier(**kwargs):
                    message = await original_require(**kwargs)
                    parent_checkpoint_ready.set()
                    await release_projection.wait()
                    return message

                service._require_nonempty_persistent_message = require_with_barrier
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
                            "message": {"content": "取消 Invocation 竞争"},
                        },
                    )
                    assert submitted.status_code == 202
                    summary = submitted.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])
                    await asyncio.wait_for(
                        parent_checkpoint_ready.wait(), timeout=2
                    )

                    cancel_response = await client.post(
                        f"/api/v1/chat/runs/{run_id}/cancel"
                    )
                    assert cancel_response.status_code == 202
                    release_projection.set()

                    repository = PostgresRunRepository(settings)
                    for _ in range(100):
                        terminal = await repository.get(run_id)
                        if terminal.status == "cancelled":
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("取消请求未在限定时间内形成终态")

                    open_invocation = await PostgresRuntimeEventRepository(
                        settings
                    ).get_open_capability_invocation(run_id=run_id)
                    assert open_invocation is None
        finally:
            release_projection.set()
            if session_id is not None:
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
def test_runtime_failure_after_gateway_success_closes_open_invocation() -> None:
    """Gateway 成功后 Runtime 读取失败也必须关闭 open Invocation。"""

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
        local_user_id=f"s3-12-fail-invocation-{uuid4()}",
        redis_url="redis://127.0.0.1:1/0",
        redis_socket_timeout_seconds=0.05,
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            await allow_builtin_capabilities(settings)
            async with open_chat_service(
                settings,
                model=StageTwoBrowserFakeModel(),
            ) as service:

                async def fail_completion_status(_turn):
                    raise RuntimeError("模拟 Runtime 完成状态读取失败")

                service.get_completion_status = fail_completion_status
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
                            "message": {"content": "Runtime 读取失败"},
                        },
                    )
                    assert submitted.status_code == 202
                    summary = submitted.json()
                    session_id = UUID(summary["session_id"])
                    run_id = UUID(summary["run_id"])

                    repository = PostgresRunRepository(settings)
                    for _ in range(100):
                        terminal = await repository.get(run_id)
                        if terminal.status == "failed":
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("运行失败未在限定时间内形成终态")

                    open_invocation = await PostgresRuntimeEventRepository(
                        settings
                    ).get_open_capability_invocation(run_id=run_id)
                    assert open_invocation is None
        finally:
            if session_id is not None:
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
            await allow_builtin_capabilities(settings)
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
                    source_response = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            "request_id": str(uuid4()),
                            "message": {"content": "Stage 2 历史消息"},
                        },
                    )
                    assert (
                        source_response.status_code == 202
                    ), source_response.text
                    source_summary = source_response.json()
                    source_run_id = UUID(source_summary["run_id"])
                    session_id = UUID(source_summary["session_id"])
                    source_message_id = UUID(
                        source_summary["response_message_id"]
                    )
                    repository = PostgresRunRepository(settings)
                    for _ in range(300):
                        source_run = await repository.get(source_run_id)
                        if source_run.status in {
                            "completed",
                            "failed",
                            "cancelled",
                        }:
                            assert source_run.status == "completed"
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("来源 Run 未在限定时间内完成")

                    # 模拟升级前仅保留 Parent 公共历史、没有持久 Run 与
                    # Stage 3 Task 的会话，不给历史消息补建虚假关联。
                    await cleanup_capability_runtime_for_session(
                        settings,
                        session_id,
                    )
                    async with open_database_connection(settings) as connection:
                        await connection.execute(
                            "DELETE FROM runtime_events WHERE run_id = %s",
                            (source_run_id,),
                        )
                        await connection.execute(
                            "DELETE FROM runs WHERE run_id = %s",
                            (source_run_id,),
                        )
                        await connection.commit()

                    request_id = uuid4()
                    path = (
                        f"/api/v1/chat/sessions/{session_id}/messages/"
                        f"{source_message_id}/regenerate"
                    )
                    response = await client.post(
                        path,
                        json={"request_id": str(request_id)},
                    )
                    assert response.status_code == 202, response.text
                    run_id = UUID(response.json()["run_id"])
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
                await cleanup_capability_runtime_for_session(
                    settings,
                    session_id,
                )
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
