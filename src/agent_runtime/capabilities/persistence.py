"""Stage 3 Capability Task、Operation 与权限的 PostgreSQL 持久化。"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from agent_runtime.capabilities.persistence_models import (
    CAPABILITY_TERMINAL_TASK_STATUSES,
    CapabilityOperation,
    CapabilityOperationStatus,
    CapabilityTask,
    CapabilityTaskContext,
    CapabilityTaskStatus,
    UserCapabilityPermission,
    as_operation_status,
    as_task_status,
)
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection

logger = logging.getLogger(__name__)

_CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS capability_tasks (
    task_id UUID PRIMARY KEY,
    session_id UUID NOT NULL
        REFERENCES sessions(session_id) ON DELETE RESTRICT,
    capability_id VARCHAR(64) NOT NULL
        CHECK (capability_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    state_schema_version VARCHAR(64) NOT NULL
        CHECK (length(btrim(state_schema_version)) BETWEEN 1 AND 64),
    status TEXT NOT NULL
        CHECK (status IN ('active', 'completed', 'failed', 'cancelled')),
    last_run_id UUID NULL
        REFERENCES runs(run_id) ON DELETE RESTRICT,
    last_invocation_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NULL,
    CONSTRAINT capability_tasks_context_key
        UNIQUE (task_id, session_id, capability_id),
    CONSTRAINT capability_tasks_status_ended_check CHECK (
        (status = 'active' AND ended_at IS NULL)
        OR (status <> 'active' AND ended_at IS NOT NULL)
    )
)
"""

_CREATE_TASKS_LOOKUP_INDEX = """
CREATE INDEX IF NOT EXISTS capability_tasks_lookup_idx
ON capability_tasks (
    session_id,
    capability_id,
    status,
    updated_at DESC
)
"""

_CREATE_TASKS_LAST_RUN_INDEX = """
CREATE INDEX IF NOT EXISTS capability_tasks_last_run_idx
ON capability_tasks (last_run_id)
WHERE last_run_id IS NOT NULL
"""

_CREATE_CONTEXTS_TABLE = """
CREATE TABLE IF NOT EXISTS capability_task_contexts (
    session_id UUID NOT NULL,
    capability_id VARCHAR(64) NOT NULL
        CHECK (capability_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    current_task_id UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, capability_id),
    UNIQUE (current_task_id),
    FOREIGN KEY (current_task_id, session_id, capability_id)
        REFERENCES capability_tasks (task_id, session_id, capability_id)
        ON DELETE RESTRICT
)
"""

_CREATE_OPERATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS capability_operations (
    operation_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    invocation_id UUID NOT NULL,
    task_id UUID NOT NULL
        REFERENCES capability_tasks(task_id) ON DELETE RESTRICT,
    capability_id VARCHAR(64) NOT NULL
        CHECK (capability_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    operation_key TEXT NOT NULL CHECK (length(btrim(operation_key)) > 0),
    idempotency_key CHAR(64) NOT NULL
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'succeeded', 'failed')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, invocation_id, operation_key),
    UNIQUE (idempotency_key)
)
"""

_CREATE_OPERATIONS_TASK_INDEX = """
CREATE INDEX IF NOT EXISTS capability_operations_task_idx
ON capability_operations (task_id, updated_at DESC)
"""

_CREATE_OPERATIONS_INVOCATION_INDEX = """
CREATE INDEX IF NOT EXISTS capability_operations_invocation_idx
ON capability_operations (run_id, invocation_id)
"""

_CREATE_PERMISSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS user_capability_permissions (
    user_id TEXT NOT NULL CHECK (length(btrim(user_id)) > 0),
    capability_id VARCHAR(64) NOT NULL
        CHECK (capability_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    allowed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, capability_id)
)
"""

_CREATE_PERMISSIONS_LOOKUP_INDEX = """
CREATE INDEX IF NOT EXISTS user_capability_permissions_lookup_idx
ON user_capability_permissions (capability_id, allowed)
"""

