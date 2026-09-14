import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


def _session(session_id: UUID, *, user_id: str = "configured-user"):
    from agent_runtime.sessions.models import Session

    now = datetime.now(UTC)
    return Session(
        session_id=session_id,
        user_id=user_id,
        title="首条消息",
        created_at=now,
        updated_at=now,
    )


class FakeParentGraph:
    def __init__(self) -> None:
        self.stream_events: list[object] = []
        self.completion_status = "completed"
        self.state_exists = True
        self.stream_calls: list[tuple[dict[str, object], dict[str, object], str]] = []
        self.stream_tasks: list[asyncio.Task[object] | None] = []
        self.updates: list[
            tuple[dict[str, object], dict[str, object], str | None]
        ] = []

    async def astream(self, state, config, *, stream_mode):
        self.stream_calls.append((state, config, stream_mode))
        self.stream_tasks.append(asyncio.current_task())
        for event in self.stream_events:
            if isinstance(event, Exception):
                raise event
            yield event

    async def aget_state(self, config):
        values = (
            {"messages": [], "completion_status": self.completion_status}
            if self.state_exists
            else {}
        )
        return SimpleNamespace(values=values)

    async def aupdate_state(self, config, values, *, as_node=None):
        self.updates.append((config, values, as_node))


def test_prepare_new_turn_uses_persisted_session_and_stable_ids() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.service import SessionStart

    session_id = UUID("00000000-0000-0000-0000-000000000701")
    human_message = HumanMessage(
        content="新会话消息",
        id="00000000-0000-0000-0000-000000000702",
    )
    started = SessionStart(
        session=_session(session_id),
        human_message=human_message,
    )

    class FakeSessionService:
        async def prepare_new_session(
            self,
            *,
            session_id: UUID,
            human_message_id: UUID,
            content: str,
        ):
            assert content == "新会话消息"
            assert session_id == started.session.session_id
            assert human_message_id == UUID(str(started.human_message.id))
            return started

    class UnexpectedRepository:
        async def get(self, session_id):
            raise AssertionError("新会话不应查询已有 Session")

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=UnexpectedRepository(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
        session_id_factory=lambda: session_id,
        human_message_id_factory=lambda: UUID(str(human_message.id)),
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000703"
        ),
    )

    prepared = asyncio.run(service.prepare_turn(session_id=None, content="新会话消息"))

    assert prepared.session_id == session_id
    assert prepared.human_message == human_message
    assert prepared.response_message_id == UUID(
        "00000000-0000-0000-0000-000000000703"
    )
    assert prepared.active_run.session_id == session_id
    assert prepared.config == {
        "configurable": {
            "thread_id": str(session_id),
            "message_id": "00000000-0000-0000-0000-000000000703",
        }
    }


def test_prepare_existing_turn_validates_session_and_parent_state() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000000711")
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            assert requested_session_id == session_id
            return _session(session_id)

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
        human_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000712"
        ),
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000713"
        ),
    )

    prepared = asyncio.run(
        service.prepare_turn(session_id=session_id, content="继续会话")
    )

    assert prepared.human_message.content == "继续会话"
    assert prepared.human_message.id == "00000000-0000-0000-0000-000000000712"


@pytest.mark.parametrize(
    ("user_id", "state_exists", "expected_code", "expected_status"),
    [
        ("other-user", True, "SESSION_NOT_FOUND", 404),
        ("configured-user", False, "SESSION_STATE_NOT_FOUND", 409),
    ],
)
def test_prepare_existing_turn_rejects_invalid_session_before_streaming(
    user_id: str,
    state_exists: bool,
    expected_code: str,
    expected_status: int,
) -> None:
    from agent_runtime.chat import ChatService, ChatSessionError
    from agent_runtime.core.config import Settings

    session_id = uuid4()
    parent = FakeParentGraph()
    parent.state_exists = state_exists

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(session_id, user_id=user_id)

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
    )

    with pytest.raises(ChatSessionError) as captured:
        asyncio.run(service.prepare_turn(session_id=session_id, content="继续会话"))

    assert captured.value.code == expected_code
    assert captured.value.status_code == expected_status


def test_busy_existing_session_is_rejected_before_human_message_creation() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError

    session_id = UUID("00000000-0000-0000-0000-000000001211")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    human_message_factory_calls = 0

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            assert requested_session_id == session_id
            return _session(session_id)

    def human_message_id_factory() -> UUID:
        nonlocal human_message_factory_calls
        human_message_factory_calls += 1
        return UUID("00000000-0000-0000-0000-000000001212")

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
        run_registry=registry,
        human_message_id_factory=human_message_id_factory,
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000001213"
        ),
    )

    async def exercise() -> None:
        occupied = await registry.reserve(
            session_id,
            UUID("00000000-0000-0000-0000-000000001214"),
        )
        with pytest.raises(SessionBusyError):
            await service.prepare_turn(session_id=session_id, content="不应写入")
        await registry.release(occupied)

    asyncio.run(exercise())

    assert human_message_factory_calls == 0
    assert parent.stream_calls == []


