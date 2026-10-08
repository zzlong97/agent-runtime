"""RuntimeEvent 领域对象与 Run 状态事件提交结果。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel

from agent_runtime.runtime.models import JsonValue, Run

EventVisibility: TypeAlias = Literal["public", "internal"]
EventDurability: TypeAlias = Literal["durable", "transient"]


@dataclass(frozen=True, slots=True)
class RuntimeEventDraft:
    """由执行器提交给 Run Sequencer 的强类型事件草稿。"""

    event_type: str
    source: str
    visibility: EventVisibility
    payload: BaseModel
    schema_version: int
    durability: EventDurability
    created_at: datetime

    def __post_init__(self) -> None:
        """在分配序号前拒绝缺少稳定标识或类型声明的草稿。"""

        if not self.event_type.strip():
            raise ValueError("RuntimeEvent 事件类型不能为空")
        if not self.source.strip():
            raise ValueError("RuntimeEvent 来源不能为空")
        if self.visibility not in {"public", "internal"}:
            raise ValueError("RuntimeEvent 可见性只允许 public 或 internal")
        if self.durability not in {"durable", "transient"}:
            raise ValueError("RuntimeEvent 耐久性只允许 durable 或 transient")
        if self.schema_version != 1:
            raise ValueError("Stage 2.5 RuntimeEvent Schema 版本只允许 1")
        if not isinstance(self.payload, BaseModel):
            raise TypeError("RuntimeEvent payload 必须是 Pydantic Schema 对象")


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """已分配 UUIDv4 与单 Run 单调序号的运行时事件。"""

    event_id: UUID
    run_id: UUID
    seq: int
    event_type: str
    source: str
    visibility: EventVisibility
    payload: dict[str, JsonValue]
    schema_version: int
    durability: EventDurability
    created_at: datetime

    def __post_init__(self) -> None:
        """保证事件标识、序号和版本满足持久协议下限。"""

        if self.event_id.version != 4:
            raise ValueError("RuntimeEvent event_id 必须是 UUIDv4")
        if self.seq < 1:
            raise ValueError("RuntimeEvent seq 必须大于等于 1")
        if self.visibility not in {"public", "internal"}:
            raise ValueError("RuntimeEvent 可见性只允许 public 或 internal")
        if self.durability not in {"durable", "transient"}:
            raise ValueError("RuntimeEvent 耐久性只允许 durable 或 transient")
        if self.schema_version != 1:
            raise ValueError("Stage 2.5 RuntimeEvent Schema 版本只允许 1")


@dataclass(frozen=True, slots=True)
class SequenceBlock:
    """从 PostgreSQL Run 高水位原子预留的闭区间序号块。"""

    first: int
    last: int

    def __post_init__(self) -> None:
        """拒绝空区间和非正序号。"""

        if self.first < 1 or self.last < self.first:
            raise ValueError("RuntimeEvent 序号块必须是有效的正整数闭区间")


@dataclass(frozen=True, slots=True)
class RunEventCommit:
    """同一事务内完成的 Run 状态与持久事件提交结果。"""

    run: Run
    event: RuntimeEvent | None
    changed: bool


@dataclass(frozen=True, slots=True)
class CapabilityInvocationEventCommit:
    """Invocation started 的新建或崩溃恢复复用结果。"""

    event: RuntimeEvent
    recovered: bool
