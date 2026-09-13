"""Stage 2 Session 产品接口 Schema。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class SessionRenameRequest(BaseModel):
    """Session 改名请求，用户身份始终由服务端控制。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "新的 Session 标题；服务端先去除首尾空白，再要求长度为 1～100 "
            "个字符；Stage 2 S2-02 不调用模型生成标题。"
        ),
    )

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> Any:
        """先清理字符串首尾空白，再以中文错误拒绝非法长度。"""

        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Session 标题去除首尾空白后不能为空")
        if len(normalized) > 100:
            raise ValueError("Session 标题去除首尾空白后不能超过 100 个字符")
        return normalized


class SessionRenameResponse(BaseModel):
    """Session 改名成功后的产品响应。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    session_id: UUID = Field(
        description=(
            "已改名 Session 的服务端稳定 UUID；Stage 2 S2-02 不允许客户端"
            "修改该值。"
        )
    )
    title: str = Field(
        description="去除首尾空白后已持久化的新标题，长度为 1～100 个字符。"
    )
    created_at: datetime = Field(
        description=(
            "Session 原始创建时间；改名不会修改该值，返回带时区的 "
            "ISO 8601 时间。"
        )
    )
    updated_at: datetime = Field(
        description=(
            "本次改名的持久化时间；用于 Session 列表首要降序排序，"
            "返回带时区的 ISO 8601 时间。"
        )
    )
