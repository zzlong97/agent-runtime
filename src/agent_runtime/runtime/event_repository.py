"""RuntimeEvent 序号租约、持久化与 Run 状态原子提交。"""

import logging
from collections.abc import Mapping
from datetime import datetime
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime.event_models import (
    CapabilityInvocationEventCommit,
    EventDurability,
    EventVisibility,
    RuntimeEvent,
    RunEventCommit,
    SequenceBlock,
)
from agent_runtime.runtime.event_schemas import (
    CAPABILITY_INVOCATION_EVENT_TYPES,
    CAPABILITY_TASK_EVENT_TYPES,
    STATEFUL_INTERNAL_EVENT_TYPES,
    STATEFUL_PUBLIC_EVENT_TYPES,
    project_public_event,
    validate_internal_event_payload,
)
from agent_runtime.runtime.models import (
    TERMINAL_RUN_STATUSES,
    JsonValue,
    Run,
    RunStatus,
    can_transition_run,
)
from agent_runtime.runtime.interrupts import (
    InterruptRequestConflictError,
    InterruptRunCommit,
    InterruptStateConflictError,
    RunInterrupt,
    _interrupt_from_row,
)
from agent_runtime.runtime.repository import (
    RunNotFoundError,
    RunStateConflictError,
    _run_from_row,
)

logger = logging.getLogger(__name__)

_ALLOWED_STATE_EVENT_TRANSITIONS = frozenset(
    {
        ("queued", "run.started", "running"),
        ("queued", "run.cancelled", "cancelled"),
        (
            "running",
            "internal.run.cancel_requested",
            "cancel_requested",
        ),
        ("running", "run.completed", "completed"),
        ("running", "run.failed", "failed"),
        ("running", "interrupt.required", "interrupted"),
        (
            "running",
            "internal.run.recovery_claimed",
            "recovering",
        ),
        (
            "recovering",
            "internal.run.recovery_claimed",
            "recovering",
        ),
        (
            "recovering",
            "internal.run.recovery_activated",
            "running",
        ),
        ("recovering", "run.completed", "completed"),
        ("recovering", "run.failed", "failed"),
        ("recovering", "interrupt.required", "interrupted"),
        (
            "recovering",
            "internal.run.cancel_requested",
            "cancel_requested",
        ),
        ("interrupted", "run.cancelled", "cancelled"),
        ("interrupted", "interrupt.resumed", "running"),
        ("cancel_requested", "run.cancelled", "cancelled"),
    }
)

_CREATE_RUNTIME_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq BIGINT NOT NULL CHECK (seq > 0),
    event_type TEXT NOT NULL CHECK (length(event_type) > 0),
    source TEXT NOT NULL CHECK (length(source) > 0),
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'internal')),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    durability TEXT NOT NULL CHECK (durability IN ('durable', 'transient')),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT runtime_events_run_seq_key UNIQUE (run_id, seq),
    CONSTRAINT runtime_events_durable_only CHECK (durability = 'durable')
)
"""

_CREATE_PUBLIC_REPLAY_INDEX = """
CREATE INDEX IF NOT EXISTS runtime_events_public_replay_idx
ON runtime_events (run_id, seq)
WHERE visibility = 'public'
"""

_CREATE_TERMINAL_EVENT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_one_terminal_per_run_idx
ON runtime_events (run_id)
WHERE event_type IN ('run.completed', 'run.cancelled', 'run.failed')
"""

_RESERVE_SEQUENCE_BLOCK = """
UPDATE runs
SET seq_high_watermark = seq_high_watermark + %s
WHERE run_id = %s
RETURNING seq_high_watermark
"""

_SELECT_RUN_MESSAGE_ID_FOR_UPDATE = """
SELECT response_message_id
FROM runs
WHERE run_id = %s
FOR UPDATE
"""

_SELECT_LATEST_MESSAGE_ATTEMPT = """
SELECT MAX((payload ->> 'attempt')::INTEGER) AS latest_attempt
FROM runtime_events
WHERE run_id = %s
  AND event_type = 'message.started'
"""

_EVENT_COLUMNS = """
event_id,
run_id,
seq,
event_type,
source,
visibility,
payload,
schema_version,
durability,
created_at
"""

_INSERT_RUNTIME_EVENT = f"""
INSERT INTO runtime_events (
    event_id,
    run_id,
    seq,
    event_type,
    source,
    visibility,
    payload,
    schema_version,
    durability,
    created_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING {_EVENT_COLUMNS}
"""

_SELECT_PUBLIC_EVENTS = f"""
SELECT {_EVENT_COLUMNS}
FROM runtime_events
WHERE run_id = %s
  AND visibility = 'public'
  AND seq > %s
ORDER BY seq ASC
LIMIT %s
"""

_SELECT_RUN_FOR_UPDATE = """
SELECT
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
FROM runs
WHERE run_id = %s
FOR UPDATE
"""

_UPDATE_RUN_STATE = """
UPDATE runs
SET status = %s,
    input_payload = %s,
    error_code = %s,
    error_message = %s,
    started_at = %s,
    finished_at = %s,
    updated_at = %s
WHERE run_id = %s
RETURNING
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

_UPDATE_RUN_RECOVERY_STATE = """
UPDATE runs
SET status = 'recovering',
    recovery_attempts = recovery_attempts + 1,
    updated_at = %s
WHERE run_id = %s
RETURNING
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