def test_new_session_is_reserved_before_persistence_and_uses_injected_ids() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError
    from agent_runtime.sessions.service import SessionStart

    session_id = UUID("00000000-0000-0000-0000-000000001221")
    human_message_id = UUID("00000000-0000-0000-0000-000000001222")
    registry = ActiveRunRegistry()

    class FakeSessionService:
        async def prepare_new_session(
            self,
            *,
            session_id: UUID,
            human_message_id: UUID,
            content: str,
        ) -> SessionStart:
            assert content == "新会话"
            assert session_id == UUID("00000000-0000-0000-0000-000000001221")
            assert human_message_id == UUID(
                "00000000-0000-0000-0000-000000001222"
            )
            with pytest.raises(SessionBusyError):
                await registry.reserve(session_id, uuid4())
            return SessionStart(
                session=_session(session_id),
                human_message=HumanMessage(
                    content=content,
                    id=str(human_message_id),
                ),
            )

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
        session_id_factory=lambda: session_id,
        human_message_id_factory=lambda: human_message_id,
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000001223"
        ),
    )

    async def exercise():
        turn = await service.prepare_turn(session_id=None, content="新会话")
        await registry.release(turn.active_run)
        return turn

    prepared = asyncio.run(exercise())

    assert prepared.session_id == session_id
    assert prepared.human_message.id == str(human_message_id)
    assert prepared.active_run.response_message_id == prepared.response_message_id


def test_new_session_preparation_failure_releases_reservation() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001231")
    registry = ActiveRunRegistry()

    class FailingSessionService:
        async def prepare_new_session(self, **kwargs):
            raise RuntimeError("持久化失败")

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=FailingSessionService(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
        session_id_factory=lambda: session_id,
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="持久化失败"):
            await service.prepare_turn(session_id=None, content="新会话")
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)

    asyncio.run(exercise())


def test_prepare_regeneration_reserves_session_and_uses_checkpoint_fork() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError

    session_id = UUID("00000000-0000-0000-0000-000000001801")
    old_message_id = UUID("00000000-0000-0000-0000-000000001802")
    new_message_id = UUID("00000000-0000-0000-0000-000000001803")
    registry = ActiveRunRegistry()
    fork_config = {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_id": "fork-checkpoint",
            "message_id": str(new_message_id),
        }
    }
    calls: list[tuple[UUID, UUID, UUID]] = []

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeCheckpointForker:
        async def create_fork(
            self,
            *,
            session_id,
            message_id,
            response_message_id,
        ):
            calls.append((session_id, message_id, response_message_id))
            return fork_config

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
        checkpoint_forker=FakeCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=old_message_id,
        )
        with pytest.raises(SessionBusyError):
            await registry.reserve(session_id, uuid4())
        await registry.release(turn.active_run)
        return turn

    prepared = asyncio.run(exercise())

    assert calls == [(session_id, old_message_id, new_message_id)]
    assert prepared.human_message is None
    assert prepared.is_regeneration is True
    assert prepared.response_message_id == new_message_id
    assert prepared.config == fork_config


def test_prepare_regeneration_failure_releases_session_reservation() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.regeneration import RegenerationError
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001811")
    registry = ActiveRunRegistry()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FailingCheckpointForker:
        async def create_fork(self, **kwargs):
            raise RegenerationError(
                code="MESSAGE_REGENERATE_NOT_ALLOWED",
                message=(
                    "仅允许重新生成当前活动分支最新的 completed AIMessage"
                ),
                status_code=409,
            )

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
        checkpoint_forker=FailingCheckpointForker(),
    )

    async def exercise() -> None:
        with pytest.raises(RegenerationError):
            await service.prepare_regeneration(
                session_id=session_id,
                message_id=UUID(
                    "00000000-0000-0000-0000-000000001812"
                ),
            )
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)

    asyncio.run(exercise())


def test_regeneration_stream_resumes_parent_with_none_input() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001821")
    new_message_id = UUID("00000000-0000-0000-0000-000000001823")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    parent.stream_events = [AIMessageChunk(content="新回复")]

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeCheckpointForker:
        async def create_fork(self, **kwargs):
            return {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "fork-checkpoint",
                    "message_id": str(new_message_id),
                }
            }

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=FakeCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=UUID(
                "00000000-0000-0000-0000-000000001822"
            ),
        )
        messages = [message async for message in service.stream_turn(turn)]
        status = await service.get_completion_status(turn)
        await registry.release(turn.active_run)
        return messages, status

    messages, status = asyncio.run(exercise())

    assert [str(message.text) for message in messages] == ["新回复"]
    assert status == "completed"
    assert parent.stream_calls == [
        (
            None,
            {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "fork-checkpoint",
                    "message_id": str(new_message_id),
                }
            },
            "custom",
        )
    ]


def test_unstarted_regeneration_stop_persists_only_stopped_ai_message() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001831")
    new_message_id = UUID("00000000-0000-0000-0000-000000001833")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id):
            pass

    class FakeCheckpointForker:
        async def create_fork(self, **kwargs):
            return {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "fork-checkpoint",
                    "message_id": str(new_message_id),
                }
            }

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=FakeCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise() -> str:
        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=UUID(
                "00000000-0000-0000-0000-000000001832"
            ),
        )
        result = await service.stop_session(session_id=session_id)
        assert turn.active_run.terminal_future.done()
        return result

    result = asyncio.run(exercise())

    assert result == "stopped"
    assert len(parent.updates) == 1
    _config, values, as_node = parent.updates[0]
    assert as_node == "invoke_capability"
    assert len(values["messages"]) == 1
    stopped_message = values["messages"][0]
    assert isinstance(stopped_message, AIMessage)
    assert stopped_message.id == str(new_message_id)
    assert stopped_message.additional_kwargs["runtime_status"] == "stopped"


