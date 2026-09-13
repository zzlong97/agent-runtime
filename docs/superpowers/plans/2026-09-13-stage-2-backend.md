# Stage 2 Backend Product Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持 Stage 1 Parent / Child 状态边界的前提下，完成 Stage 2 的 Session、History、Active Run、Stop、Regenerate、Feedback 和 Delete 产品接口。

**Architecture:** Parent 最新活动 checkpoint 继续作为公共消息权威源；Session 与 Feedback 使用 PostgreSQL 业务表；Active Run 使用单实例进程内 Registry 和独立 producer task。所有 HTTP 与 SSE 只暴露稳定 Product DTO。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、LangGraph 1.2.11、PostgreSQL、psycopg 3、pytest、httpx、uv

---

## File Structure

```text
src/agent_runtime/
├── api/
│   ├── routes/
│   │   ├── chat.py                 # 普通聊天和 Regenerate SSE 入口
│   │   ├── messages.py             # 历史与 Feedback
│   │   └── sessions.py             # Session 列表、改名、Stop、删除
│   └── schemas/
│       ├── chat.py                 # Stage 2 SSE Schema
│       ├── messages.py             # Product Message、分页与 Feedback Schema
│       └── sessions.py             # Session、游标、改名和 Stop Schema
├── chat.py                         # Turn 准备与 Parent Graph 执行
├── feedback.py                     # Feedback 表和最终值存取
├── history.py                      # 活动 Parent 分支到 Product Message
├── regeneration.py                 # checkpoint 定位与 fork
├── runs.py                         # ActiveRunRegistry 与 Run 生命周期
├── sessions/
│   ├── cursor.py                   # Session 不透明游标
│   ├── repository.py               # Session 列表、改名、touch、删除
│   └── service.py                  # Session 产品规则
└── persistence/
    ├── checkpointers.py             # 将三个 saver 注入清理和 Regenerate
    └── parent_state.py              # 活动消息读取与 checkpoint 辅助
```

新增或修改的注释、docstring、提示词和用户可见文本必须使用中文。每个 Pydantic
字段必须写明用途、允许值和 Stage 2 限制的中文 `description`。

### Task 1: Session 游标列表

**Files:**
- Create: `src/agent_runtime/sessions/cursor.py`
- Create: `src/agent_runtime/api/schemas/sessions.py`
- Create: `src/agent_runtime/api/routes/sessions.py`
- Modify: `src/agent_runtime/sessions/repository.py`
- Modify: `src/agent_runtime/sessions/service.py`
- Modify: `src/agent_runtime/main.py`
- Test: `tests/sessions/test_cursor.py`
- Test: `tests/sessions/test_repository.py`
- Test: `tests/api/test_sessions_endpoint.py`

- [ ] **Step 1: 写游标编解码失败测试**

```python
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_runtime.sessions.cursor import (
    InvalidSessionCursorError,
    SessionCursor,
    decode_session_cursor,
    encode_session_cursor,
)


def test_session_cursor_round_trip() -> None:
    cursor = SessionCursor(
        updated_at=datetime(2026, 9, 13, 10, 30, tzinfo=UTC),
        session_id=UUID("00000000-0000-0000-0000-000000000101"),
    )

    assert decode_session_cursor(encode_session_cursor(cursor)) == cursor


def test_session_cursor_rejects_invalid_payload() -> None:
    with pytest.raises(InvalidSessionCursorError) as captured:
        decode_session_cursor("not-a-valid-cursor")

    assert captured.value.code == "SESSION_CURSOR_INVALID"
    assert captured.value.status_code == 400
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/sessions/test_cursor.py -q`
Expected: FAIL，原因是 `agent_runtime.sessions.cursor` 尚不存在。

- [ ] **Step 3: 实现版本化不透明游标**

```python
"""Session 列表的不透明游标。"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from agent_runtime.core.errors import ApplicationError


class InvalidSessionCursorError(ApplicationError):
    """客户端提供了无法解析的 Session 游标。"""


@dataclass(frozen=True, slots=True)
class SessionCursor:
    """稳定排序所需的更新时间和 Session UUID。"""

    updated_at: datetime
    session_id: UUID


def encode_session_cursor(cursor: SessionCursor) -> str:
    """将排序键编码成客户端不可解释的 URL-safe 游标。"""

    payload = json.dumps(
        {
            "v": 1,
            "updated_at": cursor.updated_at.isoformat(),
            "session_id": str(cursor.session_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_session_cursor(value: str) -> SessionCursor:
    """解析并严格验证 Stage 2 Session 游标。"""

    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload["v"] != 1:
            raise ValueError("unsupported cursor version")
        return SessionCursor(
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            session_id=UUID(payload["session_id"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidSessionCursorError(
            code="SESSION_CURSOR_INVALID",
            message="Session 分页游标无效",
            status_code=400,
        ) from error
```

- [ ] **Step 4: 为 Repository 增加稳定分页查询**

```python
_LIST_SESSIONS = """
SELECT session_id, user_id, title, created_at, updated_at
FROM sessions
WHERE user_id = %s
  AND (
    %s::timestamptz IS NULL
    OR (updated_at, session_id) < (%s::timestamptz, %s::uuid)
  )
ORDER BY updated_at DESC, session_id DESC
LIMIT %s
"""


async def list_for_user(
    self,
    *,
    user_id: str,
    cursor: SessionCursor | None,
    limit: int,
) -> list[Session]:
    """按固定用户和稳定排序键读取 limit + 1 条 Session。"""

    cursor_time = cursor.updated_at if cursor else None
    cursor_id = cursor.session_id if cursor else None
    async with open_database_connection(self._settings) as connection:
        result = await connection.execute(
            _LIST_SESSIONS,
            (user_id, cursor_time, cursor_time, cursor_id, limit),
        )
        rows = await result.fetchall()
    return [Session(**row) for row in rows]
```

调用方必须向 Repository 传入 `limit + 1`，用额外一条判断是否生成
`next_cursor`。

- [ ] **Step 5: 增加 Session Schema 与 GET 路由**

```python
class SessionItem(BaseModel):
    """聊天页面可直接消费的 Session 元数据。"""

    session_id: UUID = Field(description="服务端生成的 Session UUID。")
    title: str = Field(description="当前 Session 标题，Stage 2 最长 100 个字符。")
    created_at: datetime = Field(description="Session 创建时间，使用带时区时间。")
    updated_at: datetime = Field(description="Session 最近产品活动时间，使用带时区时间。")


class SessionPage(BaseModel):
    """按更新时间倒序返回的一页 Session。"""

    items: list[SessionItem] = Field(description="当前页 Session，最多 100 条。")
    next_cursor: str | None = Field(
        description="下一页不透明游标；没有更多 Session 时为 null。"
    )
```