_SELECT_EVENT_TYPE_EXISTS = """
SELECT EXISTS (
    SELECT 1
    FROM runtime_events
    WHERE run_id = %s AND event_type = %s
) AS event_exists
"""

_SELECT_CAPABILITY_INVOCATION_EVENTS = f"""
SELECT {_EVENT_COLUMNS}
FROM runtime_events
WHERE run_id = %s
  AND event_type = ANY(%s)
ORDER BY seq ASC
"""

_SELECT_CAPABILITY_INVOCATION_TERMINAL = f"""
SELECT {_EVENT_COLUMNS}
FROM runtime_events
WHERE run_id = %s
  AND event_type = %s
  AND payload ->> 'invocation_id' = %s
ORDER BY seq ASC
LIMIT 1
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

_INSERT_PENDING_INTERRUPT = f"""
INSERT INTO run_interrupts (
    run_id,
    interrupt_id,
    status,
    interrupt_payload,
    created_at
)
VALUES (%s, %s, 'pending', %s, %s)
RETURNING {_INTERRUPT_COLUMNS}
"""

_SELECT_INTERRUPT_FOR_UPDATE = f"""
SELECT {_INTERRUPT_COLUMNS}
FROM run_interrupts
WHERE run_id = %s AND interrupt_id = %s
FOR UPDATE
"""

_SELECT_INTERRUPT_BY_RESUME_REQUEST = f"""
SELECT {_INTERRUPT_COLUMNS}
FROM run_interrupts
WHERE resume_request_id = %s
"""

_RESUME_INTERRUPT = f"""
UPDATE run_interrupts
SET status = 'resumed',
    resume_payload = %s,
    resume_request_id = %s,
    resume_request_fingerprint = %s,
    resumed_at = %s
WHERE run_id = %s
  AND interrupt_id = %s
  AND status = 'pending'
RETURNING {_INTERRUPT_COLUMNS}
"""

_CANCEL_PENDING_INTERRUPTS = """
UPDATE run_interrupts
SET status = 'cancelled',
    cancelled_at = %s
