"""Stage 3 Capability 持久化领域对象。"""

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal, cast
from uuid import UUID

CapabilityTaskStatus = Literal["active", "completed", "failed", "cancelled"]
CapabilityOperationStatus = Literal["pending", "succeeded", "failed"]

CAPABILITY_TASK_STATUSES = frozenset(
    {"active", "completed", "failed", "cancelled"}
)
CAPABILITY_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled"}
)
CAPABILITY_OPERATION_STATUSES = frozenset({"pending", "succeeded", "failed"})

_CAPABILITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_capability_id(value: str) -> None:
    """按 Manifest 的公开 ID 格式验证持久化关联键。"""

    if _CAPABILITY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Capability ID 格式非法")


@dataclass(frozen=True, slots=True)
class CapabilityTask:
    """Capability 长期任务的生命周期与最近执行投影。"""

    task_id: UUID
    session_id: UUID
    capability_id: str
    state_schema_version: str
    status: CapabilityTaskStatus
    last_run_id: UUID | None
    last_invocation_id: UUID | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None

    def __post_init__(self) -> None:
        """拒绝数据库不应接受的非法 Task 投影。"""

        _validate_capability_id(self.capability_id)
        if not self.state_schema_version.strip():
            raise ValueError("State Schema 版本不能为空")
        if len(self.state_schema_version) > 64:
            raise ValueError("State Schema 版本最长 64 个字符")
        if self.status not in CAPABILITY_TASK_STATUSES:
            raise ValueError("Task 状态非法")
        if self.status == "active" and self.ended_at is not None:
            raise ValueError("active Task 的 ended_at 必须为空")
        if self.status != "active" and self.ended_at is None:
            raise ValueError("终态 Task 的 ended_at 不能为空")


@dataclass(frozen=True, slots=True)
class CapabilityTaskContext:
    """单个 Session 与 Capability 的唯一 current Task 指针。"""

    session_id: UUID
    capability_id: str
    current_task_id: UUID
    updated_at: datetime

    def __post_init__(self) -> None:
        """验证 Context 使用合法 Capability ID。"""

        _validate_capability_id(self.capability_id)


@dataclass(frozen=True, slots=True)
class CapabilityOperation:
    """外部副作用的最小幂等账本投影，不保存业务响应。"""

    operation_id: UUID
    run_id: UUID
    invocation_id: UUID
    task_id: UUID
    capability_id: str
    operation_key: str
    idempotency_key: str
    status: CapabilityOperationStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """验证 Operation 的公开关联键、业务键与状态。"""

        _validate_capability_id(self.capability_id)
        if not self.operation_key.strip():
            raise ValueError("Operation Key 不能为空")
        if _SHA256_PATTERN.fullmatch(self.idempotency_key) is None:
            raise ValueError("Operation 幂等键必须是小写 SHA-256")
        if self.status not in CAPABILITY_OPERATION_STATUSES:
            raise ValueError("Operation 状态非法")


@dataclass(frozen=True, slots=True)
class UserCapabilityPermission:
    """固定用户对单个 Capability 的显式允许或拒绝记录。"""

    user_id: str
    capability_id: str
    allowed: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """拒绝空用户、非法 Capability ID 与非布尔授权值。"""

        if not self.user_id.strip():
            raise ValueError("用户 ID 不能为空")
        _validate_capability_id(self.capability_id)
        if type(self.allowed) is not bool:
            raise TypeError("allowed 必须是布尔值")


def as_task_status(value: object) -> CapabilityTaskStatus:
    """把数据库值收窄为已校验的 Task 状态。"""

    if value not in CAPABILITY_TASK_STATUSES:
        raise ValueError("数据库包含非法 Task 状态")
    return cast(CapabilityTaskStatus, value)


def as_operation_status(value: object) -> CapabilityOperationStatus:
    """把数据库值收窄为已校验的 Operation 状态。"""

    if value not in CAPABILITY_OPERATION_STATUSES:
        raise ValueError("数据库包含非法 Operation 状态")
    return cast(CapabilityOperationStatus, value)
