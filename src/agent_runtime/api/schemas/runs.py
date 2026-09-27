"""Stage 2.5 异步 Run 产品接口 Schema。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegenerateRunRequest(BaseModel):
    """重新生成请求的幂等标识。"""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(
        description=(
            "客户端为本次 Regenerate 生成的全局唯一 UUID；相同请求"
            "重试必须复用该值，同一值对应不同请求时返回 409。"
        )
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
