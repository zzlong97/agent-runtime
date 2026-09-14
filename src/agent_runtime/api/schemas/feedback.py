"""Stage 2 消息反馈产品 Schema。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    """保存、替换或取消消息反馈的请求。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["like", "dislike", "cancel"] = Field(
        description=(
            "要执行的最终反馈动作；like 保存喜欢，dislike 保存不喜欢，cancel "
            "幂等删除当前反馈；Stage 2 不接受其他值。"
        )
    )


class FeedbackResponse(BaseModel):
    """反馈操作完成后的最终产品状态。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    message_id: UUID = Field(
        description=(
            "已完成反馈操作的公共 AIMessage 稳定 UUID；目标必须仍位于固定本地"
            "用户的当前活动分支。"
        )
    )
    feedback: Literal["like", "dislike"] | None = Field(
        description=(
            "操作后的最终反馈值；like 或 dislike 表示已保存，cancel 成功后为 null。"
        )
    )
