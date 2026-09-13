import asyncio
from datetime import UTC, datetime
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


def test_list_sessions_uses_fixed_user_and_builds_next_cursor() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.pagination import decode_cursor
    from agent_runtime.sessions.service import SessionService

    newer_time = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
    older_time = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
    sessions = [
        Session(
            session_id=UUID("00000000-0000-0000-0000-000000000502"),
            user_id="configured-user",
            title="较新",
            created_at=newer_time,
            updated_at=newer_time,
        ),
        Session(
            session_id=UUID("00000000-0000-0000-0000-000000000501"),
            user_id="configured-user",
            title="较旧",
            created_at=older_time,
            updated_at=older_time,
        ),
    ]
    calls: list[tuple[str, object, int]] = []

    class FakeSessionRepository:
        async def list_page(self, *, user_id, cursor, limit):
            calls.append((user_id, cursor, limit))
            return sessions, True

    service = SessionService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        parent_state_store=object(),
    )

    page = asyncio.run(service.list_sessions(cursor=None, limit=2))

    assert calls == [("configured-user", None, 2)]
    assert page.items == tuple(sessions)
    assert page.next_cursor is not None
    next_cursor = decode_cursor(page.next_cursor)
    assert next_cursor.updated_at == older_time
    assert next_cursor.session_id == sessions[-1].session_id


def test_list_sessions_decodes_cursor_and_omits_cursor_at_last_page() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.models import Session, SessionCursor
    from agent_runtime.sessions.pagination import encode_cursor
    from agent_runtime.sessions.service import SessionService

    updated_at = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    boundary = SessionCursor(
        updated_at=updated_at,
        session_id=UUID("00000000-0000-0000-0000-000000000601"),
    )
    item = Session(
        session_id=UUID("00000000-0000-0000-0000-000000000600"),
        user_id="configured-user",
        title="最后一页",
        created_at=updated_at,
        updated_at=updated_at,
    )
    received_cursor = None

    class FakeSessionRepository:
        async def list_page(self, *, user_id, cursor, limit):
            nonlocal received_cursor
            received_cursor = cursor
            return [item], False

    service = SessionService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        parent_state_store=object(),
    )

    page = asyncio.run(
        service.list_sessions(cursor=encode_cursor(boundary), limit=20)
    )

    assert received_cursor == boundary
    assert page.items == (item,)
    assert page.next_cursor is None


def test_list_sessions_rejects_invalid_cursor_before_database_query() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.pagination import InvalidSessionCursorError
    from agent_runtime.sessions.service import SessionService

    class UnexpectedRepository:
        async def list_page(self, *, user_id, cursor, limit):
            raise AssertionError("非法游标不应访问数据库")

    service = SessionService(
        settings=Settings(_env_file=None),
        session_repository=UnexpectedRepository(),
        parent_state_store=object(),
    )

    with pytest.raises(InvalidSessionCursorError):
        asyncio.run(service.list_sessions(cursor="broken", limit=20))
