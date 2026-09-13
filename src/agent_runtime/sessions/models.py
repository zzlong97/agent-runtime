"""Session 元数据与列表分页领域对象。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Session:
    """Stage 1 起持久化的最小 Session 实体。"""

    session_id: UUID
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionCursor:
    """Session 列表下一页使用的复合排序边界。"""

    updated_at: datetime
    session_id: UUID


@dataclass(frozen=True, slots=True)
class SessionPage:
    """固定本地用户的一页 Session 产品数据。"""

    items: tuple[Session, ...]
    next_cursor: str | None
