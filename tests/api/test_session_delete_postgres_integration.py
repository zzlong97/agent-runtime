import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.config import get_stream_writer


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


async def _store_child_checkpoint(checkpointer, *, thread_id: str) -> None:
    """为删除验收写入一份可观测的 Child checkpoint。"""

    version = checkpointer.get_next_version(None, None)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["messages"] = [
        HumanMessage(content="待删除的 Child 状态", id=str(uuid4()))
    ]
    checkpoint["channel_versions"]["messages"] = version
    checkpoint["updated_channels"] = ["messages"]
    await checkpointer.aput(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        },
        checkpoint,
        {"source": "input", "step": -1, "parents": {}},
        {"messages": version},
    )


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_delete_session_stops_run_and_removes_all_postgres_data() -> None:
    """活动 Run 停止后按顺序清除三类状态、反馈和 Session 行。"""

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.feedback import FeedbackService, PostgresFeedbackStore
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.main import create_app
    from agent_runtime.persistence.checkpointers import (
        open_stage_one_checkpointers,
    )
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.persistence.parent_state import PostgresParentStateStore
    from agent_runtime.runs import ActiveRunRegistry, SessionUnavailableError
    from agent_runtime.session_deletion import SessionDeletionService
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    settings = Settings()
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        session_repository = PostgresSessionRepository(settings)
        feedback_store = PostgresFeedbackStore(settings)
        parent_state_store = PostgresParentStateStore(settings)
        await session_repository.setup()
        await feedback_store.setup()
        await parent_state_store.setup()
        session_service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=parent_state_store,
        )
        started = await session_service.prepare_new_session(
            content="等待删除"
        )
        session_id = started.session.session_id
        feedback_message_id = uuid4()

        class FakeTargetFinder:
            async def find_feedback_session(self, *, message_id: UUID) -> UUID:
                assert message_id == feedback_message_id
                assert session_id is not None
                return session_id

        class PausingFeedbackStore:
            def __init__(self) -> None:
                self.write_started = asyncio.Event()
                self.allow_write = asyncio.Event()

            async def upsert(self, **kwargs) -> None:
                self.write_started.set()
                await self.allow_write.wait()
                await feedback_store.upsert(**kwargs)

            async def delete(self, **kwargs) -> None:
                await feedback_store.delete(**kwargs)

        pausing_feedback_store = PausingFeedbackStore()

        class PausingDeletionService:
            """在 Registry 删除临界区内暂停真实持久化清理。"""

            def __init__(self, delegate: SessionDeletionService) -> None:
                self._delegate = delegate
                self.critical_section_entered = asyncio.Event()
                self.allow_delete = asyncio.Event()

            async def delete_persisted_data(
                self,
                *,
                session_id: UUID,
            ) -> None:
                self.critical_section_entered.set()
                await self.allow_delete.wait()
                await self._delegate.delete_persisted_data(
                    session_id=session_id
                )

        try:
            async with open_stage_one_checkpointers(settings) as checkpointers:
                await _store_child_checkpoint(
                    checkpointers.general_chat,
                    thread_id=f"{session_id}:general_chat",
                )
                await _store_child_checkpoint(
                    checkpointers.en_to_zh,
                    thread_id=f"{session_id}:en_to_zh",
                )
                async def route(state, config):
                    return {"resolved_capability_id": "general_chat"}

                async def invoke_capability(state, config):
                    get_stream_writer()(
                        AIMessageChunk(
                            content="已输出部分",
                            additional_kwargs={
                                "capability_id": "general_chat"
                            },
                        )
                    )
                    await asyncio.Event().wait()
                    return CapabilityInvocation(
                        result=ChildResult(
                            status="completed",
                            control_signal=None,
                        ),
                        message=AIMessage(content="不会到达"),
                    )

                parent_graph = build_parent_graph(
                    route=route,
                    invoke_capability=invoke_capability,
                    checkpointer=checkpointers.parent,
                )
                deletion_service = SessionDeletionService(
                    settings=settings,
                    parent_checkpointer=checkpointers.parent,
                    general_chat_checkpointer=checkpointers.general_chat,
                    en_to_zh_checkpointer=checkpointers.en_to_zh,
                    feedback_store=feedback_store,
                    session_repository=session_repository,
                )
                pausing_deletion_service = PausingDeletionService(
                    deletion_service
                )
                run_registry = ActiveRunRegistry()
                feedback_service = FeedbackService(
                    settings=settings,
                    target_finder=FakeTargetFinder(),
                    store=pausing_feedback_store,
                    operation_coordinator=run_registry,
                    now_factory=lambda: datetime.now(UTC),
                )
                service = ChatService(
                    settings=settings,
                    session_repository=session_repository,
                    session_service=session_service,
                    parent_graph=parent_graph,
                    feedback_service=feedback_service,
                    deletion_service=pausing_deletion_service,
                    run_registry=run_registry,
                )
                turn = await service.prepare_turn(
                    session_id=session_id,
                    content="请开始长任务",
                )
                await service.start_producer(turn)
                first_event = await turn.active_run.event_queue.get()
                assert first_event is not None
                assert first_event.name == "message"

                feedback_task = asyncio.create_task(
                    service.submit_feedback(
                        message_id=feedback_message_id,
                        action="like",
                    )
                )
                await pausing_feedback_store.write_started.wait()

                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    deletion_task = asyncio.create_task(
                        client.delete(
                            f"/api/v1/chat/sessions/{session_id}"
                        )
                    )
                    pausing_feedback_store.allow_write.set()
                    try:
                        feedback_result = await feedback_task
                        async with asyncio.timeout(30):
                            await pausing_deletion_service.critical_section_entered.wait()
                        assert deletion_task.done() is False
                        with pytest.raises(SessionUnavailableError):
                            await service.submit_feedback(
                                message_id=feedback_message_id,
                                action="dislike",
                            )
                    except BaseException:
                        deletion_task.cancel()
                        raise
                    finally:
                        pausing_deletion_service.allow_delete.set()
                        await asyncio.gather(
                            deletion_task,
                            return_exceptions=True,
                        )
                    response = deletion_task.result()
                    repeated = await client.delete(
                        f"/api/v1/chat/sessions/{session_id}"
                    )

                assert feedback_result.feedback == "like"
                assert response.status_code == 204
                assert response.content == b""
                assert repeated.status_code == 204
                assert repeated.content == b""

                done_event = await turn.active_run.event_queue.get()
                assert done_event is not None
                assert done_event.name == "done"
                assert done_event.data.status == "stopped"
                assert await turn.active_run.event_queue.get() is None

                thread_ids = (
                    str(session_id),
                    f"{session_id}:general_chat",
                    f"{session_id}:en_to_zh",
                )
                async with open_database_connection(settings) as connection:
                    session_cursor = await connection.execute(
                        "SELECT COUNT(*) AS total FROM sessions "
                        "WHERE session_id = %s",
                        (session_id,),
                    )
                    feedback_cursor = await connection.execute(
                        "SELECT COUNT(*) AS total FROM message_feedback "
                        "WHERE session_id = %s",
                        (session_id,),
                    )
                    session_row = await session_cursor.fetchone()
                    feedback_row = await feedback_cursor.fetchone()
                    assert session_row is not None
                    assert feedback_row is not None
                    assert session_row["total"] == 0
                    assert feedback_row["total"] == 0

                    for table_name in (
                        "checkpoints",
                        "checkpoint_blobs",
                        "checkpoint_writes",
                    ):
                        cursor = await connection.execute(
                            f"SELECT COUNT(*) AS total FROM {table_name} "
                            "WHERE thread_id = ANY(%s)",
                            (list(thread_ids),),
                        )
                        row = await cursor.fetchone()
                        assert row is not None
                        assert row["total"] == 0
        finally:
            if session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM message_feedback WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()
                async with open_stage_one_checkpointers(
                    settings
                ) as checkpointers:
                    await checkpointers.parent.adelete_thread(str(session_id))
                    await checkpointers.general_chat.adelete_thread(
                        f"{session_id}:general_chat"
                    )
                    await checkpointers.en_to_zh.adelete_thread(
                        f"{session_id}:en_to_zh"
                    )

    run_on_psycopg_compatible_loop(exercise())
