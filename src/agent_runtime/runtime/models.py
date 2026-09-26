"""持久 Run 实体、提交参数与状态机规则。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

RunType = Literal["normal", "regenerate"]
RunStatus = Literal[
    "queued",
    "running",
    "recovering",
    "interrupted",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
]
JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)

RUN_TYPES = frozenset({"normal", "regenerate"})
RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "recovering",
        "interrupted",
        "cancel_requested",
        "completed",
        "failed",
        "cancelled",
    }
)
ACTIVE_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "recovering",
        "interrupted",
        "cancel_requested",
    }
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset(
        {
            "completed",
            "failed",
            "interrupted",
            "cancel_requested",
            "recovering",
        }
    ),
    "recovering": frozenset(
        {"running", "failed", "interrupted", "cancel_requested"}
    ),
    "interrupted": frozenset({"running", "cancelled"}),
    "cancel_requested": frozenset({"cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """创建 queued Run 所需的已分配标识和最小恢复输入。"""

    run_id: UUID
    request_id: UUID
    session_id: UUID
    thread_id: str
    parent_run_id: UUID | None
    run_type: RunType
    input_message_id: UUID
    response_message_id: UUID
    start_checkpoint_id: str | None
    input_payload: dict[str, JsonValue]
    request_fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        """拒绝超出 Stage 2.5 已确认范围的 Run 类型与非法指纹。"""

        if self.run_type not in RUN_TYPES:
            raise ValueError("Run 类型只允许 normal 或 regenerate")
        if not self.thread_id.strip():
            raise ValueError("Run thread_id 不能为空")
        if len(self.request_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.request_fingerprint
        ):
            raise ValueError("Run 请求指纹必须是小写 SHA-256 十六进制字符串")


@dataclass(frozen=True, slots=True)
class Run:
    """PostgreSQL 中作为执行生命周期权威源的 Run。"""

    run_id: UUID
    request_id: UUID
    session_id: UUID
    thread_id: str
    parent_run_id: UUID | None
    run_type: RunType
    input_message_id: UUID
    response_message_id: UUID
    start_checkpoint_id: str | None
    input_payload: dict[str, JsonValue] | None
    request_fingerprint: str
    status: RunStatus
    recovery_attempts: int
    seq_high_watermark: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunCreateResult:
    """幂等创建返回的权威 Run 及本次是否新增。"""

    run: Run
    created: bool


def can_transition_run(current_status: str, target_status: str) -> bool:
    """判断状态变化是否符合已确认状态机；相同状态视为幂等读取。"""

    if current_status not in RUN_STATUSES or target_status not in RUN_STATUSES:
        return False
    if current_status == target_status:
        return True
    return target_status in _RUN_TRANSITIONS[current_status]