```python
@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionPage:
    """返回固定本地用户的 Session 游标页。"""

    return await _get_chat_service(request).list_sessions(
        cursor=cursor,
        limit=limit,
    )
```

- [ ] **Step 6: 补 Repository 与 API 测试**

测试必须构造至少三个 Session，其中两个具有相同 `updated_at`，验证两页查询
没有重复或遗漏；API 测试验证默认 limit、非法游标和响应中不存在 `user_id`。

- [ ] **Step 7: 运行 Task 1 测试**

Run: `uv run pytest tests/sessions/test_cursor.py tests/sessions/test_repository.py tests/api/test_sessions_endpoint.py -q`
Expected: PASS。

- [ ] **Step 8: 提交 Task 1**

```bash
git add src/agent_runtime/sessions src/agent_runtime/api/schemas/sessions.py src/agent_runtime/api/routes/sessions.py src/agent_runtime/main.py tests/sessions tests/api/test_sessions_endpoint.py
git commit -m "feat: add cursor-based session listing"
```

### Task 2: Session 改名与标题限制

**Files:**
- Modify: `src/agent_runtime/sessions/repository.py`
- Modify: `src/agent_runtime/sessions/service.py`
- Modify: `src/agent_runtime/api/schemas/sessions.py`
- Modify: `src/agent_runtime/api/routes/sessions.py`
- Test: `tests/sessions/test_service.py`
- Test: `tests/api/test_sessions_endpoint.py`

- [ ] **Step 1: 写改名与默认标题失败测试**

```python
def test_rename_trims_title_and_persists_result() -> None:
    result = asyncio.run(service.rename_session(session_id, "  新标题  "))

    assert result.title == "新标题"
    assert repository.renamed == [(session_id, LOCAL_USER_ID, "新标题")]


def test_new_session_title_is_limited_to_one_hundred_characters() -> None:
    started = asyncio.run(
        service.prepare_new_session(content="甲" * 120)
    )

    assert started.session.title == "甲" * 100
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/sessions/test_service.py -q`
Expected: FAIL，原因是改名方法不存在且默认标题未截断。

- [ ] **Step 3: 实现所有权约束的 Repository 更新**

```python
_RENAME_SESSION = """
UPDATE sessions
SET title = %s, updated_at = %s
WHERE session_id = %s AND user_id = %s
RETURNING session_id, user_id, title, created_at, updated_at
"""


async def rename(
    self,
    *,
    session_id: UUID,
    user_id: str,
    title: str,
    updated_at: datetime,
) -> Session:
    """只更新固定用户拥有的 Session 标题。"""

    async with open_database_connection(self._settings) as connection:
        cursor = await connection.execute(
            _RENAME_SESSION,
            (title, updated_at, session_id, user_id),
        )
        row = await cursor.fetchone()
        await connection.commit()
    if row is None:
        raise SessionNotFoundError(
            code="SESSION_NOT_FOUND",
            message="Session 不存在",
            status_code=404,
        )
    return Session(**row)
```

- [ ] **Step 4: 增加严格请求 Schema 和路由**

```python
class RenameSessionRequest(BaseModel):
    """Stage 2 Session 手动改名请求。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        max_length=100,
        description="去除首尾空白后的标题；Stage 2 允许 1～100 个字符。",
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """去除首尾空白并拒绝空标题。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("title 不能为空")
        return normalized
```

```python
@router.patch("/sessions/{session_id}/rename", response_model=SessionItem)
async def rename_session(
    session_id: UUID,
    payload: RenameSessionRequest,
    request: Request,
) -> SessionItem:
    """手动修改固定用户拥有的 Session 标题。"""

    return await _get_chat_service(request).rename_session(
        session_id=session_id,
        title=payload.title,
    )
```

- [ ] **Step 5: 运行 Task 2 测试**

Run: `uv run pytest tests/sessions tests/api/test_sessions_endpoint.py -q`
Expected: PASS，并覆盖空白标题、101 字符、额外字段和 404。

- [ ] **Step 6: 提交 Task 2**

```bash
git add src/agent_runtime/sessions src/agent_runtime/api tests/sessions tests/api/test_sessions_endpoint.py
git commit -m "feat: add session rename support"
```

### Task 3: ActiveRunRegistry 与占用顺序

**Files:**
- Create: `src/agent_runtime/runs.py`
- Modify: `src/agent_runtime/chat.py`
- Modify: `src/agent_runtime/sessions/service.py`
- Test: `tests/test_runs.py`
- Test: `tests/test_chat_service.py`
- Test: `tests/api/test_chat_endpoint.py`

- [ ] **Step 1: 写 Registry 并发失败测试**

```python
import asyncio
from uuid import UUID

import pytest

from agent_runtime.runs import ActiveRunRegistry, SessionBusyError


SESSION_ID = UUID("00000000-0000-0000-0000-000000000301")
MESSAGE_ID = UUID("00000000-0000-0000-0000-000000000302")


def test_registry_rejects_second_run_and_releases_by_token() -> None:
    async def exercise() -> None:
        registry = ActiveRunRegistry()
        first = await registry.reserve(SESSION_ID, MESSAGE_ID)

        with pytest.raises(SessionBusyError) as captured:
            await registry.reserve(SESSION_ID, MESSAGE_ID)

        assert captured.value.code == "SESSION_BUSY"
        await registry.release(first)
        second = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(second)

    asyncio.run(exercise())
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/test_runs.py -q`
Expected: FAIL，原因是 `agent_runtime.runs` 尚不存在。

- [ ] **Step 3: 实现带 token 的 ActiveRunRegistry**