def test_stop_waits_for_regeneration_fork_handoff_before_persisting() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001841")
    old_message_id = UUID("00000000-0000-0000-0000-000000001842")
    new_message_id = UUID("00000000-0000-0000-0000-000000001843")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    fork_started = asyncio.Event()
    allow_fork_handoff = asyncio.Event()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id):
            pass

    class BlockingCheckpointForker:
        async def create_fork(self, **kwargs):
            fork_started.set()
            await allow_fork_handoff.wait()
            return {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "fork-checkpoint",
                    "message_id": str(new_message_id),
                }
            }

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=BlockingCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        preparation = asyncio.create_task(
            service.prepare_regeneration(
                session_id=session_id,
                message_id=old_message_id,
            )
        )
        await fork_started.wait()
        stop = asyncio.create_task(service.stop_session(session_id=session_id))
        await asyncio.sleep(0)

        assert stop.done() is False
        assert parent.updates == []
        allow_fork_handoff.set()
        turn, stop_result = await asyncio.gather(preparation, stop)

        events = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        assert await registry.get_active(session_id) is None
        assert session_id not in service._prepared_turns
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return turn, stop_result, events

    turn, stop_result, events = asyncio.run(exercise())

    assert stop_result == "stopped"
    assert [event.name for event in events] == ["done"]
    assert events[0].data.status == "stopped"
    assert len(parent.updates) == 1
    config, values, as_node = parent.updates[0]
    assert config == turn.config
    assert as_node == "invoke_capability"
    assert len(values["messages"]) == 1
    stopped_message = values["messages"][0]
    assert stopped_message.id == str(new_message_id)
    assert stopped_message.content == ""
    assert stopped_message.additional_kwargs["runtime_status"] == "stopped"


def test_repeatedly_cancelled_regeneration_finishes_fork_handoff() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001851")
    old_message_id = UUID("00000000-0000-0000-0000-000000001852")
    new_message_id = UUID("00000000-0000-0000-0000-000000001853")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    fork_committed = asyncio.Event()
    allow_config_handoff = asyncio.Event()
    fork_cancelled = asyncio.Event()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id):
            pass

    class CommittedCheckpointForker:
        async def create_fork(self, **kwargs):
            fork_committed.set()
            try:
                await allow_config_handoff.wait()
            except asyncio.CancelledError:
                fork_cancelled.set()
                raise
            return {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "committed-fork",
                    "message_id": str(new_message_id),
                }
            }

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=CommittedCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        preparation = asyncio.create_task(
            service.prepare_regeneration(
                session_id=session_id,
                message_id=old_message_id,
            )
        )
        await fork_committed.wait()
        preparation.cancel()
        await asyncio.sleep(0)
        preparation.cancel()
        await asyncio.sleep(0)
        assert preparation.done() is False
        allow_config_handoff.set()
        result = await asyncio.gather(preparation, return_exceptions=True)

        assert await registry.get_active(session_id) is None
        assert session_id not in service._prepared_turns
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return result[0]

    result = asyncio.run(exercise())

    assert isinstance(result, asyncio.CancelledError)
    assert fork_cancelled.is_set() is False
    assert len(parent.updates) == 1
    config, values, as_node = parent.updates[0]
    assert config["configurable"]["checkpoint_id"] == "committed-fork"
    assert as_node == "invoke_capability"
    assert len(values["messages"]) == 1
    incomplete_message = values["messages"][0]
    assert incomplete_message.id == str(new_message_id)
    assert incomplete_message.content == ""
    assert incomplete_message.additional_kwargs["runtime_status"] == "incomplete"


def test_repeated_preparation_cancel_and_stop_share_stopped_terminal() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001861")
    old_message_id = UUID("00000000-0000-0000-0000-000000001862")
    new_message_id = UUID("00000000-0000-0000-0000-000000001863")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    fork_committed = asyncio.Event()
    allow_config_handoff = asyncio.Event()
    fork_cancelled = asyncio.Event()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id):
            pass

    class CommittedCheckpointForker:
        async def create_fork(self, **kwargs):
            fork_committed.set()
            try:
                await allow_config_handoff.wait()
            except asyncio.CancelledError:
                fork_cancelled.set()
                raise
            return {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "committed-stop-fork",
                    "message_id": str(new_message_id),
                }
            }

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=CommittedCheckpointForker(),
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        preparation = asyncio.create_task(
            service.prepare_regeneration(
                session_id=session_id,
                message_id=old_message_id,
            )
        )
        await fork_committed.wait()
        stop = asyncio.create_task(service.stop_session(session_id=session_id))
        await asyncio.sleep(0)
        preparation.cancel()
        await asyncio.sleep(0)
        preparation.cancel()
        await asyncio.sleep(0)

        assert stop.done() is False
        assert preparation.done() is False
        allow_config_handoff.set()
        preparation_result = await asyncio.gather(
            preparation,
            return_exceptions=True,
        )
        stop_result = await stop

        assert await registry.get_active(session_id) is None
        assert session_id not in service._prepared_turns
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return preparation_result[0], stop_result

    preparation_result, stop_result = asyncio.run(exercise())

    assert isinstance(preparation_result, asyncio.CancelledError)
    assert stop_result == "stopped"
    assert fork_cancelled.is_set() is False
    assert len(parent.updates) == 1
    config, values, as_node = parent.updates[0]
    assert config["configurable"]["checkpoint_id"] == "committed-stop-fork"
    assert as_node == "invoke_capability"
    assert len(values["messages"]) == 1
    stopped_message = values["messages"][0]
    assert stopped_message.id == str(new_message_id)
    assert stopped_message.content == ""
    assert stopped_message.additional_kwargs["runtime_status"] == "stopped"


