"""持久 Run 的 PostgreSQL Schema、幂等创建与状态转换。"""

import logging
from collections.abc import Mapping
from datetime import datetime
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime.models import (
    JsonValue,
    Run,
    RunCreateResult,
    RunStatus,
    RunSubmission,
    RunType,
)

logger = logging.getLogger(__name__)

_ACTIVE_RUN_INDEX_NAME = "runs_one_active_per_session_idx"

_CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES sessions(session_id),
    thread_id TEXT NOT NULL CHECK (length(thread_id) > 0),
    parent_run_id UUID NULL REFERENCES runs(run_id),
    run_type TEXT NOT NULL CHECK (run_type IN ('normal', 'regenerate')),
    input_message_id UUID NOT NULL,
    response_message_id UUID NOT NULL,
    start_checkpoint_id TEXT NULL,
    input_payload JSONB NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued',
            'running',
            'recovering',
            'interrupted',
            'cancel_requested',
            'completed',
            'failed',
            'cancelled'
        )
    ),
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    seq_high_watermark BIGINT NOT NULL DEFAULT 0,
    error_code TEXT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT runs_request_id_key UNIQUE (request_id)
)
"""

_CREATE_ACTIVE_RUN_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_ACTIVE_RUN_INDEX_NAME}
ON runs (session_id)
WHERE status IN (
    'queued',
    'running',
    'recovering',
    'interrupted',
    'cancel_requested'
)
"""

_RUN_COLUMNS = """
run_id,
request_id,
session_id,
thread_id,
parent_run_id,
run_type,
input_message_id,
response_message_id,
start_checkpoint_id,
input_payload,
request_fingerprint,
status,
recovery_attempts,
seq_high_watermark,
error_code,
error_message,
created_at,
started_at,
finished_at,
updated_at
"""

_INSERT_RUN = f"""
INSERT INTO runs (
    run_id,
    request_id,
    session_id,
    thread_id,
    parent_run_id,
    run_type,
    input_message_id,
    response_message_id,
    start_checkpoint_id,
    input_payload,
    request_fingerprint,
    status,
    recovery_attempts,
    seq_high_watermark,
    error_code,
    error_message,
    created_at,
    started_at,
    finished_at,
    updated_at
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, 'queued', 0, 0, NULL, NULL, %s, NULL, NULL, %s
)
ON CONFLICT (request_id) DO NOTHING
RETURNING {_RUN_COLUMNS}
"""

_SELECT_RUN = f"""
SELECT {_RUN_COLUMNS}
FROM runs
WHERE run_id = %s
"""

_SELECT_RUN_BY_REQUEST = f"""
SELECT {_RUN_COLUMNS}
FROM runs
WHERE request_id = %s
"""

class RunPersistenceError(ApplicationError):
    """Run Schema 或持久化读写失败时返回的稳定应用错误。"""


class RunNotFoundError(ApplicationError):
    """指定 Run 不存在。"""


class RunRequestConflictError(ApplicationError):
    """同一 request_id 被用于不同规范化请求。"""


class RunSessionBusyError(ApplicationError):
    """数据库检测到同一 Session 已存在活动 Run。"""


class RunStateConflictError(ApplicationError):
    """Run 状态变化不符合已确认状态机或试图覆盖终态。"""