```python
"""单实例 Stage 2 Active Run 协调。"""

import asyncio
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

from agent_runtime.core.errors import ApplicationError

type CancelReason = Literal["stopped", "disconnected"]


class SessionBusyError(ApplicationError):
    """同一 Session 已经存在活动 Run。"""


@dataclass(slots=True)
class ActiveRun:
    """一次活动执行的并发控制句柄，不保存业务 State。"""

    session_id: UUID
    message_id: UUID
    token: UUID = field(default_factory=uuid4)
    task: asyncio.Task[None] | None = None
    cancel_reason: CancelReason | None = None
    terminal: asyncio.Event = field(default_factory=asyncio.Event)


class ActiveRunRegistry:
    """保证单实例内每个 Session 同时最多一个 Run。"""

    def __init__(self) -> None:
        self._runs: dict[UUID, ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, session_id: UUID, message_id: UUID) -> ActiveRun:
        """在写入新 HumanMessage 前占用 Session。"""

        async with self._lock:
            if session_id in self._runs:
                raise SessionBusyError(
                    code="SESSION_BUSY",
                    message="当前 Session 正在生成回复",
                    status_code=409,
                    retryable=True,
                )
            run = ActiveRun(session_id=session_id, message_id=message_id)
            self._runs[session_id] = run
            return run

    async def attach_task(
        self,
        run: ActiveRun,
        task: asyncio.Task[None],
    ) -> None:
        """绑定 producer，并处理绑定前已经到达的取消。"""

        async with self._lock:
            current = self._runs.get(run.session_id)
            if current is not run:
                task.cancel()
                return
            run.task = task
            should_cancel = run.cancel_reason is not None
        if should_cancel:
            task.cancel()

    async def request_stop(
        self,
        session_id: UUID,
        reason: CancelReason,
    ) -> bool:
        """请求停止并等待对应 Run 完成全部终态处理。"""

        async with self._lock:
            run = self._runs.get(session_id)
            if run is None:
                return False
            if run.cancel_reason is None:
                run.cancel_reason = reason
            task = run.task
        if task is not None:
            task.cancel()
        await run.terminal.wait()
        return True

    async def release(self, run: ActiveRun) -> None:
        """只允许当前 token 对应的 Run 释放 Session。"""

        async with self._lock:
            if self._runs.get(run.session_id) is run:
                del self._runs[run.session_id]
        run.terminal.set()
```

- [ ] **Step 4: 将占用放到 HumanMessage 写入之前**

重构 `ChatService.prepare_turn`：

```python
response_message_id = self._response_message_id_factory()
if session_id is None:
    session_id = self._session_id_factory()
    active_run = await self._run_registry.reserve(
        session_id,
        response_message_id,
    )
    try:
        started = await self._session_service.prepare_new_session(
            session_id=session_id,
            content=content,
        )
    except Exception:
        await self._run_registry.release(active_run)
        raise
    human_message = started.human_message
else:
    await self._require_owned_session_state(session_id)
    active_run = await self._run_registry.reserve(
        session_id,
        response_message_id,
    )
    human_message = HumanMessage(
        content=content,
        id=str(self._human_message_id_factory()),
    )
```

`PreparedChatTurn` 增加 `active_run: ActiveRun`。新 Session 的 UUID 从
`ChatService` 注入 `SessionService.prepare_new_session`，保持“Session 和首条
HumanMessage 在 Router 前持久化”的既有规则。

- [ ] **Step 5: 测试 SESSION_BUSY 零写入**

```python
def test_busy_session_does_not_create_human_message() -> None:
    first_turn = asyncio.run(
        service.prepare_turn(session_id=session_id, content="第一条")
    )

    with pytest.raises(SessionBusyError):
        asyncio.run(
            service.prepare_turn(session_id=session_id, content="不应写入")
        )

    assert parent_message_contents(session_id) == existing_contents
    asyncio.run(registry.release(first_turn.active_run))
```

- [ ] **Step 6: 运行 Task 3 测试**

Run: `uv run pytest tests/test_runs.py tests/test_chat_service.py tests/api/test_chat_endpoint.py -q`
Expected: PASS，并验证不同 Session 可并发、异常准备会释放占用。

- [ ] **Step 7: 提交 Task 3**

```bash
git add src/agent_runtime/runs.py src/agent_runtime/chat.py src/agent_runtime/sessions/service.py tests/test_runs.py tests/test_chat_service.py tests/api/test_chat_endpoint.py
git commit -m "feat: coordinate one active run per session"
```

### Task 4: Producer、Stop 与 Stage 2 SSE

**Files:**
- Modify: `src/agent_runtime/runs.py`
- Modify: `src/agent_runtime/chat.py`
- Modify: `src/agent_runtime/streaming/sse.py`
- Modify: `src/agent_runtime/api/schemas/chat.py`
- Modify: `src/agent_runtime/api/schemas/sessions.py`
- Modify: `src/agent_runtime/api/routes/sessions.py`
- Modify: `src/agent_runtime/graph/parent.py`
- Modify: `src/agent_runtime/graph/context.py`
- Test: `tests/test_runs.py`
- Test: `tests/streaming/test_sse.py`
- Test: `tests/streaming/test_incomplete.py`
- Test: `tests/api/test_sessions_endpoint.py`
- Test: `tests/graph/test_context.py`

- [ ] **Step 1: 写 Stop 终态失败测试**

```python
def test_stop_waits_for_stopped_persistence_and_release() -> None:
    async def exercise() -> None:
        turn = await service.prepare_turn(
            session_id=session_id,
            content="生成一个很长的回答",
        )
        stream = service.stream_product_events(turn)
        first = await anext(stream)
        assert first.name == "message"

        stopped = await service.stop_session(session_id)

        remaining = [event async for event in stream]
        assert stopped.active_run is False
        assert remaining[-1].name == "done"
        assert remaining[-1].data.status == "stopped"
        message = latest_parent_ai_message(session_id)
        assert message.additional_kwargs["runtime_status"] == "stopped"
        assert await registry.contains(session_id) is False

    asyncio.run(exercise())
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/streaming/test_sse.py tests/api/test_sessions_endpoint.py -q`
Expected: FAIL，当前 Graph 仍直接运行在 SSE 生成器中，且没有 Stop。

- [ ] **Step 3: 定义内部 ProductRunEvent**

```python
@dataclass(frozen=True, slots=True)
class ProductRunEvent:
    """producer 与 SSE Adapter 之间的稳定产品事件。"""

    name: Literal["message", "error", "done"]
    data: MessageEventData | ErrorEventData | DoneEventData
```

`ActiveRun` 增加
`queue: asyncio.Queue[ProductRunEvent | None] = field(default_factory=asyncio.Queue)`。
`None` 是 producer 已结束的内部哨兵，不进入 SSE。

- [ ] **Step 4: 扩展 SSE Schema**

```python
type CapabilityId = Literal["general_chat", "en_to_zh"]


class MessageEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(description="本轮所属的稳定 Session UUID。")
    message_id: UUID = Field(description="本轮 AIMessage 的服务端稳定 UUID。")
    capability_id: CapabilityId | None = Field(
        description="实际处理本轮的固定 Capability；unsupported 或未知时为 null。"
    )
    delta: str = Field(description="本次事件携带的用户可见增量文本。")


class DoneEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(description="本轮所属的稳定 Session UUID。")
    message_id: UUID = Field(description="本轮 AIMessage 的服务端稳定 UUID。")
    capability_id: CapabilityId | None = Field(
        description="本轮最终使用的固定 Capability；unsupported 或未知时为 null。"
    )
    status: Literal[
        "completed", "unsupported", "stopped", "failed"
    ] = Field(description="Stage 2 Run 的唯一产品终态。")
```

