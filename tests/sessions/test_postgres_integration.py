import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def run_on_psycopg_compatible_loop(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_router_failure_retains_session_and_parent_human_message_in_postgres() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.persistence.parent_state import PostgresParentStateStore
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    settings = Settings()
    session_repository = PostgresSessionRepository(settings)
    parent_state_store = PostgresParentStateStore(settings)
    routed_session_id = None

    class ExpectedRouterFailure(RuntimeError):
        pass

    async def exercise() -> None:
        nonlocal routed_session_id
        await session_repository.setup()
        await parent_state_store.setup()
        service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=parent_state_store,
        )

        async def failing_route(start) -> None:
            nonlocal routed_session_id
            routed_session_id = start.session.session_id
            restored_session = await session_repository.get(routed_session_id)
            restored_messages = await parent_state_store.get_messages(
                routed_session_id
            )
            assert restored_session == start.session
            assert restored_messages == [start.human_message]
            raise ExpectedRouterFailure("router failed")

        try:
            with pytest.raises(ExpectedRouterFailure, match="router failed"):
                await service.start_new_session(
                    content="Persist before Router",
                    route=failing_route,
                )

            assert routed_session_id is not None
            restored_session = await session_repository.get(routed_session_id)
            restored_messages = await parent_state_store.get_messages(
                routed_session_id
            )
            assert restored_session.title == "Persist before Router"
            assert restored_session.user_id == settings.local_user_id
            assert restored_messages[0].content == "Persist before Router"
            assert restored_messages[0].id is not None
        finally:
            if routed_session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (routed_session_id,),
                    )
                    await connection.commit()
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    await checkpointer.adelete_thread(str(routed_session_id))

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_session_list_paginates_composite_sort_key_without_gaps() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s2-list-test-{uuid4()}",
        _env_file=None,
    )
    session_repository = PostgresSessionRepository(settings)
    base_time = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    configured_sessions = [
        Session(
            session_id=uuid4(),
            user_id=settings.local_user_id,
            title=f"分页会话 {index}",
            created_at=base_time,
            updated_at=base_time + timedelta(hours=offset),
        )
        for index, offset in enumerate((3, 3, 3, 2, 1), start=1)
    ]
    other_user_session = Session(
        session_id=uuid4(),
        user_id=f"{settings.local_user_id}-other",
        title="不应返回",
        created_at=base_time,
        updated_at=base_time + timedelta(hours=4),
    )
    inserted_ids = [
        *(session.session_id for session in configured_sessions),
        other_user_session.session_id,
    ]

    async def exercise() -> None:
        await session_repository.setup()
        for session in [*configured_sessions, other_user_session]:
            await session_repository.add(session)

        service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=object(),
        )
        received = []
        cursor = None
        try:
            while True:
                page = await service.list_sessions(cursor=cursor, limit=2)
                received.extend(page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

            expected = sorted(
                configured_sessions,
                key=lambda session: (session.updated_at, session.session_id),
                reverse=True,
            )
            assert received == expected
            assert len({session.session_id for session in received}) == 5
            assert all(
                session.user_id == settings.local_user_id for session in received
            )
        finally:
            async with open_database_connection(settings) as connection:
                for session_id in inserted_ids:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_session_rename_persists_across_service_restart_and_hides_other_user() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import (
        PostgresSessionRepository,
        SessionNotFoundError,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s2-rename-test-{uuid4()}",
        _env_file=None,
    )
    session_repository = PostgresSessionRepository(settings)
    original_time = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    owned_session = Session(
        session_id=uuid4(),
        user_id=settings.local_user_id,
        title="旧标题",
        created_at=original_time,
        updated_at=original_time,
    )
    other_session = Session(
        session_id=uuid4(),
        user_id=f"{settings.local_user_id}-other",
        title="其他用户标题",
        created_at=original_time,
        updated_at=original_time,
    )
    inserted_ids = (owned_session.session_id, other_session.session_id)

    async def exercise() -> None:
        await session_repository.setup()
        await session_repository.add(owned_session)
        await session_repository.add(other_session)
        service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=object(),
        )
        try:
            renamed = await service.rename_session(
                session_id=owned_session.session_id,
                title="持久化新标题",
            )

            restarted_repository = PostgresSessionRepository(settings)
            restarted_service = SessionService(
                settings=settings,
                session_repository=restarted_repository,
                parent_state_store=object(),
            )
            restored = await restarted_repository.get(owned_session.session_id)
            assert restored == renamed
            assert restored.title == "持久化新标题"
            assert restored.created_at == original_time
            assert restored.updated_at > original_time

            with pytest.raises(SessionNotFoundError):
                await restarted_service.rename_session(
                    session_id=other_session.session_id,
                    title="越权标题",
                )
            untouched = await restarted_repository.get(other_session.session_id)
            assert untouched.title == "其他用户标题"
            assert untouched.updated_at == original_time
        finally:
            async with open_database_connection(settings) as connection:
                for session_id in inserted_ids:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())
