import asyncio
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_run_setup_does_not_backfill_stage_two_sessions() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-run-legacy-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)

    async def exercise() -> None:
        await session_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Stage 2 历史会话",
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await run_repository.setup()
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    "SELECT COUNT(*) AS total FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                row = await cursor.fetchone()
            assert row is not None
            assert row["total"] == 0
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_run_repository_enforces_idempotency_and_active_uniqueness(
    caplog,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import (
        PostgresRunRepository,
        RunRequestConflictError,
        RunSessionBusyError,
    )
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-run-store-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
    request_id = uuid4()
    fingerprint = build_request_fingerprint(
        run_type="normal",
        session_id=session_id,
        request_payload={"message": {"content": "持久化执行"}},
    )
    original = RunSubmission(
        run_id=uuid4(),
        request_id=request_id,
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "持久化执行"}},
        request_fingerprint=fingerprint,
        created_at=now,
    )
    duplicate = replace(
        original,
        run_id=uuid4(),
        input_message_id=uuid4(),
        response_message_id=uuid4(),
    )
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Run 持久化",
                created_at=now,
                updated_at=now,
            )
        )
        try:
            first, repeated = await asyncio.gather(
                run_repository.create_or_get(original),
                run_repository.create_or_get(duplicate),
            )
            assert {first.created, repeated.created} == {True, False}
            assert first.run == repeated.run
            winning_submission = original if first.created else duplicate
            assert first.run.run_id == winning_submission.run_id
            assert (
                first.run.input_message_id
                == winning_submission.input_message_id
            )
            assert (
                first.run.response_message_id
                == winning_submission.response_message_id
            )
            assert first.run.input_payload == winning_submission.input_payload

            with pytest.raises(RunRequestConflictError) as conflict:
                await run_repository.create_or_get(
                    replace(
                        duplicate,
                        request_fingerprint="b" * 64,
                    )
                )
            assert conflict.value.code == "RUN_REQUEST_CONFLICT"
            assert conflict.value.status_code == 409

            with pytest.raises(RunSessionBusyError) as busy:
                await run_repository.create_or_get(
                    replace(
                        duplicate,
                        run_id=uuid4(),
                        request_id=uuid4(),
                        request_fingerprint="c" * 64,
                    )
                )
            assert busy.value.code == "SESSION_BUSY"
            assert busy.value.status_code == 409
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    with caplog.at_level(logging.INFO, logger="agent_runtime.runtime.repository"):
        run_on_psycopg_compatible_loop(exercise())

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Run持久化创建拒绝" in log_text
    assert '"duration_ms":' in next(
        record.getMessage()
        for record in caplog.records
        if "Run持久化创建完成" in record.getMessage()
    )
    create_log = next(
        record.getMessage()
        for record in caplog.records
        if "Run持久化创建完成" in record.getMessage()
    )
    assert '"input_message_id":' in create_log
    assert '"response_message_id":' in create_log
    assert "持久化执行" not in log_text


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_new_session_and_run_commit_atomically_and_support_active_scan() -> None:
    """新 Session 与幂等 Run 同事务落库，重试不留孤儿 Session。"""

    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import (
        PostgresSessionRepository,
        SessionNotFoundError,
    )

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-run-submit-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)
    session_id = uuid4()
    duplicate_session_id = uuid4()
    request_id = uuid4()
    fingerprint = build_request_fingerprint(
        run_type="normal",
        session_id=None,
        request_payload={"message": {"content": "新 Session 异步提交"}},
    )
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=request_id,
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "新 Session 异步提交"}},
        request_fingerprint=fingerprint,
        created_at=now,
    )
    session = Session(
        session_id=session_id,
        user_id=settings.local_user_id,
        title="新 Session 异步提交",
        created_at=now,
        updated_at=now,
    )
    repository = PostgresRunRepository(settings)
    session_repository = PostgresSessionRepository(settings)

    async def exercise() -> None:
        await session_repository.setup()
        await repository.setup()
        try:
            created = await repository.create_or_get(
                submission,
                new_session=session,
            )
            assert created.created is True
            assert await session_repository.get(session_id) == session
            assert await repository.get_active_for_session(session_id) == (
                created.run
            )
            assert await repository.list_active_for_user(
                user_id=settings.local_user_id
            ) == [created.run]
            assert await repository.list_active_for_user(
                user_id=f"{settings.local_user_id}-other"
            ) == []

            duplicate = replace(
                submission,
                run_id=uuid4(),
                session_id=duplicate_session_id,
                thread_id=str(duplicate_session_id),
                input_message_id=uuid4(),
                response_message_id=uuid4(),
            )
            repeated = await repository.create_or_get(
                duplicate,
                new_session=replace(
                    session,
                    session_id=duplicate_session_id,
                ),
            )
            assert repeated.created is False
            assert repeated.run == created.run
            with pytest.raises(SessionNotFoundError):
                await session_repository.get(duplicate_session_id)
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id IN (%s, %s)",
                    (session_id, duplicate_session_id),
                )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())