@pytest.mark.parametrize("failure_kind", ["application_error", "cancelled"])
def test_regeneration_preparation_failure_is_not_reported_as_stopped(
    failure_kind: str,
) -> None:
    from agent_runtime.chat import ChatRuntimeError, ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.regeneration import RegenerationError
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001871")
    old_message_id = UUID("00000000-0000-0000-0000-000000001872")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    fork_started = asyncio.Event()
    allow_failure = asyncio.Event()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id):
            pass

    class FailingCheckpointForker:
        async def create_fork(self, **kwargs):
            fork_started.set()
            await allow_failure.wait()
            if failure_kind == "cancelled":
                raise asyncio.CancelledError
            raise RegenerationError(
                code="MESSAGE_REGENERATE_FORK_FAILED",
                message="创建消息重新生成分支失败",
                retryable=True,
            )

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        checkpoint_forker=FailingCheckpointForker(),
    )

    async def exercise():
        preparation = asyncio.create_task(
            service.prepare_regeneration(
                session_id=session_id,
                message_id=old_message_id,
            )
        )
        await fork_started.wait()
        stop = asyncio.create_task(service.stop_session(session_id=session_id))
        await asyncio.sleep(0)
        assert stop.done() is False

        allow_failure.set()
        preparation_result, stop_result = await asyncio.gather(
            preparation,
            stop,
            return_exceptions=True,
        )
        assert await registry.get_active(session_id) is None
        assert session_id not in service._prepared_turns
        assert session_id not in service._turn_preparation_barriers
        return preparation_result, stop_result

    preparation_result, stop_result = asyncio.run(exercise())

    if failure_kind == "application_error":
        assert isinstance(preparation_result, RegenerationError)
    else:
        assert isinstance(preparation_result, asyncio.CancelledError)
    assert isinstance(stop_result, ChatRuntimeError)
    assert stop_result.code == "MESSAGE_REGENERATE_FORK_FAILED"
    assert stop_result.message == "创建消息重新生成分支失败"
    assert stop_result.retryable is True
    assert parent.updates == []


def test_stream_turn_and_persist_incomplete_message_in_parent() -> None:
    from agent_runtime.chat import ChatService, PreparedChatTurn
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000000721")
    response_message_id = UUID("00000000-0000-0000-0000-000000000723")
    config = {
        "configurable": {
            "thread_id": str(session_id),
            "message_id": str(response_message_id),
        }
    }
    parent = FakeParentGraph()
    parent.stream_events = [
        AIMessageChunk(content="部分"),
        AIMessageChunk(content="输出"),
    ]
    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=object(),
        parent_graph=parent,
    )

    async def exercise():
        registry = ActiveRunRegistry()
        active_run = await registry.reserve(session_id, response_message_id)
        turn = PreparedChatTurn(
            session_id=session_id,
            human_message=HumanMessage(
                content="触发部分输出",
                id="00000000-0000-0000-0000-000000000722",
            ),
            response_message_id=response_message_id,
            config=config,
            active_run=active_run,
        )
        messages = [message async for message in service.stream_turn(turn)]
        status = await service.get_completion_status(turn)
        await service.persist_incomplete(turn, "部分输出")
        await registry.release(active_run)
        return turn, messages, status

    turn, messages, status = asyncio.run(exercise())

    assert [str(message.text) for message in messages] == ["部分", "输出"]
    assert status == "completed"
    assert parent.stream_calls == [
        ({"messages": [turn.human_message]}, config, "custom")
    ]
    assert len(parent.updates) == 1
    update_config, update_values, as_node = parent.updates[0]
    assert update_config == config
    assert as_node == "invoke_capability"
    incomplete_message = update_values["messages"][0]
    assert isinstance(incomplete_message, AIMessage)
    assert incomplete_message.content == "部分输出"
    assert incomplete_message.id == str(response_message_id)
    assert incomplete_message.additional_kwargs["runtime_status"] == "incomplete"


def test_chat_service_delegates_session_listing_to_session_service() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    expected_page = object()
    calls: list[tuple[str | None, int]] = []

    class FakeSessionService:
        async def list_sessions(self, *, cursor, limit):
            calls.append((cursor, limit))
            return expected_page

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
    )

    page = asyncio.run(service.list_sessions(cursor="opaque", limit=7))

    assert page is expected_page
    assert calls == [("opaque", 7)]