_TASK_COLUMNS = """
task_id, session_id, capability_id, state_schema_version, status,
last_run_id, last_invocation_id, created_at, updated_at, ended_at
"""
_CONTEXT_COLUMNS = """
session_id, capability_id, current_task_id, updated_at
"""
_OPERATION_COLUMNS = """
operation_id, run_id, invocation_id, task_id, capability_id,
operation_key, idempotency_key, status, created_at, updated_at
"""
_PERMISSION_COLUMNS = """
user_id, capability_id, allowed, created_at, updated_at
"""

_INSERT_TASK = f"""
INSERT INTO capability_tasks ({_TASK_COLUMNS})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING {_TASK_COLUMNS}
"""

_SELECT_TASK = f"""
SELECT {_TASK_COLUMNS}
FROM capability_tasks
WHERE task_id = %s
"""

_SELECT_TASK_FOR_UPDATE = f"""
SELECT {_TASK_COLUMNS}
FROM capability_tasks
WHERE task_id = %s
FOR UPDATE
"""

_LOCK_SESSION = """
SELECT session_id
FROM sessions
WHERE session_id = %s
FOR UPDATE
"""

_UPSERT_CONTEXT = f"""
WITH target_task AS (
    SELECT task_id
    FROM capability_tasks
    WHERE task_id = %s
      AND session_id = %s
      AND capability_id = %s
      AND status = 'active'
    FOR UPDATE
)
INSERT INTO capability_task_contexts (
    session_id, capability_id, current_task_id, updated_at
)
SELECT %s, %s, %s, %s
FROM target_task
ON CONFLICT (session_id, capability_id) DO UPDATE SET
    current_task_id = EXCLUDED.current_task_id,
    updated_at = EXCLUDED.updated_at
RETURNING {_CONTEXT_COLUMNS}
"""

_SELECT_CONTEXT = f"""
SELECT {_CONTEXT_COLUMNS}
FROM capability_task_contexts
WHERE session_id = %s AND capability_id = %s
"""

_DELETE_CONTEXT = """
DELETE FROM capability_task_contexts
WHERE session_id = %s AND capability_id = %s
"""

_DELETE_CONTEXT_BY_TASK = """
DELETE FROM capability_task_contexts
WHERE current_task_id = %s
"""

_UPDATE_TASK_PROJECTION = f"""
UPDATE capability_tasks
SET last_run_id = %s,
    last_invocation_id = %s,
    updated_at = %s
WHERE task_id = %s
RETURNING {_TASK_COLUMNS}
"""

_FINISH_TASK = f"""
UPDATE capability_tasks
SET status = %s,
    last_run_id = %s,
    last_invocation_id = %s,
    updated_at = %s,
    ended_at = %s
WHERE task_id = %s AND status = 'active'
RETURNING {_TASK_COLUMNS}
"""

_DELETE_TASK = """
DELETE FROM capability_tasks
WHERE task_id = %s
"""

_INSERT_OPERATION = f"""
INSERT INTO capability_operations ({_OPERATION_COLUMNS})
SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
FROM capability_tasks AS task
JOIN runs AS run
  ON run.run_id = %s AND run.session_id = task.session_id
WHERE task.task_id = %s AND task.capability_id = %s
ON CONFLICT DO NOTHING
RETURNING {_OPERATION_COLUMNS}
"""

_SELECT_OPERATION_BY_BUSINESS_KEY = f"""
SELECT {_OPERATION_COLUMNS}
FROM capability_operations
WHERE run_id = %s AND invocation_id = %s AND operation_key = %s
"""

_SELECT_OPERATION_BY_IDEMPOTENCY_KEY = f"""
SELECT {_OPERATION_COLUMNS}
FROM capability_operations
WHERE idempotency_key = %s
"""

_UPDATE_OPERATION_STATUS = f"""
UPDATE capability_operations
SET status = %s, updated_at = %s
WHERE operation_id = %s
RETURNING {_OPERATION_COLUMNS}
"""

_DELETE_OPERATION = """
DELETE FROM capability_operations
WHERE operation_id = %s
"""

_SELECT_PERMISSION = f"""
SELECT {_PERMISSION_COLUMNS}
FROM user_capability_permissions
WHERE user_id = %s AND capability_id = %s
"""

_SELECT_PERMISSION_ALLOWED = """
SELECT allowed
FROM user_capability_permissions
WHERE user_id = %s AND capability_id = %s
"""

