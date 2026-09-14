"""Stage 2 当前活动分支历史消息产品 Schema。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MessageHistoryQuery(BaseModel):
    """历史消息的 before 分页查询参数。"""

    model_config = ConfigDict(extra="forbid")

    before: UUID | None = Field(
        default=None,
        description=(
            "上一页首条消息的服务端稳定 UUID；省略或传 null 时读取当前活动 "
            "Parent 分支的最新一页，且该 UUID 必须存在于当前活动历史中。"
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description=(
            "本页最多返回的产品消息数量；允许 1～100，默认 50，"
            "从 before 之前向更早消息截取。"
        ),
    )


class ProductMessageResponse(BaseModel):
    """从 Parent 权威消息转换的前端产品消息。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    message_id: UUID = Field(
        description="公共消息的服务端稳定 UUID，用于历史分页和后续消息操作。"
    )
    role: Literal["user", "assistant"] = Field(
        description=(
            "产品消息角色；user 表示 HumanMessage，assistant 表示 AIMessage，"
            "不暴露内部 System 或 Tool 消息。"
        )
    )
    content: str = Field(
        description="公共消息的完整文本正文，不包含内部状态或 checkpoint 元数据。"
    )
    runtime_status: Literal[
        "completed",
        "unsupported",
        "incomplete",
        "stopped",
    ] | None = Field(
        description=(
            "AIMessage 的运行终态，只允许 completed、unsupported、incomplete 或 "
            "stopped；HumanMessage 为 null，缺少 Stage 2 元数据的旧 AIMessage 按 "
            "completed 返回。"
        )
    )
    capability_id: Literal["general_chat", "en_to_zh"] | None = Field(
        description=(
            "生成 AIMessage 的能力标识，只允许 general_chat、en_to_zh 或 null；"
            "HumanMessage 及缺少该元数据的旧消息为 null。"
        )
    )
    feedback: Literal["like", "dislike"] | None = Field(
        description=(
            "固定本地用户对当前活动 AIMessage 的最终反馈，只允许 like、dislike "
            "或尚无反馈时的 null；S2-05 尚未开放反馈写入。"
        )
    )


class MessageHistoryResponse(BaseModel):
    """当前活动 Parent 分支的一页历史消息响应。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    items: list[ProductMessageResponse] = Field(
        description=(
            "当前活动 Parent 分支的一页产品消息，过滤内部消息后按对话时间正序返回。"
        )
    )
    next_before: UUID | None = Field(
        description=(
            "更早一页应作为 before 传入的消息 UUID；没有更早产品消息时为 null。"
        )
    )