def test_chat_service_delegates_session_rename_to_session_service() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000001101")
    expected_session = object()
    calls: list[tuple[UUID, str]] = []

    class FakeSessionService:
        async def rename_session(self, *, session_id, title):
            calls.append((session_id, title))
            return expected_session

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
    )

    renamed = asyncio.run(
        service.rename_session(session_id=session_id, title="新标题")
    )

    assert renamed is expected_session
    assert calls == [(session_id, "新标题")]


def test_chat_service_delegates_feedback_to_feedback_service() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.feedback import FeedbackResult

    message_id = UUID("00000000-0000-0000-0000-000000002731")
    expected_result = FeedbackResult(message_id=message_id, feedback="like")
    calls: list[tuple[UUID, str]] = []

    class FakeFeedbackService:
        async def submit(self, *, message_id, action):
            calls.append((message_id, action))
            return expected_result

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=object(),
        parent_graph=FakeParentGraph(),
        feedback_service=FakeFeedbackService(),
    )

    result = asyncio.run(
        service.submit_feedback(message_id=message_id, action="like")
    )

    assert result is expected_result
    assert calls == [(message_id, "like")]


def test_producer_queues_product_events_and_releases_completed_run() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001241")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    parent.stream_events = [
        AIMessageChunk(content="你"),
        AIMessageChunk(content="好"),
    ]

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        def __init__(self) -> None:
            self.touched: list[UUID] = []

        async def touch_session(self, *, session_id: UUID):
            self.touched.append(session_id)

    session_service = FakeSessionService()

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=session_service,
        parent_graph=parent,
        run_registry=registry,
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000001243"
        ),
    )

    async def exercise():
        caller_task = asyncio.current_task()
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        await turn.active_run.terminal_future
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return turn, events, caller_task

    turn, events, caller_task = asyncio.run(exercise())

    assert [event.name for event in events] == ["message", "message", "done"]
    assert [event.data.delta for event in events[:2]] == ["你", "好"]
    assert all(
        event.data.message_id == turn.response_message_id for event in events[:2]
    )
    assert events[-1].data.status == "completed"
    assert session_service.touched == [session_id]
    assert parent.stream_calls[0][2] == "custom"
    assert parent.stream_tasks[0] is not caller_task


def test_producer_queues_failed_terminal_events_and_releases_run() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001251")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    parent.stream_events = [
        AIMessageChunk(content="部分"),
        ApplicationError(
            code="MODEL_CALL_FAILED",
            message="模型调用失败",
            retryable=True,
        ),
    ]

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        def __init__(self) -> None:
            self.touched: list[UUID] = []

        async def touch_session(self, *, session_id: UUID):
            self.touched.append(session_id)

    session_service = FakeSessionService()

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=session_service,
        parent_graph=parent,
        run_registry=registry,
    )

    async def exercise():
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        await turn.active_run.terminal_future
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return events

    events = asyncio.run(exercise())

    assert [event.name for event in events] == ["message", "error", "done"]
    assert events[1].data.code == "MODEL_CALL_FAILED"
    assert events[1].data.retryable is True
    assert events[2].data.status == "failed"
    assert session_service.touched == [session_id]
    incomplete_message = parent.updates[0][1]["messages"][0]
    assert incomplete_message.content == "部分"
    assert incomplete_message.additional_kwargs["runtime_status"] == "incomplete"