- [ ] **Step 5: 为公共 AIMessage 写入产品元数据**

在 Parent 完成分支中复制消息并合并元数据：

```python
metadata = {
    **invocation.message.additional_kwargs,
    "runtime_status": "completed",
    "capability_id": capability_id,
}
public_message = invocation.message.model_copy(
    update={"additional_kwargs": metadata}
)
```

unsupported 消息写入
`{"runtime_status": "unsupported", "capability_id": None}`。Context Builder
将 `stopped` 与 `unsupported`、`incomplete` 一样排除。

- [ ] **Step 6: 实现统一 producer 终态**

```python
async def _produce_turn(
    self,
    turn: PreparedChatTurn,
) -> None:
    """执行 Parent 并把所有产品终态写入 Run 队列。"""

    partial: list[str] = []
    capability_id: CapabilityId | None = None
    try:
        async for message in self.stream_turn(turn):
            if capability_id is None:
                capability_id = await self.resolve_capability_id(turn.config)
            delta = str(message.text)
            if delta:
                partial.append(delta)
                await turn.active_run.queue.put(
                    ProductRunEvent(
                        name="message",
                        data=MessageEventData(
                            session_id=turn.session_id,
                            message_id=turn.response_message_id,
                            capability_id=capability_id,
                            delta=delta,
                        ),
                    )
                )
        status = await self.get_completion_status(turn)
        await turn.active_run.queue.put(
            self.done_event(turn, capability_id, status)
        )
    except asyncio.CancelledError:
        reason = turn.active_run.cancel_reason or "disconnected"
        runtime_status = "stopped" if reason == "stopped" else "incomplete"
        await self.persist_interrupted(
            turn,
            content="".join(partial),
            runtime_status=runtime_status,
            capability_id=capability_id,
        )
        if reason == "stopped":
            await turn.active_run.queue.put(
                self.done_event(turn, capability_id, "stopped")
            )
    except Exception as error:
        await self.persist_failure_if_needed(
            turn,
            content="".join(partial),
            capability_id=capability_id,
        )
        await turn.active_run.queue.put(self.error_event(turn, error))
        await turn.active_run.queue.put(
            self.done_event(turn, capability_id, "failed")
        )
    finally:
        await self.touch_session(turn.session_id)
        await turn.active_run.queue.put(None)
        await self._run_registry.release(turn.active_run)
```

`persist_interrupted` 对 stopped 始终保存 AIMessage；对 disconnected 也保存
`incomplete` AIMessage，即使 content 为空，使已经接受的 HumanMessage 有明确
结果。

- [ ] **Step 7: 实现 SSE 消费与断开处理**

```python
async def stream_chat_sse(
    chat_service: ChatService,
    turn: PreparedChatTurn,
) -> AsyncIterator[str]:
    """消费独立 producer 的产品事件，并在断开时停止 Run。"""

    await chat_service.start_producer(turn)
    completed = False
    try:
        while True:
            event = await turn.active_run.queue.get()
            if event is None:
                completed = True
                return
            yield encode_sse(event.name, event.data)
    finally:
        if not completed:
            await chat_service.disconnect_session(turn.session_id)
```

- [ ] **Step 8: 增加幂等 Stop 路由**

```python
class StopSessionResponse(BaseModel):
    """Stop 返回后的确定运行状态。"""

    session_id: UUID = Field(description="已检查或停止的 Session UUID。")
    stopped: bool = Field(description="本次请求是否实际停止了一个 active Run。")
    active_run: Literal[False] = Field(
        default=False,
        description="返回时固定为 false，表示同 Session 可以立即开始新 Run。",
    )
```

```python
@router.post(
    "/sessions/{session_id}/stop",
    response_model=StopSessionResponse,
)
async def stop_session(
    session_id: UUID,
    request: Request,
) -> StopSessionResponse:
    """停止并等待固定用户 Session 的当前 Run 完成清理。"""

    return await _get_chat_service(request).stop_session(session_id)
```

- [ ] **Step 9: 运行 Task 4 测试**

Run: `uv run pytest tests/test_runs.py tests/streaming tests/api/test_chat_endpoint.py tests/api/test_sessions_endpoint.py tests/graph/test_context.py -q`
Expected: PASS；事件严格为 `message/error/done`，Stop 为
`message.../done(stopped)`，断开无残留 Run。

- [ ] **Step 10: 提交 Task 4**

```bash
git add src/agent_runtime/runs.py src/agent_runtime/chat.py src/agent_runtime/streaming src/agent_runtime/api src/agent_runtime/graph tests
git commit -m "feat: add stoppable stage two streaming runs"
```

### Task 5: 活动分支消息历史 Adapter

**Files:**
- Create: `src/agent_runtime/history.py`
- Create: `src/agent_runtime/api/schemas/messages.py`
- Create: `src/agent_runtime/api/routes/messages.py`
- Modify: `src/agent_runtime/main.py`
- Test: `tests/test_history.py`
- Test: `tests/api/test_messages_endpoint.py`

- [ ] **Step 1: 写状态转换与 before 分页失败测试**

```python
def test_history_returns_latest_page_in_chronological_order() -> None:
    page = asyncio.run(service.get_messages(session_id, before=None, limit=4))

    assert [item.message_id for item in page.items] == message_ids[-4:]
    assert page.next_before == message_ids[-4]
    assert page.items[-1].runtime_status == "stopped"


def test_history_rejects_cursor_outside_active_branch() -> None:
    with pytest.raises(MessageCursorError) as captured:
        asyncio.run(
            service.get_messages(
                session_id,
                before=old_branch_message_id,
                limit=50,
            )
        )

    assert captured.value.code == "MESSAGE_CURSOR_INVALID"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/test_history.py -q`
Expected: FAIL，原因是 `agent_runtime.history` 尚不存在。

- [ ] **Step 3: 定义 Product Message Schema**

