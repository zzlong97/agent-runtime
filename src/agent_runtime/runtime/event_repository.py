"""RuntimeEvent 序号租约、持久化与 Run 状态原子提交。"""

import logging
from collections.abc import Mapping
from datetime import datetime
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime.event_models import (
    EventDurability,
    EventVisibility,
    RuntimeEvent,
    RunEventCommit,
    SequenceBlock,
)
from agent_runtime.runtime.event_schemas import (
    STATEFUL_PUBLIC_EVENT_TYPES,
    project_public_event,
)
from agent_runtime.runtime.models import (
    TERMINAL_RUN_STATUSES,
    JsonValue,
    Run,
    RunStatus,
    can_transition_run,
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
        ("running", "run.completed", "completed"),
        ("running", "run.failed", "failed"),
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
                if not can_transition_run(current.status, target_status):
                    raise RunStateConflictError(
                        code="RUN_STATE_CONFLICT",
                        message="Run 状态不允许执行该转换",
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

    def _validate_state_transition_event(
        self,
        *,
        run_id: UUID,
        event: RuntimeEvent,
    ) -> None:
        self._validate_durable_event(event, allow_stateful=True)
        if (
            event.run_id != run_id
            or event.event_type not in STATEFUL_PUBLIC_EVENT_TYPES
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