def _run_from_row(row: Mapping[str, Any]) -> Run:
    """把 PostgreSQL 行转换为不可变 Run 领域对象。"""

    return Run(
        run_id=UUID(str(row["run_id"])),
        request_id=UUID(str(row["request_id"])),
        session_id=UUID(str(row["session_id"])),
        thread_id=str(row["thread_id"]),
        parent_run_id=(
            UUID(str(row["parent_run_id"]))
            if row["parent_run_id"] is not None
            else None
        ),
        run_type=cast(RunType, row["run_type"]),
        input_message_id=UUID(str(row["input_message_id"])),
        response_message_id=UUID(str(row["response_message_id"])),
        start_checkpoint_id=(
            str(row["start_checkpoint_id"])
            if row["start_checkpoint_id"] is not None
            else None
        ),
        input_payload=cast(
            dict[str, JsonValue] | None,
            row["input_payload"],
        ),
        request_fingerprint=str(row["request_fingerprint"]),
        status=cast(RunStatus, row["status"]),
        recovery_attempts=int(row["recovery_attempts"]),
        seq_high_watermark=int(row["seq_high_watermark"]),
        error_code=(
            str(row["error_code"]) if row["error_code"] is not None else None
        ),
        error_message=(
            str(row["error_message"])
            if row["error_message"] is not None
            else None
        ),
        created_at=cast(datetime, row["created_at"]),
        started_at=cast(datetime | None, row["started_at"]),
        finished_at=cast(datetime | None, row["finished_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


class PostgresRunRepository:
    """以 PostgreSQL Run 作为执行生命周期权威源。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """创建 Run 表和同 Session 单活动 Run 的部分唯一索引。"""

        operation_started_at = perf_counter()
        log_business_event(logger, "Run持久化初始化开始")
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_CREATE_RUNS_TABLE)
                await connection.execute(_CREATE_ACTIVE_RUN_INDEX)
                await connection.commit()
        except Exception as error:
            log_business_event(
                logger,
                "Run持久化初始化失败",
                level=logging.ERROR,
                error_code="RUN_PERSISTENCE_SETUP_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise RunPersistenceError(
                code="RUN_PERSISTENCE_SETUP_FAILED",
                message="Run 持久化初始化失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "Run持久化初始化完成",
            duration_ms=_elapsed_ms(operation_started_at),
        )

    async def create_or_get(
        self,
        submission: RunSubmission,
    ) -> RunCreateResult:
        """幂等创建 queued Run；重复 request_id 返回数据库中的原 Run。"""

        operation_started_at = perf_counter()
        log_business_event(
            logger,
            "Run持久化创建开始",
            run_id=submission.run_id,
            request_id=submission.request_id,
            session_id=submission.session_id,
            run_type=submission.run_type,
            input_message_id=submission.input_message_id,
            response_message_id=submission.response_message_id,
        )
        try:
            try:
                result = await self._insert_or_resolve_request(submission)
            except UniqueViolation as error:
                result = await self._resolve_unique_violation(
                    submission,
                    error,
                )
        except ApplicationError as error:
            failed = error.status_code >= 500
            log_business_event(
                logger,
                "Run持久化创建失败" if failed else "Run持久化创建拒绝",
                level=logging.ERROR if failed else logging.INFO,
                run_id=submission.run_id,
                request_id=submission.request_id,
                session_id=submission.session_id,
                input_message_id=submission.input_message_id,
                response_message_id=submission.response_message_id,
                error_code=error.code,
                error_type=(
                    type(error.__cause__).__name__
                    if error.__cause__ is not None
                    else type(error).__name__
                ),
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise
        except Exception as error:
            log_business_event(
                logger,
                "Run持久化创建失败",
                level=logging.ERROR,
                run_id=submission.run_id,
                request_id=submission.request_id,
                session_id=submission.session_id,
                input_message_id=submission.input_message_id,
                response_message_id=submission.response_message_id,
                error_code="RUN_PERSIST_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise RunPersistenceError(
                code="RUN_PERSIST_FAILED",
                message="Run 保存失败",
                retryable=True,
            ) from error

        log_business_event(
            logger,
            "Run持久化创建完成",
            run_id=result.run.run_id,
            request_id=result.run.request_id,
            session_id=result.run.session_id,
            run_type=result.run.run_type,
            status=result.run.status,
            created=result.created,
            input_message_id=result.run.input_message_id,
            response_message_id=result.run.response_message_id,
            duration_ms=_elapsed_ms(operation_started_at),
        )
        return result

    async def get(self, run_id: UUID) -> Run:
        """按服务端 Run UUID 读取权威执行生命周期。"""

        operation_started_at = perf_counter()
        log_business_event(logger, "Run持久化读取开始", run_id=run_id)
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(_SELECT_RUN, (run_id,))
                row = await cursor.fetchone()
        except Exception as error:
            log_business_event(
                logger,
                "Run持久化读取失败",
                level=logging.ERROR,
                run_id=run_id,
                error_code="RUN_READ_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise RunPersistenceError(
                code="RUN_READ_FAILED",
                message="Run 读取失败",
                retryable=True,
            ) from error
        if row is None:
            log_business_event(
                logger,
                "Run持久化读取拒绝",
                run_id=run_id,
                error_code="RUN_NOT_FOUND",
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise RunNotFoundError(
                code="RUN_NOT_FOUND",
                message="Run 不存在",
                status_code=404,
            )
        run = _run_from_row(row)
        self._log_read_completed(run, operation_started_at)
        return run

    async def get_by_request_id(self, request_id: UUID) -> Run:
        """按全局 request_id 读取原始 Run，供幂等产品接口复用。"""

        operation_started_at = perf_counter()
        log_business_event(
            logger,
            "Run持久化读取开始",
            request_id=request_id,
        )
        try:
            run = await self._get_by_request_id_or_none(request_id)
        except RunPersistenceError as error:
            log_business_event(
                logger,
                "Run持久化读取失败",
                level=logging.ERROR,
                request_id=request_id,
                error_code=error.code,
                error_type=(
                    type(error.__cause__).__name__
                    if error.__cause__ is not None
                    else type(error).__name__
                ),
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise
        if run is None:
            log_business_event(
                logger,
                "Run持久化读取拒绝",
                request_id=request_id,
                error_code="RUN_NOT_FOUND",
                duration_ms=_elapsed_ms(operation_started_at),
            )
            raise RunNotFoundError(
                code="RUN_NOT_FOUND",
                message="Run 不存在",
                status_code=404,
            )
        self._log_read_completed(run, operation_started_at)
        return run

    async def _insert_or_resolve_request(
        self,
        submission: RunSubmission,
    ) -> RunCreateResult:
        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(
                _INSERT_RUN,
                (
                    submission.run_id,
                    submission.request_id,
                    submission.session_id,
                    submission.thread_id,
                    submission.parent_run_id,
                    submission.run_type,
                    submission.input_message_id,
                    submission.response_message_id,
                    submission.start_checkpoint_id,
                    Jsonb(submission.input_payload),
                    submission.request_fingerprint,
                    submission.created_at,
                    submission.created_at,
                ),
            )
            row = await cursor.fetchone()
            if row is not None:
                await connection.commit()
                return RunCreateResult(run=_run_from_row(row), created=True)

            cursor = await connection.execute(
                _SELECT_RUN_BY_REQUEST,
                (submission.request_id,),
            )
            existing_row = await cursor.fetchone()
            if existing_row is None:
                raise RunPersistenceError(
                    code="RUN_PERSIST_FAILED",
                    message="Run 幂等结果读取失败",
                    retryable=True,
                )
            existing = _run_from_row(existing_row)
            self._ensure_same_request(existing, submission)
            await connection.commit()
            return RunCreateResult(run=existing, created=False)

    async def _resolve_unique_violation(
        self,
        submission: RunSubmission,
        error: UniqueViolation,
    ) -> RunCreateResult:
        if error.diag.constraint_name != _ACTIVE_RUN_INDEX_NAME:
            raise RunPersistenceError(
                code="RUN_PERSIST_FAILED",
                message="Run 保存失败",
                retryable=True,
            ) from error

        existing = await self._get_by_request_id_or_none(submission.request_id)
        if existing is not None:
            self._ensure_same_request(existing, submission)
            return RunCreateResult(run=existing, created=False)
        raise RunSessionBusyError(
            code="SESSION_BUSY",
            message="当前 Session 已有活动 Run",
            status_code=409,
            retryable=True,
        ) from error

    async def _get_by_request_id_or_none(
        self,
        request_id: UUID,
    ) -> Run | None:
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_RUN_BY_REQUEST,
                    (request_id,),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise RunPersistenceError(
                code="RUN_READ_FAILED",
                message="Run 读取失败",
                retryable=True,
            ) from error
        return _run_from_row(row) if row is not None else None

    @staticmethod
    def _log_read_completed(run: Run, operation_started_at: float) -> None:
        """记录不含恢复输入的 Run 读取结果。"""

        log_business_event(
            logger,
            "Run持久化读取完成",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            input_message_id=run.input_message_id,
            response_message_id=run.response_message_id,
            status=run.status,
            duration_ms=_elapsed_ms(operation_started_at),
        )

    @staticmethod
    def _ensure_same_request(
        existing: Run,
        submission: RunSubmission,
    ) -> None:
        if existing.request_fingerprint == submission.request_fingerprint:
            return
        raise RunRequestConflictError(
            code="RUN_REQUEST_CONFLICT",
            message="request_id 已用于不同请求",
            status_code=409,
        )


def _elapsed_ms(started_at: float) -> float:
    """返回适合业务日志的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