```python
type MessageRole = Literal["user", "assistant"]
type RuntimeStatus = Literal[
    "completed", "unsupported", "incomplete", "stopped"
]
type FeedbackValue = Literal["like", "dislike"]


class ProductMessage(BaseModel):
    """从 Parent 权威历史转换的前端消息。"""

    message_id: UUID = Field(description="服务端分配的稳定消息 UUID。")
    role: MessageRole = Field(description="消息角色，只允许 user 或 assistant。")
    content: str = Field(description="完整用户可见文本，可为空的 stopped 输出除外。")
    runtime_status: RuntimeStatus | None = Field(
        description="AIMessage 产品状态；HumanMessage 固定为 null。"
    )
    capability_id: CapabilityId | None = Field(
        description="处理 AIMessage 的固定 Capability；用户消息或旧消息可为 null。"
    )
    feedback: FeedbackValue | None = Field(
        description="固定用户对 completed AIMessage 的最终反馈；没有反馈时为 null。"
    )


class MessagePage(BaseModel):
    """当前活动分支的一页公共消息。"""

    items: list[ProductMessage] = Field(
        description="按对话时间正序排列的公共消息，最多 100 条。"
    )
    next_before: UUID | None = Field(
        description="加载更早消息时使用的 message_id；没有更早消息时为 null。"
    )
```

- [ ] **Step 4: 实现活动 Parent 消息转换**

```python
_AI_STATUSES = {"completed", "unsupported", "incomplete", "stopped"}
_CAPABILITY_IDS = {"general_chat", "en_to_zh"}


def to_product_message(
    message: BaseMessage,
    feedback: FeedbackValue | None,
) -> ProductMessage:
    """将公共 LangChain Message 转成稳定 Product DTO。"""

    if message.id is None:
        raise HistoryStateError(
            code="MESSAGE_ID_MISSING",
            message="公共消息缺少稳定 message_id",
        )
    if isinstance(message, HumanMessage):
        return ProductMessage(
            message_id=UUID(message.id),
            role="user",
            content=str(message.content),
            runtime_status=None,
            capability_id=None,
            feedback=None,
        )
    if not isinstance(message, AIMessage):
        raise HistoryStateError(
            code="MESSAGE_TYPE_INVALID",
            message="公共历史包含不支持的消息类型",
        )
    raw_status = message.additional_kwargs.get("runtime_status", "completed")
    if raw_status not in _AI_STATUSES:
        raise HistoryStateError(
            code="MESSAGE_STATUS_INVALID",
            message="公共 AIMessage 包含非法运行状态",
        )
    raw_capability = message.additional_kwargs.get("capability_id")
    capability = raw_capability if raw_capability in _CAPABILITY_IDS else None
    return ProductMessage(
        message_id=UUID(message.id),
        role="assistant",
        content=str(message.content),
        runtime_status=raw_status,
        capability_id=capability,
        feedback=feedback,
    )
```

- [ ] **Step 5: 实现 before 切片**

```python
def paginate_messages(
    messages: list[ProductMessage],
    *,
    before: UUID | None,
    limit: int,
) -> MessagePage:
    """对当前活动消息列表执行向前游标切片。"""

    end = len(messages)
    if before is not None:
        try:
            end = next(
                index
                for index, item in enumerate(messages)
                if item.message_id == before
            )
        except StopIteration as error:
            raise MessageCursorError(
                code="MESSAGE_CURSOR_INVALID",
                message="消息分页游标不属于当前活动历史",
                status_code=400,
            ) from error
    start = max(0, end - limit)
    items = messages[start:end]
    next_before = items[0].message_id if start > 0 and items else None
    return MessagePage(items=items, next_before=next_before)
```

`HistoryService` 先验证 Session 所属关系，从
`parent_graph.aget_state(parent_thread_config(session_id))` 读取最新活动
`messages`，批量查询这些 message_id 的 Feedback，再转换和切片。

- [ ] **Step 6: 增加消息历史路由**

```python
@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessagePage,
)
async def list_messages(
    session_id: UUID,
    request: Request,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    """返回当前活动 Parent 分支的一页公共消息。"""

    return await _get_chat_service(request).get_messages(
        session_id=session_id,
        before=before,
        limit=limit,
    )
```

- [ ] **Step 7: 运行 Task 5 测试**

Run: `uv run pytest tests/test_history.py tests/api/test_messages_endpoint.py tests/graph/test_context.py -q`
Expected: PASS，并断言 JSON 中不包含
`checkpoint`、`next`、`tasks`、`metadata`、`node`。

- [ ] **Step 8: 提交 Task 5**

```bash
git add src/agent_runtime/history.py src/agent_runtime/api/schemas/messages.py src/agent_runtime/api/routes/messages.py src/agent_runtime/main.py tests/test_history.py tests/api/test_messages_endpoint.py
git commit -m "feat: expose active-branch message history"
```

### Task 6: 最新完成回答 Regenerate

**Files:**
- Create: `src/agent_runtime/regeneration.py`
- Modify: `src/agent_runtime/chat.py`
- Modify: `src/agent_runtime/api/routes/chat.py`
- Modify: `src/agent_runtime/graph/config.py`
- Test: `tests/test_regeneration.py`
- Test: `tests/api/test_regenerate_endpoint.py`
- Test: `tests/graph/test_regenerate_checkpoint.py`

- [ ] **Step 1: 写 LangGraph fork 特征测试**

```python
def test_update_state_forks_before_capability_without_duplicate_human() -> None:
    async def exercise() -> None:
        runtime = build_in_memory_runtime()
        session_id, human_id, first_ai_id = await runtime.complete_one_turn()
        config = parent_thread_config(session_id)
        snapshots = [
            snapshot
            async for snapshot in runtime.parent.aget_state_history(config)
        ]
        before_answer = next(
            snapshot
            for snapshot in snapshots
            if snapshot.next == ("invoke_capability",)
            and snapshot.values["messages"][-1].id == str(human_id)
        )

        fork_config = await runtime.parent.aupdate_state(
            before_answer.config,
            {"completion_status": None},
        )
        fork_config["configurable"]["message_id"] = str(NEW_AI_ID)
        await consume(
            runtime.parent.astream(
                None,
                fork_config,
                stream_mode="custom",
            )
        )

        latest = await runtime.parent.aget_state(config)
        ids = [message.id for message in latest.values["messages"]]
        assert ids.count(str(human_id)) == 1
        assert str(first_ai_id) not in ids
        assert str(NEW_AI_ID) in ids
        assert len(snapshots) < len(
            [item async for item in runtime.parent.aget_state_history(config)]
        )

    asyncio.run(exercise())
```

- [ ] **Step 2: 运行特征测试并确认失败**

Run: `uv run pytest tests/graph/test_regenerate_checkpoint.py -q`
Expected: FAIL，当前没有 Regenerate 辅助代码；完成测试夹具后应验证安装的
LangGraph 1.2.11 与官方 time-travel 语义一致。

- [ ] **Step 3: 实现目标资格检查**