def test_session_timestamp_failure_becomes_failed_terminal_event() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001255")
    registry = ActiveRunRegistry()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FailingSessionService:
        async def touch_session(self, *, session_id: UUID):
            raise RuntimeError("数据库连接细节不得泄漏")

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FailingSessionService(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> list[ProductRunEvent]:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        await turn.active_run.terminal_future
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return events

    events = asyncio.run(exercise())

    assert [event.name for event in events] == ["error", "done"]
    assert events[0].data.code == "CHAT_SESSION_TOUCH_FAILED"
    assert events[0].data.message == "未能更新 Session 的活跃时间"
    assert events[1].data.status == "failed"


@pytest.mark.parametrize("reason", ["stopped", "disconnected"])
def test_cancelled_producer_releases_run_for_every_cancel_reason(reason: str) -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001261")
    registry = ActiveRunRegistry()
    producer_started = asyncio.Event()
    producer_cancelled = asyncio.Event()

    class BlockingParentGraph(FakeParentGraph):
        async def astream(self, state, config, *, stream_mode):
            producer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                producer_cancelled.set()
            yield

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        def __init__(self) -> None:
            self.touched: list[UUID] = []

        async def touch_session(self, *, session_id: UUID):
            self.touched.append(session_id)

    session_service = FakeSessionService()

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=session_service,
        parent_graph=BlockingParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        await producer_started.wait()
        cancelled = await service.cancel_run(turn.active_run, reason=reason)

        assert cancelled is True
        assert producer_cancelled.is_set()
        assert turn.active_run.cancel_reason == reason
        assert session_service.touched == [session_id]
        if reason == "stopped":
            done = await turn.active_run.event_queue.get()
            assert done is not None
            assert done.name == "done"
            assert done.data.status == "stopped"
        assert await turn.active_run.event_queue.get() is None
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("stream_events", "expected_content", "expected_capability_id"),
    [
        ([], "", None),
        (
            [
                AIMessageChunk(
                    content="已经输出",
                    additional_kwargs={"capability_id": "general_chat"},
                )
            ],
            "已经输出",
            "general_chat",
        ),
    ],
)
def test_stop_persists_partial_or_empty_message_and_waits_for_cleanup(
    stream_events: list[AIMessageChunk],
    expected_content: str,
    expected_capability_id: str | None,
) -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001262")
    message_id = UUID("00000000-0000-0000-0000-000000001263")
    registry = ActiveRunRegistry()
    graph_started = asyncio.Event()
    parent = FakeParentGraph()

    async def blocking_stream(state, config, *, stream_mode):
        graph_started.set()
        for event in stream_events:
            yield event
        await asyncio.Event().wait()
        yield

    parent.astream = blocking_stream

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        def __init__(self) -> None:
            self.touched: list[UUID] = []

        async def touch_session(self, *, session_id: UUID):
            self.touched.append(session_id)

    session_service = FakeSessionService()
    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=session_service,
        parent_graph=parent,
        run_registry=registry,
        response_message_id_factory=lambda: message_id,
    )

    async def exercise() -> tuple[str, list[ProductRunEvent]]:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        await graph_started.wait()
        if stream_events:
            first = await turn.active_run.event_queue.get()
            assert first is not None
            assert first.name == "message"

        result = await service.stop_session(session_id=session_id)
        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)

        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return result, events

    result, events = asyncio.run(exercise())

    assert result == "stopped"
    assert [event.name for event in events] == ["done"]
    assert events[0].data.message_id == message_id
    assert events[0].data.capability_id == expected_capability_id
    assert events[0].data.status == "stopped"
    stopped_message = parent.updates[-1][1]["messages"][0]
    assert stopped_message.id == str(message_id)
    assert stopped_message.content == expected_content
    assert stopped_message.additional_kwargs == {
        "runtime_status": "stopped",
        "capability_id": expected_capability_id,
    }
    assert session_service.touched == [session_id]


def test_stop_without_active_run_is_idempotent_and_keeps_history_unchanged() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000001264")
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
    )

    result = asyncio.run(service.stop_session(session_id=session_id))

    assert result == "idle"
    assert parent.updates == []


def test_disconnect_persists_empty_incomplete_message_without_done_event() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001267")
    message_id = UUID("00000000-0000-0000-0000-000000001268")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID):
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        response_message_id_factory=lambda: message_id,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(session_id=session_id, content="未开始即断开")
        assert await service.cancel_run(
            turn.active_run,
            reason="disconnected",
        ) is True
        assert await turn.active_run.event_queue.get() is None

    asyncio.run(exercise())

    messages = parent.updates[-1][1]["messages"]
    assert [message.content for message in messages] == ["未开始即断开", ""]
    assert messages[-1].id == str(message_id)
    assert messages[-1].additional_kwargs == {
        "runtime_status": "incomplete",
        "capability_id": None,
    }


def test_delayed_old_sse_start_cannot_overwrite_replacement_turn_cleanup() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001269")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID):
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
    )

    async def exercise() -> None:
        stale_turn = await service.prepare_turn(
            session_id=session_id,
            content="旧请求",
        )
        assert await service.stop_session(session_id=session_id) == "stopped"

        replacement = await service.prepare_turn(
            session_id=session_id,
            content="替代请求",
        )
        await service.start_producer(stale_turn)
        await asyncio.sleep(0)
        assert await service.cancel_run(
            replacement.active_run,
            reason="disconnected",
        ) is True

    asyncio.run(exercise())

    replacement_messages = parent.updates[-1][1]["messages"]
    assert [message.content for message in replacement_messages] == [
        "替代请求",
        "",
    ]
    assert replacement_messages[-1].additional_kwargs["runtime_status"] == (
        "incomplete"
    )


def test_stop_immediately_after_attachment_recovers_prestart_cancellation() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001270")
    response_message_id = UUID("00000000-0000-0000-0000-000000001271")
    registry = ActiveRunRegistry()
    parent = FakeParentGraph()
    producer_entered = asyncio.Event()

    async def never_entered_stream(state, config, *, stream_mode):
        producer_entered.set()
        await asyncio.Event().wait()
        yield

    parent.astream = never_entered_stream

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID):
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        run_registry=registry,
        response_message_id_factory=lambda: response_message_id,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(
            session_id=session_id,
            content="绑定后立即停止",
        )
        await service.start_producer(turn)
        result = await asyncio.wait_for(
            service.stop_session(session_id=session_id),
            timeout=1,
        )

        assert result == "stopped"
        assert producer_entered.is_set() is False
        done = await turn.active_run.event_queue.get()
        assert done is not None
        assert done.name == "done"
        assert done.data.status == "stopped"
        assert await turn.active_run.event_queue.get() is None
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)

    asyncio.run(exercise())

    messages = parent.updates[-1][1]["messages"]
    assert [message.content for message in messages] == ["绑定后立即停止", ""]
    assert messages[-1].id == str(response_message_id)
    assert messages[-1].additional_kwargs == {
        "runtime_status": "stopped",
        "capability_id": None,
    }