_UPSERT_PERMISSION = f"""
INSERT INTO user_capability_permissions (
    user_id, capability_id, allowed, created_at, updated_at
)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, capability_id) DO UPDATE SET
    allowed = EXCLUDED.allowed,
    updated_at = EXCLUDED.updated_at
RETURNING {_PERMISSION_COLUMNS}
"""

_DELETE_PERMISSION = """
DELETE FROM user_capability_permissions
WHERE user_id = %s AND capability_id = %s
"""


class CapabilityPersistenceError(ApplicationError):
    """Capability 持久化初始化或读写失败。"""


class CapabilityPersistenceConflictError(ApplicationError):
    """Capability 持久化关系或唯一键发生冲突。"""


class CapabilityTaskNotFoundError(ApplicationError):
    """指定 Capability Task 不存在。"""


class CapabilityOperationNotFoundError(ApplicationError):
    """指定 Capability Operation 不存在。"""


@dataclass(frozen=True, slots=True)
class CapabilityOperationCreateResult:
    """Operation 幂等创建结果。"""

    operation: CapabilityOperation
    created: bool


async def _setup_schema(
    *,
    settings: Settings,
    statements: Sequence[str],
    business_name: str,
) -> None:
    """在同一事务内幂等创建一组表或索引。"""

    started_at = perf_counter()
    log_business_event(logger, f"{business_name}初始化开始")
    try:
        async with open_database_connection(settings) as connection:
            for statement in statements:
                await connection.execute(statement)
            await connection.commit()
    except Exception as error:
        log_business_event(
            logger,
            f"{business_name}初始化失败",
            level=logging.ERROR,
            error_code="CAPABILITY_PERSISTENCE_SETUP_FAILED",
            error_type=type(error).__name__,
            duration_ms=_elapsed_ms(started_at),
        )
        raise CapabilityPersistenceError(
            code="CAPABILITY_PERSISTENCE_SETUP_FAILED",
            message="Capability 持久化初始化失败",
            retryable=True,
        ) from error
    log_business_event(
        logger,
        f"{business_name}初始化完成",
        duration_ms=_elapsed_ms(started_at),
    )