```python
def require_regeneratable_message(
    messages: list[BaseMessage],
    message_id: UUID,
) -> HumanMessage:
    """确认目标是活动分支最后一个 completed AIMessage。"""

    ai_messages = [
        message for message in messages if isinstance(message, AIMessage)
    ]
    if not ai_messages or ai_messages[-1].id != str(message_id):
        raise RegenerationError(
            code="MESSAGE_NOT_REGENERATABLE",
            message="只能重新生成当前会话的最新完成回答",
            status_code=409,
        )
    target = ai_messages[-1]
    status = target.additional_kwargs.get("runtime_status", "completed")
    if status != "completed":
        raise RegenerationError(
            code="MESSAGE_NOT_REGENERATABLE",
            message="该消息状态不允许重新生成",
            status_code=409,
        )
    target_index = messages.index(target)
    if target_index == 0 or not isinstance(messages[target_index - 1], HumanMessage):
        raise RegenerationError(
            code="MESSAGE_HISTORY_INVALID",
            message="目标回答缺少对应的用户消息",
        )
    return messages[target_index - 1]
```

- [ ] **Step 4: 实现 checkpoint 定位与 fork**

```python
async def fork_before_answer(
    parent_graph: Any,
    *,
    session_id: UUID,
    human_message_id: UUID,
    response_message_id: UUID,
) -> RunnableConfig:
    """从回答执行前的 Parent checkpoint 创建新分支。"""

    config = parent_thread_config(session_id)
    snapshot = None
    async for item in parent_graph.aget_state_history(config):
        if (
            item.next == ("invoke_capability",)
            and item.values["messages"]
            and item.values["messages"][-1].id == str(human_message_id)
        ):
            snapshot = item
            break
    if snapshot is None:
        raise RegenerationError(
            code="REGENERATION_CHECKPOINT_NOT_FOUND",
            message="未找到可重新生成的历史状态",
            status_code=409,
        )
    fork_config = await parent_graph.aupdate_state(
        snapshot.config,
        {"completion_status": None},
    )
    configurable = dict(fork_config["configurable"])
    configurable["message_id"] = str(response_message_id)
    return {**fork_config, "configurable": configurable}
```

StateSnapshot 只在该内部函数中使用，不得返回给 API。

- [ ] **Step 5: 将 PreparedChatTurn 支持空图输入**

```python
@dataclass(frozen=True, slots=True)
class PreparedChatTurn:
    """普通聊天或 Regenerate 的统一产品 Run。"""

    session_id: UUID
    response_message_id: UUID
    config: RunnableConfig
    active_run: ActiveRun
    graph_input: dict[str, list[BaseMessage]] | None
```

普通聊天设置 `graph_input={"messages": [human_message]}`；Regenerate 设置
`graph_input=None`。producer 一律调用：

```python
self._parent_graph.astream(
    turn.graph_input,
    turn.config,
    stream_mode="custom",
)
```

- [ ] **Step 6: 增加 Regenerate SSE 路由**

```python
@router.post(
    "/sessions/{session_id}/messages/{message_id}/regenerate"
)
async def regenerate_message(
    session_id: UUID,
    message_id: UUID,
    request: Request,
) -> StreamingResponse:
    """从最新完成回答前的 checkpoint fork 并返回产品 SSE。"""

    turn = await _get_chat_service(request).prepare_regeneration(
        session_id=session_id,
        message_id=message_id,
    )
    return build_streaming_response(turn, request)
```

`prepare_regeneration` 必须先 reserve Session，再读取最新活动状态并 fork；任一
验证或 fork 失败都释放占用。

- [ ] **Step 7: 运行 Task 6 测试**

Run: `uv run pytest tests/test_regeneration.py tests/api/test_regenerate_endpoint.py tests/graph/test_regenerate_checkpoint.py -q`
Expected: PASS，并覆盖中间回答、unsupported、incomplete、stopped、旧分支
message_id、SESSION_BUSY 和新 UUID。

- [ ] **Step 8: 提交 Task 6**

```bash
git add src/agent_runtime/regeneration.py src/agent_runtime/chat.py src/agent_runtime/api/routes/chat.py src/agent_runtime/graph/config.py tests/test_regeneration.py tests/api/test_regenerate_endpoint.py tests/graph/test_regenerate_checkpoint.py
git commit -m "feat: regenerate latest response from checkpoint fork"
```

### Task 7: Feedback 最终值

**Files:**
- Create: `src/agent_runtime/feedback.py`
- Modify: `src/agent_runtime/api/schemas/messages.py`
- Modify: `src/agent_runtime/api/routes/messages.py`
- Modify: `src/agent_runtime/history.py`
- Modify: `src/agent_runtime/chat.py`
- Test: `tests/test_feedback.py`
- Test: `tests/api/test_feedback_endpoint.py`
- Test: `tests/persistence/test_feedback_postgres.py`

- [ ] **Step 1: 写 upsert、cancel 与资格失败测试**

```python
def test_feedback_keeps_only_latest_value() -> None:
    async def exercise() -> None:
        await service.set_feedback(message_id, "like")
        await service.set_feedback(message_id, "dislike")
        assert await repository.get(LOCAL_USER_ID, message_id) == "dislike"

        await service.set_feedback(message_id, "cancel")
        assert await repository.get(LOCAL_USER_ID, message_id) is None

    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["unsupported", "incomplete", "stopped"])
def test_feedback_rejects_non_completed_ai_message(status: str) -> None:
    with pytest.raises(FeedbackTargetError) as captured:
        asyncio.run(service.set_feedback(message_id_for(status), "like"))

    assert captured.value.code == "MESSAGE_NOT_FEEDBACK_ELIGIBLE"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/test_feedback.py -q`
Expected: FAIL，原因是 `agent_runtime.feedback` 尚不存在。

- [ ] **Step 3: 创建 Feedback 表和 Repository**

```python
_CREATE_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS message_feedback (
    user_id TEXT NOT NULL,
    session_id UUID NOT NULL,
    message_id UUID NOT NULL,
    value TEXT NOT NULL CHECK (value IN ('like', 'dislike')),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, message_id)
)
"""

_CREATE_FEEDBACK_SESSION_INDEX = """
CREATE INDEX IF NOT EXISTS message_feedback_session_idx
ON message_feedback (user_id, session_id)
"""

_UPSERT_FEEDBACK = """
INSERT INTO message_feedback (
    user_id, session_id, message_id, value, updated_at
)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, message_id)
DO UPDATE SET
    session_id = EXCLUDED.session_id,
    value = EXCLUDED.value,
    updated_at = EXCLUDED.updated_at
"""

_DELETE_FEEDBACK = """
DELETE FROM message_feedback
WHERE user_id = %s AND message_id = %s
"""
```