@pytest.mark.parametrize("start_producer", [False, True])
def test_stop_persistence_failure_is_reported_after_run_cleanup(
    start_producer: bool,
) -> None:
    from agent_runtime.chat import ChatRuntimeError, ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001272")
    registry = ActiveRunRegistry()
    producer_started = asyncio.Event()

    class FailingParentGraph(FakeParentGraph):
        async def astream(self, state, config, *, stream_mode):
            producer_started.set()
            await asyncio.Event().wait()
            yield

        async def aupdate_state(self, config, values, *, as_node=None):
            raise RuntimeError("数据库不可用")

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID):
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=FailingParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> list[ProductRunEvent]:
        turn = await service.prepare_turn(
            session_id=session_id,
            content="停止持久化失败",
        )
        if start_producer:
            await service.start_producer(turn)
            await producer_started.wait()

        with pytest.raises(ChatRuntimeError) as captured:
            await asyncio.wait_for(
                service.stop_session(session_id=session_id),
                timeout=1,
            )
        assert captured.value.code == "CHAT_STOP_PERSIST_FAILED"
        assert captured.value.message == "未能保存停止后的回复"
        assert turn.active_run.terminal_future.done()

        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return events

    events = asyncio.run(exercise())

    assert [event.name for event in events] == ["error", "done"]
    assert events[0].data.code == "CHAT_STOP_PERSIST_FAILED"
    assert events[1].data.status == "failed"


@pytest.mark.parametrize("start_producer", [False, True])
def test_stop_session_touch_failure_is_reported_after_run_cleanup(
    start_producer: bool,
) -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.runs import ActiveRunRegistry, ProductRunEvent

    session_id = UUID("00000000-0000-0000-0000-000000001273")
    registry = ActiveRunRegistry()
    producer_started = asyncio.Event()
    parent = FakeParentGraph()

    async def blocking_stream(state, config, *, stream_mode):
        producer_started.set()
        await asyncio.Event().wait()
        yield

    parent.astream = blocking_stream

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FailingSessionService:
        async def touch_session(self, *, session_id: UUID):
            raise RuntimeError("更新时间失败")

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FailingSessionService(),
        parent_graph=parent,
        run_registry=registry,
    )

    async def exercise() -> list[ProductRunEvent]:
        turn = await service.prepare_turn(
            session_id=session_id,
            content="停止时间更新失败",
        )
        if start_producer:
            await service.start_producer(turn)
            await producer_started.wait()

        with pytest.raises(ApplicationError) as captured:
            await asyncio.wait_for(
                service.stop_session(session_id=session_id),
                timeout=1,
            )
        assert captured.value.code == "CHAT_SESSION_TOUCH_FAILED"
        assert turn.active_run.terminal_future.done()

        events: list[ProductRunEvent] = []
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                break
            events.append(event)
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)
        return events

    events = asyncio.run(exercise())

    assert [event.name for event in events] == ["error", "done"]
    assert events[0].data.code == "CHAT_SESSION_TOUCH_FAILED"
    assert events[1].data.status == "failed"


@pytest.mark.parametrize("fail_persistence", [False, True])
def test_concurrent_stop_calls_share_the_same_terminal_result(
    fail_persistence: bool,
) -> None:
    from agent_runtime.chat import ChatRuntimeError, ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001274")
    registry = ActiveRunRegistry()
    producer_started = asyncio.Event()
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    class ControlledParentGraph(FakeParentGraph):
        async def astream(self, state, config, *, stream_mode):
            producer_started.set()
            await asyncio.Event().wait()
            yield

        async def aupdate_state(self, config, values, *, as_node=None):
            persistence_started.set()
            await allow_persistence.wait()
            if fail_persistence:
                raise RuntimeError("停止终态持久化失败")
            await super().aupdate_state(config, values, as_node=as_node)

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID):
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=ControlledParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> list[object]:
        turn = await service.prepare_turn(
            session_id=session_id,
            content="并发停止",
        )
        await service.start_producer(turn)
        await producer_started.wait()

        first = asyncio.create_task(service.stop_session(session_id=session_id))
        await persistence_started.wait()
        second = asyncio.create_task(service.stop_session(session_id=session_id))
        await asyncio.sleep(0)
        assert first.done() is False
        assert second.done() is False
        allow_persistence.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = asyncio.run(exercise())

    if fail_persistence:
        assert all(isinstance(result, ChatRuntimeError) for result in results)
        assert [result.code for result in results] == [
            "CHAT_STOP_PERSIST_FAILED",
            "CHAT_STOP_PERSIST_FAILED",
        ]
    else:
        assert results == ["stopped", "stopped"]


def test_cancel_waits_for_terminal_session_timestamp_update() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001265")
    registry = ActiveRunRegistry()
    touch_started = asyncio.Event()
    allow_touch = asyncio.Event()
    touch_finished = asyncio.Event()
    touch_cancelled = asyncio.Event()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class BlockingSessionService:
        async def touch_session(self, *, session_id: UUID):
            touch_started.set()
            try:
                await allow_touch.wait()
                touch_finished.set()
            except asyncio.CancelledError:
                touch_cancelled.set()
                raise

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=BlockingSessionService(),
        parent_graph=FakeParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        await touch_started.wait()

        cancel_waiter = asyncio.create_task(
            service.cancel_run(turn.active_run, reason="disconnected")
        )
        await asyncio.sleep(0)

        assert touch_cancelled.is_set() is False
        assert cancel_waiter.done() is False
        allow_touch.set()
        assert await cancel_waiter is False
        assert touch_finished.is_set()
        assert turn.active_run.cancel_reason is None

    asyncio.run(exercise())


