"""Stage 2.5 异步 Run 产品接口 Schema。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class RegenerateRunRequest(BaseModel):
    """重新生成请求的幂等标识。"""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(
        description=(
            "客户端为本次 Regenerate 生成的全局唯一 UUID；相同请求"
            "重试必须复用该值，同一值对应不同请求时返回 409。"
        )
    )


class ResumeRunRequest(BaseModel):
    """同一持久 Run 的 Interrupt 恢复请求。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: UUID = Field(
        description=(
            "要消费的待处理 Interrupt UUID；必须准确属于路径中的 Run，"
            "已恢复、已取消或不匹配时拒绝。"
        )
    )
    request_id: UUID = Field(
        description=(
            "客户端为本次 Resume 生成的全局唯一 UUID；相同内容重试必须复用"
            "该值，不同内容复用时返回 409。"
        )
    )
    resume_payload: dict[str, JsonValue] = Field(
        description=(
            "提交给当前 Interrupt 的恢复输入对象；Stage 2.5 仅传给同一 Run 的 "
            "Command(resume=...)，不会创建 HumanMessage 或新 Run。"
        )
    )


class PendingInterruptResponse(BaseModel):
    """页面刷新后可安全恢复的待处理 Interrupt 摘要。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    interrupt_id: UUID = Field(
        description="当前 Run 唯一待处理 Interrupt 的服务端 UUID。"
    )
    interrupt_payload: dict[str, JsonValue] = Field(
        description=(
            "需要页面展示的固定公开中断提示对象；不得包含 Prompt、Checkpoint、"
            "私有 State 或工具原始参数。"
        )
    )
    created_at: datetime = Field(
        description="该待处理 Interrupt 首次持久化的带时区时间。"
    )


class RunSummaryResponse(BaseModel):
    """不含恢复输入和内部状态的公开 Run 摘要。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    run_id: UUID = Field(
        description=(
            "服务端为持久执行分配的 Run UUID；用于后续事件连接"
            "与显式运行操作。"
        )
    )
    session_id: UUID = Field(
        description=(
            "Run 归属的 Session UUID；只返回固定本地用户拥有的"
            "Session，不允许客户端指定 user_id。"
        )
    )
    response_message_id: UUID = Field(
        description=(
            "本 Run 固定的公共 AIMessage UUID；初次执行和后续恢复"
            "必须复用该值。"
        )
    )
    status: Literal[
        "queued",
        "running",
        "recovering",
        "interrupted",
        "cancel_requested",
        "completed",
        "failed",
        "cancelled",
    ] = Field(
        description=(
            "PostgreSQL 中的 Run 权威状态；Stage 2.5 只允许已确认的"
            "五个活动状态和三个终态。"
        )
    )
    recovery_attempts: int = Field(
        ge=0,
        le=3,
        description=(
            "PostgreSQL 已记录的崩溃恢复接管次数；初次执行为 0，Stage 2.5 "
            "最多允许 3 次，用于页面展示公开恢复进度。"
        ),
    )


class ActiveRunResponse(RunSummaryResponse):
    """页面恢复所需的活动 Run 与可选待处理 Interrupt。"""

    pending_interrupt: PendingInterruptResponse | None = Field(
        default=None,
        description=(
            "Run 为 interrupted 时的唯一待处理提示；其他活动状态为空，且响应中"
            "省略该字段。"
        ),
    )
