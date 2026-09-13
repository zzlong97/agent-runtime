"""Stage 2 Session 产品接口 Schema。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SessionListQuery(BaseModel):
    """Session 列表的游标分页查询参数。"""

    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(
        default=None,
        description=(
            "定位下一页的后端不透明游标；省略或传 null 时读取最新一页，"
            "Stage 2 客户端不得解析、修改或自行拼装。"
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "本页最多返回的 Session 数量；允许 1～100，默认 20，"
            "Stage 2 使用游标分页。"
        ),
    )


class SessionListItem(BaseModel):
    """Session 列表中面向客户端的最小产品字段。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    session_id: UUID = Field(
        description="Session 的服务端稳定 UUID，用于后续聊天和 Session 产品操作。"
    )
    title: str = Field(
        description=(
            "Session 当前标题；Stage 2 S2-01 仅用于列表展示，"
            "本接口不修改标题。"
        )
    )
    created_at: datetime = Field(
        description="Session 创建时间，返回带时区的 ISO 8601 时间。"
    )
    updated_at: datetime = Field(
        description=(
            "Session 最近更新时间，作为列表首要降序排序键，"
            "并返回带时区的 ISO 8601 时间。"
        )
    )


class SessionListResponse(BaseModel):
    """Session 列表的一页产品响应。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    items: list[SessionListItem] = Field(
        description=(
            "固定本地用户当前页的 Session 项，按 updated_at DESC、"
            "session_id DESC 排列。"
        )
    )
    next_cursor: str | None = Field(
        description=(
            "下一页的后端不透明游标；没有更多 Session 时为 null，"
            "客户端不得解析或自行拼装。"
        )
    )