def test_late_cancel_waits_for_producer_final_registry_release() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry

    session_id = UUID("00000000-0000-0000-0000-000000001266")
    registry = ActiveRunRegistry()
    graph_started = asyncio.Event()
    allow_graph_finish = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    original_release = registry.release

    async def observed_release(run) -> None:
        release_started.set()
        await allow_release.wait()
        await original_release(run)

    registry.release = observed_release

    class BlockingParentGraph(FakeParentGraph):
        async def astream(self, state, config, *, stream_mode):
            graph_started.set()
            await allow_graph_finish.wait()
            if False:
                yield None

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(requested_session_id)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID) -> None:
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=BlockingParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        await service.start_producer(turn)
        await graph_started.wait()

        allow_graph_finish.set()
        await release_started.wait()
        cancel_waiter = asyncio.create_task(
            service.cancel_run(turn.active_run, reason="disconnected")
        )
        await asyncio.sleep(0)

        try:
            assert cancel_waiter.done() is False
            allow_release.set()
            assert await asyncio.wait_for(cancel_waiter, timeout=1) is False
            assert turn.active_run.terminal_future.done()
            assert turn.active_run.cancel_reason is None
            replacement = await registry.reserve(session_id, uuid4())
            await registry.release(replacement)
        finally:
            allow_release.set()
            if not turn.active_run.terminal_future.done():
                await original_release(turn.active_run)
            await asyncio.gather(cancel_waiter, return_exceptions=True)

    asyncio.run(exercise())


def test_open_chat_service_closes_active_runs_before_checkpointers(
    monkeypatch,
) -> None:
    from agent_runtime import chat as chat_module
    from agent_runtime.core.config import Settings

    events: list[str] = []
    captured: dict[str, object] = {}

    class FakeSessionRepository:
        def __init__(self, settings):
            pass

        async def setup(self) -> None:
            events.append("session_setup")

    class FakeParentStateStore:
        def __init__(self, settings):
            pass

    class FakeFeedbackStore:
        def __init__(self, settings):
            captured["feedback_store"] = self

        async def setup(self) -> None:
            events.append("feedback_setup")

    class FakeHistoryAdapter:
        def __init__(self, **kwargs):
            captured["history_feedback_store"] = kwargs["feedback_store"]

    class FakeFeedbackService:
        def __init__(self, **kwargs):
            captured["feedback_target_finder"] = kwargs["target_finder"]
            captured["service_feedback_store"] = kwargs["store"]

    class FakeChatService:
        def __init__(self, **kwargs):
            events.append("service_created")
            captured["chat_feedback_service"] = kwargs["feedback_service"]

        async def close(self) -> None:
            events.append("runs_closed")

    @asynccontextmanager
    async def fake_checkpointers(settings):
        yield SimpleNamespace(parent=None, general_chat=None, en_to_zh=None)
        events.append("checkpointers_closed")

    class FakeRouter:
        def __init__(self, model):
            self.route = object()

    class FakeAdapter:
        def __init__(self, *, capability):
            self.invoke = object()

    monkeypatch.setattr(
        chat_module,
        "PostgresSessionRepository",
        FakeSessionRepository,
    )
    monkeypatch.setattr(
        chat_module,
        "PostgresParentStateStore",
        FakeParentStateStore,
    )
    monkeypatch.setattr(
        chat_module,
        "PostgresFeedbackStore",
        FakeFeedbackStore,
    )
    monkeypatch.setattr(
        chat_module,
        "open_stage_one_checkpointers",
        fake_checkpointers,
    )
    monkeypatch.setattr(chat_module, "StageOneRouter", FakeRouter)
    monkeypatch.setattr(
        chat_module,
        "GeneralChatCapability",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        chat_module,
        "EnglishToChineseCapability",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(chat_module, "GeneralChatAdapter", FakeAdapter)
    monkeypatch.setattr(chat_module, "EnglishToChineseAdapter", FakeAdapter)
    monkeypatch.setattr(
        chat_module,
        "build_parent_graph",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        chat_module,
        "SessionService",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        chat_module,
        "MessageHistoryAdapter",
        FakeHistoryAdapter,
    )
    monkeypatch.setattr(
        chat_module,
        "FeedbackService",
        FakeFeedbackService,
    )
    monkeypatch.setattr(chat_module, "ChatService", FakeChatService)

    async def exercise() -> None:
        async with chat_module.open_chat_service(
            Settings(_env_file=None),
            model=object(),
        ):
            events.append("serving")

    asyncio.run(exercise())

    assert events[:3] == [
        "session_setup",
        "feedback_setup",
        "service_created",
    ]
    assert captured["history_feedback_store"] is captured["feedback_store"]
    assert captured["service_feedback_store"] is captured["feedback_store"]
    assert captured["feedback_target_finder"] is not None
    assert captured["chat_feedback_service"] is not None
    assert events[-2:] == ["runs_closed", "checkpointers_closed"]
