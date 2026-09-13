"""Session 创建、列表与改名产品服务。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.persistence.parent_state import PostgresParentStateStore
from agent_runtime.sessions.models import Session, SessionCursor, SessionPage
from agent_runtime.sessions.pagination import decode_cursor, encode_cursor
from agent_runtime.sessions.repository import PostgresSessionRepository


@dataclass(frozen=True, slots=True)
class SessionStart:
    """交给 Parent 执行边界的稳定标识与已持久化值。"""

    session: Session
    human_message: HumanMessage


RouteHandler = Callable[[SessionStart], Awaitable[None]]


class SessionService:
    """创建 Session，并提供固定本地用户的产品查询能力。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_repository: PostgresSessionRepository | None = None,
        parent_state_store: PostgresParentStateStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_repository = session_repository or PostgresSessionRepository(
            self._settings
        )
        self._parent_state_store = parent_state_store or PostgresParentStateStore(
            self._settings
        )

    async def start_new_session(
        self,
        *,
        content: str,
        route: RouteHandler,
    ) -> SessionStart:
        """持久化新 Session 和首条 HumanMessage，再调用 Router。"""

        started = await self.prepare_new_session(content=content)
        await route(started)
        return started

    async def prepare_new_session(
        self,
        *,
        content: str,
    ) -> SessionStart:
        """在返回 StreamingResponse 前保存新 Session 和首条 HumanMessage。"""

        now = datetime.now(UTC)
        session = Session(
            session_id=uuid4(),
            user_id=self._settings.local_user_id,
            title=content,
            created_at=now,
            updated_at=now,
        )
        human_message = HumanMessage(content=content, id=str(uuid4()))
        started = SessionStart(
            session=session,
            human_message=human_message,
        )

        await self._session_repository.add(session)
        await self._parent_state_store.store_initial_human_message(
            session.session_id,
            human_message,
        )
        return started

    async def list_sessions(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> SessionPage:
        """解析不透明游标并查询固定本地用户的一页 Session。"""

        boundary = decode_cursor(cursor) if cursor is not None else None
        items, has_more = await self._session_repository.list_page(
            user_id=self._settings.local_user_id,
            cursor=boundary,
            limit=limit,
        )
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = encode_cursor(
                SessionCursor(
                    updated_at=last.updated_at,
                    session_id=last.session_id,
                )
            )
        return SessionPage(items=tuple(items), next_cursor=next_cursor)

    async def rename_session(
        self,
        *,
        session_id: UUID,
        title: str,
    ) -> Session:
        """按固定本地用户更新标题与更新时间。"""

        return await self._session_repository.rename(
            session_id=session_id,
            user_id=self._settings.local_user_id,
            title=title,
            updated_at=datetime.now(UTC),
        )