WHERE run_id = %s AND status = 'pending'
"""


class RuntimeEventPersistenceError(ApplicationError):
    """RuntimeEvent 建表、序号租约或持久化失败。"""


class PostgresRuntimeEventRepository:
    """以 PostgreSQL 保存 durable RuntimeEvent 和 Run 序号高水位。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """创建 RuntimeEvent 表、公开回放索引与唯一终态约束。"""

        started_at = perf_counter()
        log_business_event(logger, "RuntimeEvent持久化初始化开始")
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_CREATE_RUNTIME_EVENTS_TABLE)
                await connection.execute(_CREATE_PUBLIC_REPLAY_INDEX)
                await connection.execute(_CREATE_TERMINAL_EVENT_INDEX)
                await connection.commit()
        except Exception as error:
            log_business_event(
                logger,
                "RuntimeEvent持久化初始化失败",
                level=logging.ERROR,
                error_code="RUNTIME_EVENT_SETUP_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_SETUP_FAILED",
                message="RuntimeEvent 持久化初始化失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "RuntimeEvent持久化初始化完成",
            duration_ms=_elapsed_ms(started_at),
        )

    async def reserve_sequence_block(
        self,
        *,
        run_id: UUID,
        block_size: int,
    ) -> SequenceBlock:
        """原子推进 Run 高水位并返回当前进程可使用的序号闭区间。"""

        if block_size < 1:
            raise ValueError("RuntimeEvent 序号块大小必须大于等于 1")
        started_at = perf_counter()
        log_business_event(
            logger,
            "RuntimeEvent序号块预留开始",
            run_id=run_id,
            block_size=block_size,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _RESERVE_SEQUENCE_BLOCK,
                    (block_size, run_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RunNotFoundError(
                        code="RUN_NOT_FOUND",
                        message="Run 不存在",
                        status_code=404,
                    )
                last = int(row["seq_high_watermark"])
                await connection.commit()
        except ApplicationError:
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="RuntimeEvent序号块预留失败",
                error_code="RUNTIME_EVENT_SEQUENCE_RESERVE_FAILED",
                error=error,
                started_at=started_at,
                run_id=run_id,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_SEQUENCE_RESERVE_FAILED",
                message="RuntimeEvent 序号预留失败",
                retryable=True,
            ) from error
        block = SequenceBlock(first=last - block_size + 1, last=last)
        log_business_event(
            logger,
            "RuntimeEvent序号块预留完成",
            run_id=run_id,
            first_seq=block.first,
            last_seq=block.last,
            duration_ms=_elapsed_ms(started_at),
        )
        return block

    async def append_durable(self, event: RuntimeEvent) -> RuntimeEvent:
        """持久化已校验的 durable 事件；transient 事件禁止进入数据库。"""

        self._validate_durable_event(event, allow_stateful=False)
        started_at = perf_counter()
        log_business_event(
            logger,
            "RuntimeEvent持久化写入开始",
            run_id=event.run_id,
            event_id=event.event_id,
            event_type=event.event_type,
            seq=event.seq,
            visibility=event.visibility,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                if event.event_type == "message.started":
                    await self._validate_message_started(
                        connection=connection,
                        event=event,
                    )
                stored = await self._insert_event(connection, event)
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "RuntimeEvent持久化写入拒绝",
                run_id=event.run_id,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
                error_code=error.code,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="RuntimeEvent持久化写入失败",
                error_code="RUNTIME_EVENT_PERSIST_FAILED",
                error=error,
                started_at=started_at,
                run_id=event.run_id,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_PERSIST_FAILED",
                message="RuntimeEvent 保存失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "RuntimeEvent持久化写入完成",
            run_id=stored.run_id,
            event_id=stored.event_id,
            event_type=stored.event_type,
            seq=stored.seq,
            visibility=stored.visibility,
            duration_ms=_elapsed_ms(started_at),
        )
        return stored

    async def begin_capability_invocation(
        self,
        event: RuntimeEvent,
    ) -> CapabilityInvocationEventCommit:
        """在 Run 行锁内新建 started，或复用唯一 open Invocation。"""

        self._validate_capability_invocation_event(
            event,
            expected_type="internal.capability.invocation.started",
        )
        started_at = perf_counter()
        try:
            async with open_database_connection(self._settings) as connection:
                await _lock_run_for_capability_event(
                    connection=connection,
                    run_id=event.run_id,
                )
                open_event = await select_open_capability_invocation_in_transaction(
                    connection=connection,
                    run_id=event.run_id,
                )
                if open_event is not None:
                    await connection.commit()
                    return CapabilityInvocationEventCommit(
                        event=open_event,
                        recovered=True,
                    )
                stored = await self._insert_event(connection, event)
                await connection.commit()
        except ApplicationError:
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="Capability Invocation开始事件写入失败",
                error_code="CAPABILITY_INVOCATION_EVENT_FAILED",
                error=error,
                started_at=started_at,
                run_id=event.run_id,
                event_id=event.event_id,
                event_type=event.event_type,
            )
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_FAILED",
                message="Capability Invocation 开始事件保存失败",
                retryable=True,
            ) from error
        return CapabilityInvocationEventCommit(event=stored, recovered=False)

    async def finish_capability_invocation(
        self,
        event: RuntimeEvent,
    ) -> RuntimeEvent:
        """在 Run 行锁内让终态事件与唯一 open Invocation 严格配对。"""

        if event.event_type == "internal.capability.invocation.started":
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_INVALID",
                message="Invocation 终态入口不接受 started 事件",
                status_code=409,
            )
        self._validate_capability_invocation_event(event)
        try:
            async with open_database_connection(self._settings) as connection:
                await _lock_run_for_capability_event(
                    connection=connection,
                    run_id=event.run_id,
                )
                stored = await insert_capability_invocation_terminal(
                    connection=connection,
                    event=event,
                )
                await connection.commit()
        except ApplicationError:
            raise
        except Exception as error:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_FAILED",
                message="Capability Invocation 终态事件保存失败",
                retryable=True,
            ) from error
        return stored

    async def get_open_capability_invocation(
        self,
        *,
        run_id: UUID,
    ) -> RuntimeEvent | None:
        """读取当前唯一 open Invocation，供恢复扫描复用原标识。"""

        try:
            async with open_database_connection(self._settings) as connection:
                return await select_open_capability_invocation_in_transaction(
                    connection=connection,
                    run_id=run_id,
                )
        except ApplicationError:
            raise
        except Exception as error:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_READ_FAILED",
                message="Capability Invocation 生命周期读取失败",
                retryable=True,
            ) from error

    async def transition_run(
        self,
        *,
        run_id: UUID,
        target_status: RunStatus,
        event: RuntimeEvent,
        updated_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RunEventCommit:
        """在单个事务中先更新 Run，再写入对应 durable 状态事件。"""

        self._validate_state_transition_event(
            run_id=run_id,
            event=event,
        )
        started_at = perf_counter()
        current: Run | None = None
        log_business_event(
            logger,
            "Run状态与事件原子提交开始",
            run_id=run_id,
            target_status=target_status,
            event_id=event.event_id,
            event_type=event.event_type,
            seq=event.seq,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_RUN_FOR_UPDATE,
                    (run_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RunNotFoundError(
                        code="RUN_NOT_FOUND",
                        message="Run 不存在",
                        status_code=404,
                    )
                current = _run_from_row(row)
                self._validate_locked_state_event_transition(
                    current_status=current.status,
                    event_type=event.event_type,
                    target_status=target_status,
                )
                if (
                    event.event_type
                    == "internal.run.recovery_activated"
                    and int(event.payload.get("recovery_attempt", 0))
                    != current.recovery_attempts
                ):
                    raise RunStateConflictError(
                        code="RUN_RECOVERY_ATTEMPT_MISMATCH",
                        message="Run 恢复激活次数与权威计数不一致",
                        status_code=409,
                    )
                if not can_transition_run(current.status, target_status):
                    raise RunStateConflictError(
                        code="RUN_STATE_CONFLICT",
                        message="Run 状态不允许执行该转换",
                        status_code=409,
                    )

                if target_status == "cancelled":
                    cancelled_cursor = await connection.execute(
                        _CANCEL_PENDING_INTERRUPTS,
                        (updated_at, run_id),
                    )
                    if (
                        current.status == "interrupted"
                        and cancelled_cursor.rowcount != 1
                    ):
                        raise InterruptStateConflictError(
                            code="INTERRUPT_STATE_CONFLICT",
                            message="Run 缺少可取消的待处理 Interrupt",
                            status_code=409,
                        )

                updated = await self._update_run_state(
                    connection=connection,
                    current=current,
                    target_status=target_status,
                    updated_at=updated_at,
                    error_code=error_code,
                    error_message=error_message,
                )
                stored_event = await self._insert_event(connection, event)
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Run状态与事件原子提交拒绝",
                run_id=run_id,
                status=current.status if current is not None else None,
                target_status=target_status,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
                error_code=error.code,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="Run状态与事件原子提交失败",
                error_code="RUNTIME_EVENT_PERSIST_FAILED",
                error=error,
                started_at=started_at,
                run_id=run_id,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
                target_status=target_status,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_PERSIST_FAILED",
                message="Run 状态与 RuntimeEvent 原子保存失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "Run状态与事件原子提交完成",
            run_id=run_id,
            session_id=updated.session_id,
            status=updated.status,
            event_id=stored_event.event_id,
            event_type=stored_event.event_type,
            seq=stored_event.seq,
            duration_ms=_elapsed_ms(started_at),
        )
        return RunEventCommit(
            run=updated,
            event=stored_event,
            changed=True,
        )

    async def claim_recovery(
        self,
        *,
        run_id: UUID,
        event: RuntimeEvent,
        updated_at: datetime,
    ) -> RunEventCommit:
        """原子递增恢复次数、接管 Run 并写入内部 durable 事件。"""

        self._validate_state_transition_event(run_id=run_id, event=event)
        started_at = perf_counter()
        current: Run | None = None
        log_business_event(
            logger,
            "Run恢复接管开始",
            run_id=run_id,
            event_id=event.event_id,
            seq=event.seq,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_RUN_FOR_UPDATE,
                    (run_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RunNotFoundError(
                        code="RUN_NOT_FOUND",
                        message="Run 不存在",
                        status_code=404,
                    )
                current = _run_from_row(row)
                self._validate_locked_state_event_transition(
                    current_status=current.status,
                    event_type=event.event_type,
                    target_status="recovering",
                )
                if current.recovery_attempts >= 3:
                    raise RunStateConflictError(
                        code="RUN_RECOVERY_EXHAUSTED",
                        message="Run 已达到三次恢复上限",
                        status_code=409,
                    )
                expected_attempt = current.recovery_attempts + 1
                if int(event.payload.get("recovery_attempt", 0)) != expected_attempt:
                    raise RunStateConflictError(
                        code="RUN_RECOVERY_ATTEMPT_MISMATCH",
                        message="Run 恢复事件次数与权威计数不一致",
                        status_code=409,
                    )
                updated_cursor = await connection.execute(
                    _UPDATE_RUN_RECOVERY_STATE,
                    (updated_at, run_id),
                )
                updated_row = await updated_cursor.fetchone()
                if updated_row is None:
                    raise RuntimeEventPersistenceError(
                        code="RUNTIME_EVENT_PERSIST_FAILED",
                        message="Run 恢复状态保存失败",
                        retryable=True,
                    )
                updated = _run_from_row(updated_row)
                stored_event = await self._insert_event(connection, event)
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Run恢复接管拒绝",
                run_id=run_id,
                status=current.status if current is not None else None,
                recovery_attempt=(
                    current.recovery_attempts
                    if current is not None
                    else None
                ),
                error_code=error.code,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="Run恢复接管失败",
                error_code="RUNTIME_EVENT_PERSIST_FAILED",
                error=error,
                started_at=started_at,
                run_id=run_id,
                event_id=event.event_id,
                seq=event.seq,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_PERSIST_FAILED",
                message="Run 恢复接管与 RuntimeEvent 原子保存失败",
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "Run恢复接管完成",
            run_id=run_id,
            session_id=updated.session_id,
            status=updated.status,
            recovery_attempt=updated.recovery_attempts,
            event_id=stored_event.event_id,
            seq=stored_event.seq,
            duration_ms=_elapsed_ms(started_at),
        )
        return RunEventCommit(run=updated, event=stored_event, changed=True)

    async def require_interrupt(
        self,
        *,
        run_id: UUID,
        interrupt_id: UUID,
        interrupt_payload: dict[str, JsonValue],
        event: RuntimeEvent,
        updated_at: datetime,
    ) -> InterruptRunCommit:
        """原子新增 pending Interrupt、置 Run 为 interrupted 并写事件。"""

        self._validate_interrupt_event(
            run_id=run_id,
            interrupt_id=interrupt_id,
            event=event,
            expected_type="interrupt.required",
        )
        started_at = perf_counter()
        log_business_event(
            logger,
            "Interrupt请求原子提交开始",
            run_id=run_id,
            interrupt_id=interrupt_id,
            event_id=event.event_id,
            seq=event.seq,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                current = await self._select_run_for_update(
                    connection=connection,
                    run_id=run_id,
                )
                self._validate_locked_state_event_transition(
                    current_status=current.status,
                    event_type=event.event_type,
                    target_status="interrupted",
                )
                cursor = await connection.execute(
                    _INSERT_PENDING_INTERRUPT,
                    (
                        run_id,
                        interrupt_id,
                        Jsonb(interrupt_payload),
                        updated_at,
                    ),
                )
                interrupt_row = await cursor.fetchone()
                if interrupt_row is None:
                    raise RuntimeEventPersistenceError(
                        code="INTERRUPT_PERSIST_FAILED",
                        message="Interrupt 保存失败",
                        retryable=True,
                    )
                updated = await self._update_run_state(
                    connection=connection,
                    current=current,
                    target_status="interrupted",
                    updated_at=updated_at,
                    error_code=None,
                    error_message=None,
                )
                stored_event = await self._insert_event(connection, event)
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Interrupt请求原子提交拒绝",
                run_id=run_id,
                interrupt_id=interrupt_id,
                error_code=error.code,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="Interrupt请求原子提交失败",
                error_code="INTERRUPT_PERSIST_FAILED",
                error=error,
                started_at=started_at,
                run_id=run_id,
                interrupt_id=interrupt_id,
            )
            raise RuntimeEventPersistenceError(
                code="INTERRUPT_PERSIST_FAILED",
                message="Interrupt、Run 与事件原子保存失败",
                retryable=True,
            ) from error
        interrupt = _interrupt_from_row(interrupt_row)
        log_business_event(
            logger,
            "Interrupt请求原子提交完成",
            run_id=run_id,
            session_id=updated.session_id,
            interrupt_id=interrupt_id,
            status=updated.status,
            seq=stored_event.seq,
            duration_ms=_elapsed_ms(started_at),
        )
        return InterruptRunCommit(
            run=updated,
            interrupt=interrupt,
            event=stored_event,
            changed=True,
        )

    async def resume_interrupt(
        self,
        *,
        run_id: UUID,
        interrupt_id: UUID,
        request_id: UUID,
        request_fingerprint: str,
        resume_payload: dict[str, JsonValue],
        event: RuntimeEvent,
        updated_at: datetime,
    ) -> InterruptRunCommit:
        """行锁内幂等消费 pending Interrupt，并原子恢复同一 Run。"""

        self._validate_interrupt_event(
            run_id=run_id,
            interrupt_id=interrupt_id,
            event=event,
            expected_type="interrupt.resumed",
        )
        started_at = perf_counter()
        log_business_event(
            logger,
            "Interrupt恢复原子提交开始",
            run_id=run_id,
            interrupt_id=interrupt_id,
            request_id=request_id,
            event_id=event.event_id,
            seq=event.seq,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                current = await self._select_run_for_update(
                    connection=connection,
                    run_id=run_id,
                )
                cursor = await connection.execute(
                    _SELECT_INTERRUPT_FOR_UPDATE,
                    (run_id, interrupt_id),
                )
                interrupt_row = await cursor.fetchone()
                if interrupt_row is None:
                    raise InterruptStateConflictError(
                        code="INTERRUPT_STATE_CONFLICT",
                        message="Interrupt 不属于该 Run 或不存在",
                        status_code=409,
                    )
                interrupt = _interrupt_from_row(interrupt_row)
                if interrupt.status == "resumed":
                    self._validate_repeated_resume(
                        interrupt=interrupt,
                        request_id=request_id,
                        request_fingerprint=request_fingerprint,
                    )
                    await connection.commit()
                    return InterruptRunCommit(
                        run=current,
                        interrupt=interrupt,
                        event=None,
                        changed=False,
                    )
                if interrupt.status != "pending":
                    raise InterruptStateConflictError(
                        code="INTERRUPT_STATE_CONFLICT",
                        message="Interrupt 已取消，不能恢复",
                        status_code=409,
                    )
                self._validate_locked_state_event_transition(
                    current_status=current.status,
                    event_type=event.event_type,
                    target_status="running",
                )
                await self._ensure_resume_request_available(
                    connection=connection,
                    run_id=run_id,
                    interrupt_id=interrupt_id,
                    request_id=request_id,
                )
                cursor = await connection.execute(
                    _RESUME_INTERRUPT,
                    (
                        Jsonb(resume_payload),
                        request_id,
                        request_fingerprint,
                        updated_at,
                        run_id,
                        interrupt_id,
                    ),
                )
                resumed_row = await cursor.fetchone()
                if resumed_row is None:
                    raise InterruptStateConflictError(
                        code="INTERRUPT_STATE_CONFLICT",
                        message="Interrupt 已被其他请求处理",
                        status_code=409,
                    )
                updated = await self._update_run_state(
                    connection=connection,
                    current=current,
                    target_status="running",
                    updated_at=updated_at,
                    error_code=None,
                    error_message=None,
                )
                stored_event = await self._insert_event(connection, event)
                await connection.commit()
        except UniqueViolation as error:
            raise InterruptRequestConflictError(
                code="INTERRUPT_REQUEST_CONFLICT",
                message="恢复 request_id 已用于其他请求",
                status_code=409,
            ) from error
        except ApplicationError as error:
            log_business_event(
                logger,
                "Interrupt恢复原子提交拒绝",
                run_id=run_id,
                interrupt_id=interrupt_id,
                request_id=request_id,
                error_code=error.code,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            self._log_persistence_failure(
                event_name="Interrupt恢复原子提交失败",
                error_code="INTERRUPT_RESUME_FAILED",
                error=error,
                started_at=started_at,
                run_id=run_id,
                interrupt_id=interrupt_id,
                request_id=request_id,
            )
            raise RuntimeEventPersistenceError(
                code="INTERRUPT_RESUME_FAILED",
                message="Interrupt 恢复原子保存失败",
                retryable=True,
            ) from error
        resumed = _interrupt_from_row(resumed_row)
        log_business_event(
            logger,
            "Interrupt恢复原子提交完成",
            run_id=run_id,
            session_id=updated.session_id,
            interrupt_id=interrupt_id,
            request_id=request_id,
            status=updated.status,
            seq=stored_event.seq,
            duration_ms=_elapsed_ms(started_at),
        )
        return InterruptRunCommit(
            run=updated,
            interrupt=resumed,
            event=stored_event,
            changed=True,
        )

    @staticmethod
    async def _select_run_for_update(*, connection, run_id: UUID) -> Run:
        cursor = await connection.execute(_SELECT_RUN_FOR_UPDATE, (run_id,))
        row = await cursor.fetchone()
        if row is None:
            raise RunNotFoundError(
                code="RUN_NOT_FOUND",
                message="Run 不存在",
                status_code=404,
            )
        return _run_from_row(row)

    def _validate_interrupt_event(
        self,
        *,
        run_id: UUID,
        interrupt_id: UUID,
        event: RuntimeEvent,
        expected_type: str,
    ) -> None:
        """校验 Interrupt 状态事件的 Run、类型和公开标识一致。"""

        self._validate_state_transition_event(run_id=run_id, event=event)
        if (
            event.event_type != expected_type
            or str(event.payload.get("interrupt_id")) != str(interrupt_id)
        ):
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_SCHEMA_INVALID",
                message="Interrupt 事件与目标标识不一致",
                status_code=409,
            )

    @staticmethod
    def _validate_repeated_resume(
        *,
        interrupt: RunInterrupt,
        request_id: UUID,
        request_fingerprint: str,
    ) -> None:
        if (
            interrupt.resume_request_id == request_id
            and interrupt.resume_request_fingerprint == request_fingerprint
        ):
            return
        if interrupt.resume_request_id == request_id:
            raise InterruptRequestConflictError(
                code="INTERRUPT_REQUEST_CONFLICT",
                message="恢复 request_id 已用于不同内容",
                status_code=409,
            )
        raise InterruptStateConflictError(
            code="INTERRUPT_STATE_CONFLICT",
            message="Interrupt 已恢复，不能再次恢复",
            status_code=409,
        )

    @staticmethod
    async def _ensure_resume_request_available(
        *,
        connection,
        run_id: UUID,
        interrupt_id: UUID,
        request_id: UUID,
    ) -> None:
        cursor = await connection.execute(
            _SELECT_INTERRUPT_BY_RESUME_REQUEST,
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        existing = _interrupt_from_row(row)
        if (
            existing.run_id == run_id
            and existing.interrupt_id == interrupt_id
        ):
            return
        raise InterruptRequestConflictError(
            code="INTERRUPT_REQUEST_CONFLICT",
            message="恢复 request_id 已用于其他请求",
            status_code=409,
        )

    async def list_public(
        self,
        *,
        run_id: UUID,
        after_seq: int,
        limit: int,
    ) -> list[RuntimeEvent]:
        """按 seq 升序读取可公开 durable 事件，不要求序号连续。"""

        if after_seq < 0:
            raise ValueError("RuntimeEvent after_seq 不得小于 0")
        if limit < 1 or limit > 1000:
            raise ValueError("RuntimeEvent 查询条数只允许 1 到 1000")
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_PUBLIC_EVENTS,
                    (run_id, after_seq, limit),
                )
                rows = await cursor.fetchall()
        except Exception as error:
            log_business_event(
                logger,
                "RuntimeEvent公开事件读取失败",
                level=logging.ERROR,
                run_id=run_id,
                error_code="RUNTIME_EVENT_READ_FAILED",
                error_type=type(error).__name__,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_READ_FAILED",
                message="RuntimeEvent 读取失败",
                retryable=True,
            ) from error
        events = [_event_from_row(row) for row in rows]
        for event in events:
            project_public_event(event)
        return events

    async def next_message_attempt(self, *, run_id: UUID) -> int:
        """读取 durable message.started 后返回下一次严格递增尝试编号。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_LATEST_MESSAGE_ATTEMPT,
                    (run_id,),
                )
                row = await cursor.fetchone()
        except Exception as error:
            self._log_persistence_failure(
                event_name="RuntimeEvent生成尝试读取失败",
                error_code="RUNTIME_EVENT_READ_FAILED",
                error=error,
                started_at=perf_counter(),
                run_id=run_id,
            )
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_READ_FAILED",
                message="RuntimeEvent 生成尝试读取失败",
                retryable=True,
            ) from error
        latest = (
            int(row["latest_attempt"])
            if row is not None and row["latest_attempt"] is not None
            else 0
        )
        return latest + 1

    async def has_event_type(self, *, run_id: UUID, event_type: str) -> bool:
        """判断 Run 是否已持久化指定事件类型，用于幂等补齐投影。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_EVENT_TYPE_EXISTS,
                    (run_id, event_type),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_READ_FAILED",
                message="RuntimeEvent 对账读取失败",
                retryable=True,
            ) from error
        return bool(row and row["event_exists"])

    @staticmethod
    async def _insert_event(connection, event: RuntimeEvent) -> RuntimeEvent:
        cursor = await connection.execute(
            _INSERT_RUNTIME_EVENT,
            (
                event.event_id,
                event.run_id,
                event.seq,
                event.event_type,
                event.source,
                event.visibility,
                Jsonb(event.payload),
                event.schema_version,
                event.durability,
                event.created_at,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_PERSIST_FAILED",
                message="RuntimeEvent 保存失败",
                retryable=True,
            )
        return _event_from_row(row)

    @staticmethod
    async def _validate_message_started(
        *,
        connection,
        event: RuntimeEvent,
    ) -> None:
        """锁定 Run 并校验固定消息 ID 与严格递增的生成尝试编号。"""

        cursor = await connection.execute(
            _SELECT_RUN_MESSAGE_ID_FOR_UPDATE,
            (event.run_id,),
        )
        run_row = await cursor.fetchone()
        if run_row is None:
            raise RunNotFoundError(
                code="RUN_NOT_FOUND",
                message="Run 不存在",
                status_code=404,
            )
        if str(event.payload["response_message_id"]) != str(
            run_row["response_message_id"]
        ):
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_MESSAGE_ID_MISMATCH",
                message="RuntimeEvent 响应消息标识与 Run 不一致",
                status_code=409,
            )

        cursor = await connection.execute(
            _SELECT_LATEST_MESSAGE_ATTEMPT,
            (event.run_id,),
        )
        attempt_row = await cursor.fetchone()
        latest_attempt = (
            int(attempt_row["latest_attempt"])
            if attempt_row is not None
            and attempt_row["latest_attempt"] is not None
            else 0
        )
        if int(event.payload["attempt"]) != latest_attempt + 1:
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_MESSAGE_SEQUENCE_INVALID",
                message="message.started attempt 必须从 1 开始并严格递增",
                status_code=409,
            )

    @staticmethod
    async def _update_run_state(
        *,
        connection,
        current: Run,
        target_status: RunStatus,
        updated_at: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> Run:
        terminal = target_status in TERMINAL_RUN_STATUSES
        run_started_at = current.started_at
        if target_status == "running" and run_started_at is None:
            run_started_at = updated_at
        cursor = await connection.execute(
            _UPDATE_RUN_STATE,
            (
                target_status,
                None
                if terminal
                else Jsonb(current.input_payload)
                if current.input_payload is not None
                else None,
                error_code if target_status == "failed" else None,
                error_message if target_status == "failed" else None,
                run_started_at,
                updated_at if terminal else current.finished_at,
                updated_at,
                current.run_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_PERSIST_FAILED",
                message="Run 状态保存失败",
                retryable=True,
            )
        return _run_from_row(row)

    @staticmethod
    def _validate_durable_event(
        event: RuntimeEvent,
        *,
        allow_stateful: bool,
        allow_capability_controlled: bool = False,
    ) -> None:
        if event.durability != "durable":
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_NOT_DURABLE",
                message="transient RuntimeEvent 不允许写入 PostgreSQL",
                status_code=409,
            )
        if event.visibility == "public":
            project_public_event(event)
            if (
                not allow_stateful
                and event.event_type in STATEFUL_PUBLIC_EVENT_TYPES
            ):
                raise RuntimeEventPersistenceError(
                    code="RUNTIME_EVENT_REQUIRES_STATE_TRANSITION",
                    message="Run 状态事件必须与状态变化原子提交",
                    status_code=409,
                )
        elif not event.event_type.startswith("internal."):
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_SCHEMA_INVALID",
                message="内部事件类型必须使用 internal. 前缀",
                status_code=409,
            )
        else:
            if (
                event.event_type
                in CAPABILITY_INVOCATION_EVENT_TYPES | CAPABILITY_TASK_EVENT_TYPES
                and not allow_capability_controlled
            ):
                raise RuntimeEventPersistenceError(
                    code="RUNTIME_EVENT_REQUIRES_CAPABILITY_TRANSACTION",
                    message="Capability 生命周期事件必须通过专用事务入口写入",
                    status_code=409,
                )
            validate_internal_event_payload(
                event_type=event.event_type,
                payload=event.payload,
            )

    @classmethod
    def _validate_capability_invocation_event(
        cls,
        event: RuntimeEvent,
        *,
        expected_type: str | None = None,
    ) -> None:
        """校验 Invocation 生命周期事件只能经专用事务入口落库。"""

        cls._validate_durable_event(
            event,
            allow_stateful=False,
            allow_capability_controlled=True,
        )
        if event.event_type not in CAPABILITY_INVOCATION_EVENT_TYPES:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_INVALID",
                message="事件不是固定 Capability Invocation 生命周期事件",
                status_code=409,
            )
        if expected_type is not None and event.event_type != expected_type:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_EVENT_INVALID",
                message="Capability Invocation 事件类型与事务入口不匹配",
                status_code=409,
            )

    def _validate_state_transition_event(
        self,
        *,
        run_id: UUID,
        event: RuntimeEvent,
    ) -> None:
        self._validate_durable_event(event, allow_stateful=True)
        if (
            event.run_id != run_id
            or event.event_type
            not in STATEFUL_PUBLIC_EVENT_TYPES | STATEFUL_INTERNAL_EVENT_TYPES
        ):
            raise RuntimeEventPersistenceError(
                code="RUNTIME_EVENT_STATE_MISMATCH",
                message="RuntimeEvent 与 Run 状态变化不匹配",
                status_code=409,
            )

    @staticmethod
    def _validate_locked_state_event_transition(
        *,
        current_status: RunStatus,
        event_type: str,
        target_status: RunStatus,
    ) -> None:
        """持有 Run 行锁时校验当前阶段允许的状态—事件三元组。"""

        transition = (current_status, event_type, target_status)
        if transition in _ALLOWED_STATE_EVENT_TRANSITIONS:
            return
        raise RunStateConflictError(
            code="RUN_STATE_CONFLICT",
            message="Run 当前状态、事件类型与目标状态不构成合法转换",
            status_code=409,
        )

    @staticmethod
    def _log_persistence_failure(
        *,
        event_name: str,
        error_code: str,
        error: Exception,
        started_at: float,
        **fields: Any,
    ) -> None:
        log_business_event(
            logger,
            event_name,
            level=logging.ERROR,
            error_code=error_code,
            error_type=type(error).__name__,
            duration_ms=_elapsed_ms(started_at),
            **fields,
        )


