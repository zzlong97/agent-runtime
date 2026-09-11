import asyncio
import os

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