`FeedbackRepository.setup` 在同一连接中依次执行
`_CREATE_FEEDBACK_TABLE` 和 `_CREATE_FEEDBACK_SESSION_INDEX` 后提交。

```python
async def set(
    self,
    *,
    user_id: str,
    session_id: UUID,
    message_id: UUID,
    value: Literal["like", "dislike"],
) -> None:
    """新增或覆盖固定用户对一条消息的最终反馈。"""

    async with open_database_connection(self._settings) as connection:
        await connection.execute(
            _UPSERT_FEEDBACK,
            (
                user_id,
                session_id,
                message_id,
                value,
                datetime.now(UTC),
            ),
        )
        await connection.commit()


async def cancel(self, *, user_id: str, message_id: UUID) -> None:
    """幂等删除固定用户对一条消息的反馈。"""

    async with open_database_connection(self._settings) as connection:
        await connection.execute(
            _DELETE_FEEDBACK,
            (user_id, message_id),
        )
        await connection.commit()
```

同时实现 `get_many(user_id, message_ids)`，使用参数化
`message_id = ANY(%s::uuid[])` 一次读取当前历史页所需反馈，禁止逐条查询。

- [ ] **Step 4: 实现活动分支完成消息资格**

```python
async def require_feedback_target(
    self,
    *,
    message_id: UUID,
) -> tuple[UUID, ProductMessage]:
    """返回活动分支中可反馈的 completed AIMessage。"""

    session_ids = await self._session_repository.list_ids_for_user(
        self._settings.local_user_id
    )
    for target_session_id in session_ids:
        state = await self._parent_graph.aget_state(
            parent_thread_config(target_session_id)
        )
        for message in state.values.get("messages", []):
            if message.id != str(message_id):
                continue
            target = to_product_message(message, feedback=None)
            if (
                target.role != "assistant"
                or target.runtime_status != "completed"
            ):
                raise FeedbackTargetError(
                    code="MESSAGE_NOT_FEEDBACK_ELIGIBLE",
                    message="该消息不能提交反馈",
                    status_code=409,
                )
            return target_session_id, target
    raise FeedbackTargetError(
        code="MESSAGE_NOT_FOUND",
        message="消息不存在",
        status_code=404,
    )
```

由于 Feedback 路由只有 `message_id`，History Service 需要在固定用户拥有的
Session 中定位包含该 message_id 的最新活动 Parent 状态，并把找到的
`target_session_id` 与 ProductMessage 一起返回。Stage 2 固定按 Session
新到旧扫描；不得搜索旧 checkpoint 分支。若后续规模需要消息索引，应
作为新阶段设计而不是在本任务提前引入。

`FeedbackService.set_feedback` 使用返回的 Session ID 持久化清理索引：

```python
async def set_feedback(
    self,
    *,
    message_id: UUID,
    action: FeedbackAction,
) -> FeedbackResponse:
    """验证活动消息后设置或取消最终反馈。"""

    session_id, _target = await self._history.require_feedback_target(
        message_id=message_id
    )
    if action == "cancel":
        await self._repository.cancel(
            user_id=self._settings.local_user_id,
            message_id=message_id,
        )
        value = None
    else:
        await self._repository.set(
            user_id=self._settings.local_user_id,
            session_id=session_id,
            message_id=message_id,
            value=action,
        )
        value = action
    return FeedbackResponse(message_id=message_id, feedback=value)
```

- [ ] **Step 5: 增加 Feedback Schema 与路由**

```python
class FeedbackRequest(BaseModel):
    """对一条可反馈 AIMessage 设置最终反馈。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["like", "dislike", "cancel"] = Field(
        description="反馈动作；like/dislike 覆盖旧值，cancel 幂等删除旧值。"
    )


class FeedbackResponse(BaseModel):
    """写入后该消息的最终反馈。"""

    message_id: UUID = Field(description="被操作的 AIMessage UUID。")
    feedback: FeedbackValue | None = Field(
        description="最终反馈；cancel 后固定为 null。"
    )
```

```python
@router.post(
    "/messages/{message_id}/feedback",
    response_model=FeedbackResponse,
)
async def set_message_feedback(
    message_id: UUID,
    payload: FeedbackRequest,
    request: Request,
) -> FeedbackResponse:
    """设置或取消固定用户对活动完成消息的反馈。"""

    return await _get_chat_service(request).set_feedback(
        message_id=message_id,
        action=payload.action,
    )
```

- [ ] **Step 6: 运行 Task 7 测试**

Run: `uv run pytest tests/test_feedback.py tests/api/test_feedback_endpoint.py -q`
Expected: PASS。

Run: `$env:RUN_POSTGRES_TESTS='1'; uv run pytest tests/persistence/test_feedback_postgres.py -q`
Expected: PASS；测试结束后删除自身创建的 Feedback 和 Session 数据。

- [ ] **Step 7: 提交 Task 7**

```bash
git add src/agent_runtime/feedback.py src/agent_runtime/history.py src/agent_runtime/chat.py src/agent_runtime/api tests/test_feedback.py tests/api/test_feedback_endpoint.py tests/persistence/test_feedback_postgres.py
git commit -m "feat: persist final message feedback"
```

### Task 8: 幂等 Session 硬删除

**Files:**
- Modify: `src/agent_runtime/sessions/repository.py`
- Modify: `src/agent_runtime/sessions/service.py`
- Modify: `src/agent_runtime/feedback.py`
- Modify: `src/agent_runtime/persistence/checkpointers.py`
- Modify: `src/agent_runtime/chat.py`
- Modify: `src/agent_runtime/api/routes/sessions.py`
- Test: `tests/sessions/test_delete.py`
- Test: `tests/api/test_sessions_endpoint.py`
- Test: `tests/integration/test_stage_two_delete.py`

- [ ] **Step 1: 写删除顺序与可重试失败测试**

```python
def test_delete_stops_run_and_deletes_session_last() -> None:
    asyncio.run(service.delete_session(session_id))

    assert calls == [
        "stop",
        "child:general_chat",
        "child:en_to_zh",
        "parent",
        "feedback",
        "session",
    ]


def test_delete_keeps_session_when_checkpoint_cleanup_fails() -> None:
    cleanup.fail_on = "parent"

    with pytest.raises(CheckpointCleanupError):
        asyncio.run(service.delete_session(session_id))

    assert repository.session_exists(session_id)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/sessions/test_delete.py -q`
Expected: FAIL，当前没有产品删除流程。

- [ ] **Step 3: 实现所有权感知的幂等删除**