async def _lock_run_for_capability_event(*, connection, run_id: UUID) -> None:
    """锁定 Run 行，使 Invocation open 检查与事件写入不可交错。"""

    cursor = await connection.execute(
        _SELECT_RUN_MESSAGE_ID_FOR_UPDATE,
        (run_id,),
    )
    if await cursor.fetchone() is None:
        raise RunNotFoundError(
            code="RUN_NOT_FOUND",
            message="Run 不存在",
            status_code=404,
        )


async def select_open_capability_invocation_in_transaction(
    *,
    connection,
    run_id: UUID,
) -> RuntimeEvent | None:
    """按 seq 重放内部事件并返回唯一未配对的 started 事件。"""

    cursor = await connection.execute(
        _SELECT_CAPABILITY_INVOCATION_EVENTS,
        (run_id, list(CAPABILITY_INVOCATION_EVENT_TYPES)),
    )
    rows = await cursor.fetchall()
    open_event: RuntimeEvent | None = None
    for row in rows:
        event = _event_from_row(row)
        validate_internal_event_payload(
            event_type=event.event_type,
            payload=event.payload,
        )
        if event.event_type == "internal.capability.invocation.started":
            if open_event is not None:
                raise RuntimeEventPersistenceError(
                    code="CAPABILITY_INVOCATION_LIFECYCLE_CORRUPTED",
                    message="同一 Run 存在多个 open Capability Invocation",
                    status_code=409,
                )
            open_event = event
            continue
        if open_event is None:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_LIFECYCLE_CORRUPTED",
                message="Capability Invocation 终态事件缺少 started 事件",
                status_code=409,
            )
        if (
            event.payload["invocation_id"]
            != open_event.payload["invocation_id"]
            or event.payload["capability_id"]
            != open_event.payload["capability_id"]
            or event.payload["requested_task_action"]
            != open_event.payload["requested_task_action"]
            or event.payload["state_scope"]
            != open_event.payload["state_scope"]
        ):
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_LIFECYCLE_CORRUPTED",
                message="Capability Invocation 生命周期事件关联不一致",
                status_code=409,
            )
        open_event = None
    return open_event


