"""持久 Interrupt 实体、最小查询与恢复请求指纹。"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Literal, cast
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime.models import JsonValue
from agent_runtime.runtime.event_models import RuntimeEvent
from agent_runtime.runtime.models import Run

InterruptStatus = Literal["pending", "resumed", "cancelled"]

logger = logging.getLogger(__name__)

_CREATE_RUN_INTERRUPTS_TABLE = """
CREATE TABLE IF NOT EXISTS run_interrupts (
    run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    interrupt_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'resumed', 'cancelled')),
    interrupt_payload JSONB NOT NULL
        CHECK (jsonb_typeof(interrupt_payload) = 'object'),
    resume_payload JSONB NULL
        CHECK (resume_payload IS NULL OR jsonb_typeof(resume_payload) = 'object'),
    resume_request_id UUID NULL,
    resume_request_fingerprint CHAR(64) NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resumed_at TIMESTAMPTZ NULL,
    cancelled_at TIMESTAMPTZ NULL,
    PRIMARY KEY (run_id, interrupt_id),
    CONSTRAINT run_interrupts_status_fields_check CHECK (
        (status = 'pending'
            AND resume_payload IS NULL
            AND resume_request_id IS NULL
            AND resume_request_fingerprint IS NULL
            AND resumed_at IS NULL
            AND cancelled_at IS NULL)
        OR
        (status = 'resumed'
            AND resume_payload IS NOT NULL
            AND resume_request_id IS NOT NULL
            AND resume_request_fingerprint IS NOT NULL
            AND resumed_at IS NOT NULL
            AND cancelled_at IS NULL)
        OR
        (status = 'cancelled'
            AND resume_payload IS NULL
            AND resume_request_id IS NULL
            AND resume_request_fingerprint IS NULL
            AND resumed_at IS NULL
            AND cancelled_at IS NOT NULL)
    )
)
"""

_CREATE_PENDING_INTERRUPT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS run_interrupts_one_pending_per_run_idx
ON run_interrupts (run_id)
WHERE status = 'pending'
"""

_CREATE_RESUME_REQUEST_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS run_interrupts_resume_request_id_idx
ON run_interrupts (resume_request_id)
WHERE resume_request_id IS NOT NULL
"""

_INTERRUPT_COLUMNS = """
run_id,
interrupt_id,
status,
interrupt_payload,
resume_payload,
resume_request_id,
resume_request_fingerprint,
created_at,
resumed_at,
cancelled_at
"""

_SELECT_PENDING_INTERRUPT = f"""
SELECT {_INTERRUPT_COLUMNS}
FROM run_interrupts
WHERE run_id = %s AND status = 'pending'
"""

_SELECT_LATEST_RESUMED_INTERRUPT = f"""
SELECT {_INTERRUPT_COLUMNS}
FROM run_interrupts
WHERE run_id = %s AND status = 'resumed'
ORDER BY resumed_at DESC, interrupt_id DESC
LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class RunInterrupt:
    """PostgreSQL 中单个 Run 的人工中断投影。"""

    run_id: UUID
    interrupt_id: UUID
    status: InterruptStatus
    interrupt_payload: dict[str, JsonValue]
    resume_payload: dict[str, JsonValue] | None
    resume_request_id: UUID | None
    resume_request_fingerprint: str | None
    created_at: datetime
    resumed_at: datetime | None
    cancelled_at: datetime | None


@dataclass(frozen=True, slots=True)
class InterruptRunCommit:
    """Run、Interrupt 与 durable Event 的单事务提交结果。"""

    run: Run
    interrupt: RunInterrupt
    event: RuntimeEvent | None
    changed: bool


class InterruptPersistenceError(ApplicationError):
    """Interrupt 建表或查询失败。"""


class InterruptStateConflictError(ApplicationError):
    """Interrupt 已恢复、已取消或不属于目标 Run。"""


class InterruptRequestConflictError(ApplicationError):
    """恢复请求标识已用于不同内容或不同 Interrupt。"""