```python
async def delete_for_user(
    self,
    *,
    session_id: UUID,
    user_id: str,
) -> bool:
    """删除固定用户 Session，并返回是否实际删除。"""

    async with open_database_connection(self._settings) as connection:
        cursor = await connection.execute(
            "DELETE FROM sessions WHERE session_id = %s AND user_id = %s",
            (session_id, user_id),
        )
        await connection.commit()
    return cursor.rowcount > 0
```

`SessionService.find_owned_or_none` 对不存在和其他用户统一返回 `None`，只供
DELETE 使用；查询、改名、历史和 Regenerate 仍返回 404。

- [ ] **Step 4: 实现 checkpoint 清理边界**

```python
@dataclass(frozen=True, slots=True)
class CheckpointCleanup:
    """按 Parent / Child thread_id 删除 Stage 2 Session 状态。"""

    parent: AsyncPostgresSaver
    general_chat: AsyncPostgresSaver
    en_to_zh: AsyncPostgresSaver

    async def delete_session(self, session_id: UUID) -> None:
        """先删除两个 Child，再删除 Parent checkpoint。"""

        await self.general_chat.adelete_thread(
            f"{session_id}:general_chat"
        )
        await self.en_to_zh.adelete_thread(
            f"{session_id}:en_to_zh"
        )
        await self.parent.adelete_thread(str(session_id))
```

- [ ] **Step 5: 编排删除并保持 Session 最后**

```python
async def delete_session(self, session_id: UUID) -> None:
    """幂等停止并清理固定用户 Session 的全部 Stage 2 数据。"""

    session = await self._session_service.find_owned_or_none(session_id)
    if session is None:
        return
    await self.stop_session(session_id)
    await self._checkpoint_cleanup.delete_session(session_id)
    await self._feedback_repository.delete_for_session(
        user_id=self._settings.local_user_id,
        session_id=session_id,
    )
    await self._session_repository.delete_for_user(
        session_id=session_id,
        user_id=self._settings.local_user_id,
    )
```

`FeedbackRepository.delete_for_session(user_id, session_id)` 必须直接按
`session_id` 删除，不依赖已经可能被清理的 Parent checkpoint。因此即使
checkpoint 清理成功、Feedback 清理失败，下一次 DELETE 仍能完成剩余工作。

```python
async def delete_for_session(
    self,
    *,
    user_id: str,
    session_id: UUID,
) -> None:
    """按清理索引删除一个 Session 的全部反馈。"""

    async with open_database_connection(self._settings) as connection:
        await connection.execute(
            """
            DELETE FROM message_feedback
            WHERE user_id = %s AND session_id = %s
            """,
            (user_id, session_id),
        )
        await connection.commit()
```

- [ ] **Step 6: 增加 HTTP 204 路由**

```python
@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session(
    session_id: UUID,
    request: Request,
) -> Response:
    """幂等硬删除固定用户 Session 及其关联数据。"""

    await _get_chat_service(request).delete_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 7: 运行 Task 8 测试**

Run: `uv run pytest tests/sessions/test_delete.py tests/api/test_sessions_endpoint.py -q`
Expected: PASS。

Run: `$env:RUN_POSTGRES_TESTS='1'; uv run pytest tests/integration/test_stage_two_delete.py -q`
Expected: PASS，并证明 Parent、两个 Child、Feedback 和 Session 均不可恢复。

- [ ] **Step 8: 提交 Task 8**

```bash
git add src/agent_runtime/sessions src/agent_runtime/feedback.py src/agent_runtime/persistence/checkpointers.py src/agent_runtime/chat.py src/agent_runtime/api/routes/sessions.py tests
git commit -m "feat: hard-delete chat sessions safely"
```

### Task 9: 后端 Stage 2 集成门禁

**Files:**
- Modify: `src/agent_runtime/chat.py`
- Modify: `src/agent_runtime/main.py`
- Modify: `tests/integration/test_stage_one_acceptance.py`
- Create: `tests/integration/test_stage_two_acceptance.py`
- Modify: `docs/tasks.md`

- [ ] **Step 1: 组装生产生命周期**

`open_chat_service` 必须初始化 Session 与 Feedback 表，创建一个
`ActiveRunRegistry`，并把同一生命周期内的 Parent Graph、三个 saver、
History、Feedback 和 CheckpointCleanup 注入 `ChatService`。应用 shutdown 时
先停止并等待全部 active Run，再关闭 saver。

- [ ] **Step 2: 写完整 Fake Model 验收**

```python
def test_stage_two_product_flow_with_fake_model() -> None:
    async def exercise() -> None:
        session_id = await create_streaming_session("介绍一下 LangGraph")
        await rename_session(session_id, "演示会话")
        assert first_session_title() == "演示会话"

        stopped_id = await start_and_stop(session_id)
        history = await list_all_messages(session_id)
        assert message_by_id(history, stopped_id).runtime_status == "stopped"

        completed_id = await send_and_complete(session_id, "继续回答")
        regenerated_id = await regenerate(session_id, completed_id)
        assert regenerated_id != completed_id
        assert completed_id not in active_history_ids(session_id)

        await feedback(regenerated_id, "like")
        assert active_feedback(regenerated_id) == "like"
        await delete_session(session_id)
        assert await get_messages_status(session_id) == 404

    asyncio.run(exercise())
```

- [ ] **Step 3: 运行默认全量回归**

Run: `$env:RUN_BAILIAN_SMOKE='0'; $env:RUN_POSTGRES_TESTS='0'; uv run pytest -q`
Expected: 所有默认测试 PASS，仅显式 PostgreSQL 与百炼 smoke 测试 SKIP。

- [ ] **Step 4: 运行 PostgreSQL 集成门禁**

Run: `$env:RUN_BAILIAN_SMOKE='0'; $env:RUN_POSTGRES_TESTS='1'; uv run pytest -q`
Expected: 所有 Fake Model 与 PostgreSQL 测试 PASS，只有真实百炼 smoke SKIP。

- [ ] **Step 5: 检查依赖、Schema 和阶段范围**

Run: `uv lock --check`
Expected: lock 文件有效。

Run: `uv pip check`
Expected: 所有已安装包兼容。

Run: `uv run python -m compileall src tests`
Expected: 编译成功。

Run: `rg -n "StateSnapshot|checkpoint_metadata|task_id|node_name" src/agent_runtime/api`
Expected: Product API Schema 与响应组装代码不包含内部字段。

- [ ] **Step 6: 更新任务证据并提交**

只把 S2-01～S2-08 更新为实际达到的 `DONE`；未完成前端和总体验收时不得把
Stage 2 标记为 `VERIFIED`。

```bash
git add src tests docs/tasks.md
git commit -m "test: verify stage two backend capabilities"
```