async def insert_capability_invocation_terminal(
    *,
    connection,
    event: RuntimeEvent,
) -> RuntimeEvent:
    """在已持有 Run 行锁的事务中校验并写入 Invocation 终态。"""

    PostgresRuntimeEventRepository._validate_capability_invocation_event(event)
    open_event = await select_open_capability_invocation_in_transaction(
        connection=connection,
        run_id=event.run_id,
    )
    if open_event is None:
        cursor = await connection.execute(
            _SELECT_CAPABILITY_INVOCATION_TERMINAL,
            (
                event.run_id,
                event.event_type,
                str(event.payload["invocation_id"]),
            ),
        )
        row = await cursor.fetchone()
        if row is not None:
            existing = _event_from_row(row)
            if existing.payload == event.payload:
                return existing
        raise RuntimeEventPersistenceError(
            code="CAPABILITY_INVOCATION_NOT_OPEN",
            message="Capability Invocation 已结束或尚未开始",
            status_code=409,
        )
    if (
        event.payload["invocation_id"]
        != open_event.payload["invocation_id"]
        or event.payload["capability_id"]
        != open_event.payload["capability_id"]
        or event.payload["requested_task_action"]
        != open_event.payload["requested_task_action"]
        or event.payload["state_scope"]
        != open_event.payload["state_scope"]
    ):
        raise RuntimeEventPersistenceError(
            code="CAPABILITY_INVOCATION_MISMATCH",
            message="终态事件与当前 open Capability Invocation 不匹配",
            status_code=409,
        )
    return await PostgresRuntimeEventRepository._insert_event(connection, event)