class CapabilityTaskRepository:
    """保存 Capability Task 生命周期和最近执行投影。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """幂等创建 Task 表和查询索引。"""

        await _setup_schema(
            settings=self._settings,
            statements=(
                _CREATE_TASKS_TABLE,
                _CREATE_TASKS_LOOKUP_INDEX,
                _CREATE_TASKS_LAST_RUN_INDEX,
            ),
            business_name="Capability Task持久化",
        )

    async def add(self, task: CapabilityTask) -> CapabilityTask:
        """新增一个 Task，不修改 current Context。"""

        return await self._insert_task(task, set_current=False)

    async def create_and_set_current(
        self,
        task: CapabilityTask,
    ) -> CapabilityTask:
        """在一个事务内新增 active Task 并切换 current Context。"""

        if task.status != "active":
            raise ValueError("只有 active Task 可以成为 current Task")
        return await self._insert_task(task, set_current=True)

    async def get(self, task_id: UUID) -> CapabilityTask | None:
        """按 Task UUID 读取生命周期投影，不存在时返回空。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(_SELECT_TASK, (task_id,))
                row = await cursor.fetchone()
        except Exception as error:
            raise _persistence_error("Capability Task 读取失败", error) from error
        return _task_from_row(row) if row is not None else None

    async def update_projection(
        self,
        *,
        task_id: UUID,
        last_run_id: UUID | None,
        last_invocation_id: UUID | None,
        updated_at: datetime,
    ) -> CapabilityTask:
        """更新最近 Run 与 Invocation 投影，不改变 Task 生命周期。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _UPDATE_TASK_PROJECTION,
                    (last_run_id, last_invocation_id, updated_at, task_id),
                )
                row = await cursor.fetchone()
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability Task 更新失败", error) from error
        if row is None:
            raise _task_not_found()
        return _task_from_row(row)

    async def finish_and_clear_current(
        self,
        *,
        task_id: UUID,
        status: CapabilityTaskStatus,
        last_run_id: UUID | None,
        last_invocation_id: UUID | None,
        updated_at: datetime,
        ended_at: datetime,
    ) -> CapabilityTask:
        """锁定 Task，在同一事务写入终态并删除指向它的 Context。"""

        if status not in CAPABILITY_TERMINAL_TASK_STATUSES:
            raise ValueError("Task 终态只允许 completed、failed 或 cancelled")
        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability Task终态写入开始",
            task_id=task_id,
            status=status,
            run_id=last_run_id,
            invocation_id=last_invocation_id,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                locked = await connection.execute(
                    _SELECT_TASK_FOR_UPDATE,
                    (task_id,),
                )
                if await locked.fetchone() is None:
                    raise _task_not_found()
                await connection.execute(_DELETE_CONTEXT_BY_TASK, (task_id,))
                cursor = await connection.execute(
                    _FINISH_TASK,
                    (
                        status,
                        last_run_id,
                        last_invocation_id,
                        updated_at,
                        ended_at,
                        task_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise CapabilityPersistenceConflictError(
                        code="CAPABILITY_TASK_STATE_CONFLICT",
                        message="Capability Task 已不是 active 状态",
                        status_code=409,
                    )
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Capability Task终态写入拒绝",
                task_id=task_id,
                status=status,
                run_id=last_run_id,
                invocation_id=last_invocation_id,
                error_code=error.code,
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            raise _persistence_error("Capability Task 终态写入失败", error) from error
        result = _task_from_row(row)
        log_business_event(
            logger,
            "Capability Task终态写入完成",
            task_id=task_id,
            status=result.status,
            run_id=result.last_run_id,
            invocation_id=result.last_invocation_id,
            duration_ms=_elapsed_ms(started_at),
        )
        return result

    async def delete(self, task_id: UUID) -> None:
        """幂等删除没有 Context 或 Operation 引用的 Task。"""

        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_DELETE_TASK, (task_id,))
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability Task 删除失败", error) from error

    async def _insert_task(
        self,
        task: CapabilityTask,
        *,
        set_current: bool,
    ) -> CapabilityTask:
        """新增 Task，并可选在同一事务更新 current Context。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability Task创建开始",
            task_id=task.task_id,
            session_id=task.session_id,
            capability_id=task.capability_id,
            status=task.status,
            set_current=set_current,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                if set_current:
                    lock_cursor = await connection.execute(
                        _LOCK_SESSION,
                        (task.session_id,),
                    )
                    if await lock_cursor.fetchone() is None:
                        raise CapabilityPersistenceConflictError(
                            code="CAPABILITY_TASK_SESSION_CONFLICT",
                            message="Capability Task 所属 Session 不存在",
                            status_code=409,
                        )
                cursor = await connection.execute(
                    _INSERT_TASK,
                    _task_params(task),
                )
                row = await cursor.fetchone()
                if set_current:
                    context_cursor = await connection.execute(
                        _UPSERT_CONTEXT,
                        (
                            task.task_id,
                            task.session_id,
                            task.capability_id,
                            task.session_id,
                            task.capability_id,
                            task.task_id,
                            task.updated_at,
                        ),
                    )
                    if await context_cursor.fetchone() is None:
                        raise CapabilityPersistenceConflictError(
                            code="CAPABILITY_TASK_CONTEXT_CONFLICT",
                            message="current Task 关联关系非法",
                            status_code=409,
                        )
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Capability Task创建拒绝",
                task_id=task.task_id,
                session_id=task.session_id,
                capability_id=task.capability_id,
                status=task.status,
                set_current=set_current,
                error_code=error.code,
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            raise _persistence_error("Capability Task 创建失败", error) from error
        result = _task_from_row(row)
        log_business_event(
            logger,
            "Capability Task创建完成",
            task_id=result.task_id,
            session_id=result.session_id,
            capability_id=result.capability_id,
            status=result.status,
            set_current=set_current,
            duration_ms=_elapsed_ms(started_at),
        )
        return result


