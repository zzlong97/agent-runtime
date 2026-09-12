import asyncio
from uuid import UUID

import pytest


def test_start_new_session_persists_before_routing() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.service import SessionService

    events: list[str] = []
    persisted_sessions = {}
    persisted_messages = {}

    class FakeSessionRepository:
        async def add(self, session) -> None:
            events.append("session")
            persisted_sessions[session.session_id] = session

    class FakeParentStateStore:
        async def store_initial_human_message(self, session_id, message) -> None:
            events.append("human_message")
            persisted_messages[session_id] = [message]

    async def exercise():
        service = SessionService(
            settings=Settings(
                local_user_id="configured-user",
                _env_file=None,
            ),
            session_repository=FakeSessionRepository(),
            parent_state_store=FakeParentStateStore(),
        )

        async def route(start) -> None:
            events.append("router")
            assert persisted_sessions[start.session.session_id] == start.session
            assert persisted_messages[start.session.session_id] == [
                start.human_message
            ]

        return await service.start_new_session(
            content="First HumanMessage",
            route=route,
        )

    started = asyncio.run(exercise())

    assert events == ["session", "human_message", "router"]
    assert started.session.user_id == "configured-user"
    assert started.session.title == "First HumanMessage"
    assert started.session.created_at == started.session.updated_at
    assert started.session.created_at.tzinfo is not None
    assert UUID(str(started.session.session_id)) == started.session.session_id
    assert started.human_message.content == "First HumanMessage"
    assert started.human_message.id is not None
    assert str(UUID(started.human_message.id)) == started.human_message.id


def test_router_failure_keeps_session_and_first_human_message() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.service import SessionService

    persisted_sessions = {}
    persisted_messages = {}
    routed_session_id = None

    class FakeSessionRepository:
        async def add(self, session) -> None:
            persisted_sessions[session.session_id] = session

    class FakeParentStateStore:
        async def store_initial_human_message(self, session_id, message) -> None:
            persisted_messages[session_id] = [message]

    async def exercise() -> None:
        service = SessionService(
            settings=Settings(_env_file=None),
            session_repository=FakeSessionRepository(),
            parent_state_store=FakeParentStateStore(),
        )

        async def failing_route(start) -> None:
            nonlocal routed_session_id
            routed_session_id = start.session.session_id
            raise RuntimeError("router failed")

        with pytest.raises(RuntimeError, match="router failed"):
            await service.start_new_session(
                content="Retain me",
                route=failing_route,
            )

    asyncio.run(exercise())

    assert routed_session_id is not None
    assert routed_session_id in persisted_sessions
    assert persisted_messages[routed_session_id][0].content == "Retain me"


def test_prepare_new_session_persists_without_starting_graph() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.service import SessionService

    events: list[str] = []

    class FakeSessionRepository:
        async def add(self, session) -> None:
            events.append("session")

    class FakeParentStateStore:
        async def store_initial_human_message(self, session_id, message) -> None:
            events.append("human_message")

    service = SessionService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        parent_state_store=FakeParentStateStore(),
    )

    prepared = asyncio.run(service.prepare_new_session(content="准备流式执行"))

    assert events == ["session", "human_message"]
    assert prepared.session.user_id == "configured-user"
    assert prepared.human_message.content == "准备流式执行"