async def insert_capability_task_events(
    *,
    connection,
    events: tuple[RuntimeEvent, ...],
) -> tuple[RuntimeEvent, ...]:
    """在调用方 Task 事务内校验并写入固定内部 Task 事件。"""

    stored: list[RuntimeEvent] = []
    for event in events:
        PostgresRuntimeEventRepository._validate_durable_event(
            event,
            allow_stateful=False,
            allow_capability_controlled=True,
        )
        if event.event_type not in CAPABILITY_TASK_EVENT_TYPES:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_TASK_EVENT_INVALID",
                message="事件不是固定 Capability Task 生命周期事件",
                status_code=409,
            )
        stored.append(
            await PostgresRuntimeEventRepository._insert_event(
                connection,
                event,
            )
        )
    return tuple(stored)


def _event_from_row(row: Mapping[str, Any]) -> RuntimeEvent:
    """把 PostgreSQL 行转换为不可变 RuntimeEvent 领域对象。"""

    return RuntimeEvent(
        event_id=UUID(str(row["event_id"])),
        run_id=UUID(str(row["run_id"])),
        seq=int(row["seq"]),
        event_type=str(row["event_type"]),
        source=str(row["source"]),
        visibility=cast(EventVisibility, row["visibility"]),
        payload=cast(dict[str, JsonValue], row["payload"]),
        schema_version=int(row["schema_version"]),
        durability=cast(EventDurability, row["durability"]),
        created_at=cast(datetime, row["created_at"]),
    )


def _elapsed_ms(started_at: float) -> float:
    """返回适合中文业务日志的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