class PostgresRunInterruptRepository:
    """维护最小 run_interrupts 表并提供无正文业务查询。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """创建 Interrupt 表、pending 唯一约束和恢复请求唯一约束。"""

        started_at = perf_counter()
        log_business_event(logger, "Interrupt持久化初始化开始")
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_CREATE_RUN_INTERRUPTS_TABLE)
                await connection.execute(_CREATE_PENDING_INTERRUPT_INDEX)
                await connection.execute(_CREATE_RESUME_REQUEST_INDEX)
                await connection.commit()
        except Exception as error:
            log_business_event(
                logger,
                "Interrupt持久化初始化失败",
                level=logging.ERROR,
                error_code="INTERRUPT_SETUP_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise InterruptPersistenceError(
                code="INTERRUPT_SETUP_FAILED",
                message="Interrupt 持久化初始化失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "Interrupt持久化初始化完成",
            duration_ms=_elapsed_ms(started_at),
        )

    async def get_pending(self, *, run_id: UUID) -> RunInterrupt | None:
        """读取指定 Run 当前唯一待处理 Interrupt。"""

        return await self._get_optional(
            query=_SELECT_PENDING_INTERRUPT,
            run_id=run_id,
            event_name="Interrupt待处理查询",
        )

    async def get_latest_resumed(
        self,
        *,
        run_id: UUID,
    ) -> RunInterrupt | None:
        """读取指定 Run 最近一次已提交的恢复输入，供同进程执行器消费。"""

        return await self._get_optional(
            query=_SELECT_LATEST_RESUMED_INTERRUPT,
            run_id=run_id,
            event_name="Interrupt恢复结果查询",
        )

    async def _get_optional(
        self,
        *,
        query: str,
        run_id: UUID,
        event_name: str,
    ) -> RunInterrupt | None:
        started_at = perf_counter()
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(query, (run_id,))
                row = await cursor.fetchone()
        except Exception as error:
            log_business_event(
                logger,
                f"{event_name}失败",
                level=logging.ERROR,
                run_id=run_id,
                error_code="INTERRUPT_READ_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise InterruptPersistenceError(
                code="INTERRUPT_READ_FAILED",
                message="Interrupt 读取失败",
                retryable=True,
            ) from error
        interrupt = _interrupt_from_row(row) if row is not None else None
        log_business_event(
            logger,
            f"{event_name}完成",
            run_id=run_id,
            interrupt_id=(
                interrupt.interrupt_id if interrupt is not None else None
            ),
            status=interrupt.status if interrupt is not None else "not_found",
            duration_ms=_elapsed_ms(started_at),
        )
        return interrupt


def build_resume_request_fingerprint(
    *,
    run_id: UUID,
    interrupt_id: UUID,
    resume_payload: dict[str, JsonValue],
) -> str:
    """对恢复目标和公开恢复输入计算不记录正文的稳定 SHA-256 指纹。"""

    canonical_request = json.dumps(
        {
            "run_id": str(run_id),
            "interrupt_id": str(interrupt_id),
            "resume_payload": resume_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()


def _interrupt_from_row(row: Any) -> RunInterrupt:
    return RunInterrupt(
        run_id=UUID(str(row["run_id"])),
        interrupt_id=UUID(str(row["interrupt_id"])),
        status=cast(InterruptStatus, row["status"]),
        interrupt_payload=cast(
            dict[str, JsonValue], row["interrupt_payload"]
        ),
        resume_payload=cast(
            dict[str, JsonValue] | None, row["resume_payload"]
        ),
        resume_request_id=(
            UUID(str(row["resume_request_id"]))
            if row["resume_request_id"] is not None
            else None
        ),
        resume_request_fingerprint=(
            str(row["resume_request_fingerprint"])
            if row["resume_request_fingerprint"] is not None
            else None
        ),
        created_at=cast(datetime, row["created_at"]),
        resumed_at=cast(datetime | None, row["resumed_at"]),
        cancelled_at=cast(datetime | None, row["cancelled_at"]),
    )


def _elapsed_ms(started_at: float) -> float:
    """返回业务日志使用的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