class CapabilityTaskContextRepository:
    """保存每个 Session 与 Capability 的唯一 current Task 指针。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """幂等创建 current Task Context 表。"""

        await _setup_schema(
            settings=self._settings,
            statements=(_CREATE_CONTEXTS_TABLE,),
            business_name="Capability Task Context持久化",
        )

    async def get(
        self,
        *,
        session_id: UUID,
        capability_id: str,
    ) -> CapabilityTaskContext | None:
        """读取当前 Task 指针；无记录表示当前没有 Task。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_CONTEXT,
                    (session_id, capability_id),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise _persistence_error("Capability Task Context 读取失败", error) from error
        return _context_from_row(row) if row is not None else None

    async def set_current(
        self,
        context: CapabilityTaskContext,
    ) -> CapabilityTaskContext:
        """仅把同 Session、同 Capability 的 active Task 设置为 current。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability Task Context写入开始",
            task_id=context.current_task_id,
            session_id=context.session_id,
            capability_id=context.capability_id,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                lock_cursor = await connection.execute(
                    _LOCK_SESSION,
                    (context.session_id,),
                )
                if await lock_cursor.fetchone() is None:
                    raise CapabilityPersistenceConflictError(
                        code="CAPABILITY_TASK_SESSION_CONFLICT",
                        message="Capability Task 所属 Session 不存在",
                        status_code=409,
                    )
                cursor = await connection.execute(
                    _UPSERT_CONTEXT,
                    (
                        context.current_task_id,
                        context.session_id,
                        context.capability_id,
                        context.session_id,
                        context.capability_id,
                        context.current_task_id,
                        context.updated_at,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise CapabilityPersistenceConflictError(
                        code="CAPABILITY_TASK_CONTEXT_CONFLICT",
                        message="current Task 必须是同 Session、同 Capability 的 active Task",
                        status_code=409,
                    )
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Capability Task Context写入拒绝",
                task_id=context.current_task_id,
                session_id=context.session_id,
                capability_id=context.capability_id,
                error_code=error.code,
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            raise _persistence_error("Capability Task Context 写入失败", error) from error
        result = _context_from_row(row)
        log_business_event(
            logger,
            "Capability Task Context写入完成",
            task_id=result.current_task_id,
            session_id=result.session_id,
            capability_id=result.capability_id,
            duration_ms=_elapsed_ms(started_at),
        )
        return result

    async def clear(self, *, session_id: UUID, capability_id: str) -> None:
        """幂等删除指定 current Task Context。"""

        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(
                    _DELETE_CONTEXT,
                    (session_id, capability_id),
                )
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability Task Context 删除失败", error) from error


class CapabilityOperationRepository:
    """保存 Operation 幂等业务键和最小状态，不保存业务 payload。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """幂等创建 Operation 表和查询索引。"""

        await _setup_schema(
            settings=self._settings,
            statements=(
                _CREATE_OPERATIONS_TABLE,
                _CREATE_OPERATIONS_TASK_INDEX,
                _CREATE_OPERATIONS_INVOCATION_INDEX,
            ),
            business_name="Capability Operation持久化",
        )

    async def create_or_get(
        self,
        operation: CapabilityOperation,
    ) -> CapabilityOperationCreateResult:
        """按业务唯一键幂等创建 pending Operation。"""

        if operation.status != "pending":
            raise ValueError("新 Operation 必须是 pending 状态")
        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability Operation登记开始",
            operation_id=operation.operation_id,
            run_id=operation.run_id,
            invocation_id=operation.invocation_id,
            task_id=operation.task_id,
            capability_id=operation.capability_id,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _INSERT_OPERATION,
                    (
                        *_operation_params(operation),
                        operation.run_id,
                        operation.task_id,
                        operation.capability_id,
                    ),
                )
                row = await cursor.fetchone()
                created = row is not None
                if row is None:
                    business_cursor = await connection.execute(
                        _SELECT_OPERATION_BY_BUSINESS_KEY,
                        (
                            operation.run_id,
                            operation.invocation_id,
                            operation.operation_key,
                        ),
                    )
                    business_row = await business_cursor.fetchone()
                    idempotency_cursor = await connection.execute(
                        _SELECT_OPERATION_BY_IDEMPOTENCY_KEY,
                        (operation.idempotency_key,),
                    )
                    idempotency_row = await idempotency_cursor.fetchone()
                    if (
                        business_row is None
                        or idempotency_row is None
                        or business_row["operation_id"]
                        != idempotency_row["operation_id"]
                        or str(business_row["idempotency_key"])
                        != operation.idempotency_key
                        or UUID(str(business_row["task_id"]))
                        != operation.task_id
                        or str(business_row["capability_id"])
                        != operation.capability_id
                    ):
                        raise CapabilityPersistenceConflictError(
                            code="CAPABILITY_OPERATION_CONFLICT",
                            message="Operation 唯一键与既有记录冲突",
                            status_code=409,
                        )
                    row = business_row
                await connection.commit()
        except ApplicationError as error:
            log_business_event(
                logger,
                "Capability Operation登记拒绝",
                operation_id=operation.operation_id,
                run_id=operation.run_id,
                invocation_id=operation.invocation_id,
                task_id=operation.task_id,
                capability_id=operation.capability_id,
                error_code=error.code,
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            raise _persistence_error("Capability Operation 登记失败", error) from error
        result = CapabilityOperationCreateResult(
            operation=_operation_from_row(row),
            created=created,
        )
        log_business_event(
            logger,
            "Capability Operation登记完成",
            operation_id=result.operation.operation_id,
            run_id=result.operation.run_id,
            invocation_id=result.operation.invocation_id,
            task_id=result.operation.task_id,
            capability_id=result.operation.capability_id,
            status=result.operation.status,
            created=created,
            duration_ms=_elapsed_ms(started_at),
        )
        return result

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> CapabilityOperation | None:
        """按 Runtime 生成的稳定幂等键读取 Operation。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_OPERATION_BY_IDEMPOTENCY_KEY,
                    (idempotency_key,),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise _persistence_error("Capability Operation 读取失败", error) from error
        return _operation_from_row(row) if row is not None else None

    async def set_status(
        self,
        *,
        operation_id: UUID,
        status: CapabilityOperationStatus,
        updated_at: datetime,
    ) -> CapabilityOperation:
        """更新 Operation 最小账本状态，不写外部响应或异常详情。"""

        as_operation_status(status)
        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability Operation状态写入开始",
            operation_id=operation_id,
            status=status,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _UPDATE_OPERATION_STATUS,
                    (status, updated_at, operation_id),
                )
                row = await cursor.fetchone()
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability Operation 状态更新失败", error) from error
        if row is None:
            raise CapabilityOperationNotFoundError(
                code="CAPABILITY_OPERATION_NOT_FOUND",
                message="Capability Operation 不存在",
                status_code=404,
            )
        result = _operation_from_row(row)
        log_business_event(
            logger,
            "Capability Operation状态写入完成",
            operation_id=result.operation_id,
            run_id=result.run_id,
            invocation_id=result.invocation_id,
            task_id=result.task_id,
            capability_id=result.capability_id,
            status=result.status,
            duration_ms=_elapsed_ms(started_at),
        )
        return result

    async def delete(self, operation_id: UUID) -> None:
        """幂等删除指定 Operation。"""

        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_DELETE_OPERATION, (operation_id,))
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability Operation 删除失败", error) from error


class UserCapabilityPermissionRepository:
    """保存固定用户对 Capability 的显式允许或拒绝记录。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """幂等创建最小用户级权限表和查询索引。"""

        await _setup_schema(
            settings=self._settings,
            statements=(
                _CREATE_PERMISSIONS_TABLE,
                _CREATE_PERMISSIONS_LOOKUP_INDEX,
            ),
            business_name="Capability权限持久化",
        )

    async def get_allowed(
        self,
        *,
        user_id: str,
        capability_id: str,
    ) -> bool | None:
        """区分无记录、显式拒绝和显式允许，不应用业务默认值。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_PERMISSION_ALLOWED,
                    (user_id, capability_id),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise _persistence_error("Capability 权限读取失败", error) from error
        return None if row is None else bool(row["allowed"])

    async def get(
        self,
        *,
        user_id: str,
        capability_id: str,
    ) -> UserCapabilityPermission | None:
        """读取完整权限投影；无记录返回空。"""

        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_PERMISSION,
                    (user_id, capability_id),
                )
                row = await cursor.fetchone()
        except Exception as error:
            raise _persistence_error("Capability 权限读取失败", error) from error
        return _permission_from_row(row) if row is not None else None

    async def set_allowed(
        self,
        permission: UserCapabilityPermission,
    ) -> UserCapabilityPermission:
        """原子新增或覆盖显式 allow/deny 权限。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability权限写入开始",
            capability_id=permission.capability_id,
            allowed=permission.allowed,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _UPSERT_PERMISSION,
                    (
                        permission.user_id,
                        permission.capability_id,
                        permission.allowed,
                        permission.created_at,
                        permission.updated_at,
                    ),
                )
                row = await cursor.fetchone()
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability 权限写入失败", error) from error
        result = _permission_from_row(row)
        log_business_event(
            logger,
            "Capability权限写入完成",
            capability_id=result.capability_id,
            allowed=result.allowed,
            duration_ms=_elapsed_ms(started_at),
        )
        return result

    async def revoke(self, *, user_id: str, capability_id: str) -> None:
        """幂等删除权限行，使读取恢复为无记录。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Capability权限撤销开始",
            capability_id=capability_id,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(
                    _DELETE_PERMISSION,
                    (user_id, capability_id),
                )
                await connection.commit()
        except Exception as error:
            raise _persistence_error("Capability 权限撤销失败", error) from error
        log_business_event(
            logger,
            "Capability权限撤销完成",
            capability_id=capability_id,
            duration_ms=_elapsed_ms(started_at),
        )


def _task_params(task: CapabilityTask) -> tuple[object, ...]:
    return (
        task.task_id,
        task.session_id,
        task.capability_id,
        task.state_schema_version,
        task.status,
        task.last_run_id,
        task.last_invocation_id,
        task.created_at,
        task.updated_at,
        task.ended_at,
    )


def _operation_params(operation: CapabilityOperation) -> tuple[object, ...]:
    return (
        operation.operation_id,
        operation.run_id,
        operation.invocation_id,
        operation.task_id,
        operation.capability_id,
        operation.operation_key,
        operation.idempotency_key,
        operation.status,
        operation.created_at,
        operation.updated_at,
    )


def _task_from_row(row: Mapping[str, Any]) -> CapabilityTask:
    return CapabilityTask(
        task_id=UUID(str(row["task_id"])),
        session_id=UUID(str(row["session_id"])),
        capability_id=str(row["capability_id"]),
        state_schema_version=str(row["state_schema_version"]),
        status=as_task_status(row["status"]),
        last_run_id=(
            UUID(str(row["last_run_id"]))
            if row["last_run_id"] is not None
            else None
        ),
        last_invocation_id=(
            UUID(str(row["last_invocation_id"]))
            if row["last_invocation_id"] is not None
            else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        ended_at=row["ended_at"],
    )


def _context_from_row(row: Mapping[str, Any]) -> CapabilityTaskContext:
    return CapabilityTaskContext(
        session_id=UUID(str(row["session_id"])),
        capability_id=str(row["capability_id"]),
        current_task_id=UUID(str(row["current_task_id"])),
        updated_at=row["updated_at"],
    )


def _operation_from_row(row: Mapping[str, Any]) -> CapabilityOperation:
    return CapabilityOperation(
        operation_id=UUID(str(row["operation_id"])),
        run_id=UUID(str(row["run_id"])),
        invocation_id=UUID(str(row["invocation_id"])),
        task_id=UUID(str(row["task_id"])),
        capability_id=str(row["capability_id"]),
        operation_key=str(row["operation_key"]),
        idempotency_key=str(row["idempotency_key"]),
        status=as_operation_status(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _permission_from_row(row: Mapping[str, Any]) -> UserCapabilityPermission:
    return UserCapabilityPermission(
        user_id=str(row["user_id"]),
        capability_id=str(row["capability_id"]),
        allowed=bool(row["allowed"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _task_not_found() -> CapabilityTaskNotFoundError:
    return CapabilityTaskNotFoundError(
        code="CAPABILITY_TASK_NOT_FOUND",
        message="Capability Task 不存在",
        status_code=404,
    )


def _persistence_error(
    message: str,
    error: Exception,
) -> CapabilityPersistenceError:
    log_business_event(
        logger,
        message,
        level=logging.ERROR,
        error_code="CAPABILITY_PERSISTENCE_FAILED",
        error_type=type(error).__name__,
    )
    return CapabilityPersistenceError(
        code="CAPABILITY_PERSISTENCE_FAILED",
        message=message,
        retryable=True,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)
