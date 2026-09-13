# Stage 2 Chat Demo Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Stage 2 后端接口完成后，交付可由 FastAPI 直接打开、覆盖全部聊天产品能力的 React 演示页面。

**Architecture:** `frontend/` 保存 React/Vite 源码和前端测试，业务 UI 使用 Ant Design / Ant Design X 组合。Vite 将独立 HTML、哈希 JS 和 CSS 构建到 Python 包内的 `src/agent_runtime/static/`，FastAPI 同源提供页面和 API。

**Tech Stack:** React、Vite、Ant Design、Ant Design X、Ant Design X Markdown、Vitest、React Testing Library、Playwright、FastAPI

---

## File Structure

```text
frontend/
├── index.html
├── package.json
├── package-lock.json
├── vite.config.js
├── playwright.config.js
├── src/
│   ├── main.jsx
│   ├── App.jsx
│   ├── productStatus.js
│   ├── components/
│   │   ├── ChatWorkspace.jsx
│   │   ├── MessageList.jsx
│   │   ├── RuntimePanel.jsx
│   │   └── SessionSidebar.jsx
│   ├── hooks/
│   │   └── useChatController.js
│   ├── services/
│   │   ├── chatApi.js
│   │   └── sseClient.js
│   ├── styles/
│   │   ├── app.css
│   │   └── responsive.css
│   └── test/
│       └── setup.js
├── tests/
│   ├── api.test.js
│   ├── app.test.jsx
│   ├── chat.test.jsx
│   └── sessions.test.jsx
└── e2e/
    └── chat.spec.js

src/agent_runtime/
├── api/routes/frontend.py
└── static/
    ├── index.html
    └── assets/
        ├── *.js
        └── *.css
```

不得创建自研通用 UI 组件库。项目组件只组合业务行为；Bubble、Sender、
Conversations、Button、Modal、Tooltip、Alert、Drawer 和 Markdown 渲染均使用
第三方组件。

### Task 1: Vite 工程与 FastAPI 静态入口

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/vite.config.js`
- Create: `frontend/src/main.jsx`
- Create: `frontend/src/App.jsx`
- Create: `frontend/src/styles/app.css`
- Create: `frontend/src/styles/responsive.css`
- Create: `frontend/src/test/setup.js`
- Create: `src/agent_runtime/api/routes/frontend.py`
- Create: `tests/api/test_frontend_static.py`
- Modify: `src/agent_runtime/main.py`
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: 创建 React JavaScript 工程并安装开源依赖**

```bash
npm create vite@latest frontend -- --template react
cd frontend
npm install react react-dom antd @ant-design/icons @ant-design/x @ant-design/x-markdown
npm install --save-dev vite @vitejs/plugin-react vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event @playwright/test
```

保留 npm 生成的 `package-lock.json`。删除 Vite 示例图片和计数器组件，不引入
CDN。执行时以 lock 文件记录的版本为准，不在文档中复制易过期版本号。

- [ ] **Step 2: 配置独立静态构建**

```javascript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/agent_runtime/static',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    clearMocks: true,
  },
});
```

`package.json` scripts 固定为：

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "test": "vitest",
    "e2e": "playwright test"
  }
}
```

- [ ] **Step 3: 创建分离的 HTML、JSX 和 CSS 入口**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="description" content="AgentRuntime Stage 2 聊天演示" />
    <title>AgentRuntime</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

```javascript
import React from 'react';
import ReactDOM from 'react-dom/client';
import { XProvider } from '@ant-design/x';
import zhCNX from '@ant-design/x/locale/zh_CN';
import zhCN from 'antd/locale/zh_CN';

import App from './App';
import './styles/app.css';
import './styles/responsive.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <XProvider locale={{ ...zhCNX, ...zhCN }}>
      <App />
    </XProvider>
  </React.StrictMode>,
);
```

```javascript
export default function App() {
  return (
    <main className="app-shell" aria-label="AgentRuntime 聊天演示">
      <div>正在加载聊天页面…</div>
    </main>
  );
}
```

```css
:root {
  color: #182230;
  background: #f4f7fb;
  font-family:
    Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

* {
  box-sizing: border-box;
}

html,
body,
#root {
  min-width: 320px;
  min-height: 100%;
  margin: 0;
}

.app-shell {
  min-height: 100vh;
}
```

- [ ] **Step 4: 写 FastAPI 静态入口失败测试**

```python
def test_chat_page_and_assets_are_served() -> None:
    app = create_app(chat_service=FakeChatService())
    client = TestClient(app)

    page = client.get("/chat")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in page.text
    asset_path = extract_first_asset_path(page.text, suffix=".js")
    assert client.get(asset_path).status_code == 200
```

- [ ] **Step 5: 运行测试并确认失败**

Run: `uv run pytest tests/api/test_frontend_static.py -q`
Expected: FAIL，`/chat` 路由尚不存在。

- [ ] **Step 6: 实现 FastAPI 页面与资源路由**

```python
"""构建后聊天页面的同源静态资源入口。"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_ROOT = Path(__file__).resolve().parents[2] / "static"

router = APIRouter(include_in_schema=False)


@router.get("/chat")
async def chat_page() -> FileResponse:
    """返回 Stage 2 聊天演示页面入口。"""

    return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")
```

在 `create_app` 中确认 `STATIC_ROOT / "assets"` 存在后：

```python
application.mount(
    "/assets",
    StaticFiles(directory=STATIC_ROOT / "assets"),
    name="chat-assets",
)
application.include_router(frontend_router)
```

缺失构建产物时 `/chat` 返回中文 503
`FRONTEND_ASSETS_UNAVAILABLE`，应用启动和后端测试不得因此失败。

- [ ] **Step 7: 确保 wheel 包含静态资源**

构建后运行：

```bash
uv build
uv run python -c "from pathlib import Path; import zipfile; wheel=next(Path('dist').glob('*.whl')); names=zipfile.ZipFile(wheel).namelist(); assert any(name.endswith('agent_runtime/static/index.html') for name in names); assert any('/static/assets/' in name and name.endswith('.js') for name in names); assert any('/static/assets/' in name and name.endswith('.css') for name in names)"
```

在 `pyproject.toml` 明确加入静态目录，避免 wheel 结果依赖默认选择规则：

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/agent_runtime/static" = "agent_runtime/static"
```

该配置必须与 wheel 内容断言一起提交。

- [ ] **Step 8: 更新忽略规则**

```gitignore
frontend/node_modules/
frontend/coverage/
frontend/playwright-report/
frontend/test-results/
dist/
```

`src/agent_runtime/static/` 不得忽略，最终构建产物需要提交。

- [ ] **Step 9: 构建并运行 Task 1 测试**

Run: `cd frontend; npm run build`
Expected: `src/agent_runtime/static/index.html`、至少一个哈希 `.js` 和一个哈希
`.css` 文件存在，HTML 不包含完整业务脚本或样式。

Run: `uv run pytest tests/api/test_frontend_static.py -q`
Expected: PASS。

- [ ] **Step 10: 提交 Task 1**

```bash
git add frontend src/agent_runtime/api/routes/frontend.py src/agent_runtime/static src/agent_runtime/main.py tests/api/test_frontend_static.py pyproject.toml .gitignore
git commit -m "feat: scaffold stage two chat frontend"
```

### Task 2: JSON API 与 POST SSE 客户端

**Files:**
- Create: `frontend/src/services/chatApi.js`
- Create: `frontend/src/services/sseClient.js`
- Create: `frontend/tests/api.test.js`

- [ ] **Step 1: 写跨 chunk SSE 解析失败测试**

```javascript
import { describe, expect, it } from 'vitest';

import { parseSseStream } from '../src/services/sseClient';

describe('parseSseStream', () => {
  it('解析跨网络 chunk 的产品事件', async () => {
    const bytes = [
      'event: message\ndata: {"delta":"你",',
      '"session_id":"s","message_id":"m","capability_id":"general_chat"}\n\n',
      'event: done\ndata: {"session_id":"s","message_id":"m",',
      '"capability_id":"general_chat","status":"completed"}\n\n',
    ];
    const events = [];

    for await (const event of parseSseStream(streamFromStrings(bytes))) {
      events.push(event);
    }

    expect(events.map((event) => event.name)).toEqual(['message', 'done']);
    expect(events[0].data.delta).toBe('你');
  });
});
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd frontend; npm test -- --run tests/api.test.js`
Expected: FAIL，SSE 客户端尚不存在。

- [ ] **Step 3: 实现增量 SSE 解析器**

```javascript
export async function* parseSseStream(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n');
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (frame.trim()) {
        const lines = frame.split('\n');
        const name = lines
          .find((line) => line.startsWith('event:'))
          ?.slice('event:'.length)
          .trim();
        const dataText = lines
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice('data:'.length).trimStart())
          .join('\n');
        if (!name || !dataText) {
          throw new Error('服务器返回了无效的 SSE 事件');
        }
        yield { name, data: JSON.parse(dataText) };
      }
      boundary = buffer.indexOf('\n\n');
    }
    if (done) {
      if (buffer.trim()) {
        throw new Error('服务器提前结束了 SSE 响应');
      }
      return;
    }
  }
}
```

- [ ] **Step 4: 实现统一错误与 API 方法**

```javascript
export class ApiError extends Error {
  constructor(code, message, retryable = false, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail ?? payload;
    throw new ApiError(
      detail.code ?? 'HTTP_REQUEST_FAILED',
      detail.message ?? '请求失败',
      detail.retryable ?? false,
      response.status,
    );
  }
  return response.status === 204 ? null : response.json();
}

export const chatApi = {
  listSessions: ({ cursor, limit = 20 } = {}) =>
    requestJson(
      `/api/v1/chat/sessions?limit=${limit}${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
      }`,
    ),
  renameSession: (sessionId, title) =>
    requestJson(`/api/v1/chat/sessions/${sessionId}/rename`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteSession: (sessionId) =>
    requestJson(`/api/v1/chat/sessions/${sessionId}`, {
      method: 'DELETE',
    }),
  listMessages: ({ sessionId, before, limit = 50 }) =>
    requestJson(
      `/api/v1/chat/sessions/${sessionId}/messages?limit=${limit}${
        before ? `&before=${encodeURIComponent(before)}` : ''
      }`,
    ),
  stop: (sessionId) =>
    requestJson(`/api/v1/chat/sessions/${sessionId}/stop`, {
      method: 'POST',
    }),
  feedback: (messageId, action) =>
    requestJson(`/api/v1/chat/messages/${messageId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),
};
```

- [ ] **Step 5: 实现普通聊天和 Regenerate 流请求**

```javascript
async function* postEventStream(path, body, signal) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail ?? payload;
    throw new ApiError(
      detail.code ?? 'HTTP_REQUEST_FAILED',
      detail.message ?? '请求失败',
      detail.retryable ?? false,
      response.status,
    );
  }
  if (!response.body) {
    throw new ApiError(
      'SSE_BODY_MISSING',
      '服务器未返回流式响应',
      true,
      response.status,
    );
  }
  yield* parseSseStream(response.body);
}

export const streamChat = (payload, signal) =>
  postEventStream('/api/v1/chat/completions', payload, signal);

export const streamRegeneration = (sessionId, messageId, signal) =>
  postEventStream(
    `/api/v1/chat/sessions/${sessionId}/messages/${messageId}/regenerate`,
    undefined,
    signal,
  );
```

- [ ] **Step 6: 运行 Task 2 测试**

Run: `cd frontend; npm test -- --run tests/api.test.js`
Expected: PASS，并覆盖 HTTP 409 `SESSION_BUSY`、HTTP 404、error→done(failed)、
CRLF 和连接提前结束。

- [ ] **Step 7: 提交 Task 2**

```bash
git add frontend/src/services frontend/tests/api.test.js frontend/package-lock.json
git commit -m "feat: add stage two frontend api client"
```

### Task 3: Session 侧栏与页面状态控制

**Files:**
- Create: `frontend/src/hooks/useChatController.js`
- Create: `frontend/src/components/SessionSidebar.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/styles/app.css`
- Modify: `frontend/src/styles/responsive.css`
- Create: `frontend/tests/sessions.test.jsx`

- [ ] **Step 1: 写 Session 交互失败测试**

```javascript
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import App from '../src/App';
import { chatApi } from '../src/services/chatApi';

vi.mock('../src/services/chatApi');

beforeEach(() => {
  chatApi.listSessions.mockResolvedValue({
    items: [
      {
        session_id: 'session-1',
        title: '第一个会话',
        created_at: '2026-09-13T00:00:00Z',
        updated_at: '2026-09-13T01:00:00Z',
      },
    ],
    next_cursor: 'next-page',
  });
  chatApi.listMessages.mockResolvedValue({
    items: [],
    next_before: null,
  });
});

it('加载、选择并新建空白会话', async () => {
  const user = userEvent.setup();
  render(<App />);

  await user.click(await screen.findByText('第一个会话'));
  expect(chatApi.listMessages).toHaveBeenCalledWith(
    expect.objectContaining({ sessionId: 'session-1' }),
  );

  await user.click(screen.getByRole('button', { name: '新建会话' }));
  expect(screen.getByText('开始新的对话')).toBeInTheDocument();
  expect(chatApi.createSession).toBeUndefined();
});
```

另写测试覆盖“加载更多”、改名 trim、删除二次确认、删除当前会话后进入空白新
会话，以及 Run 期间禁止切换或删除当前 Session。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd frontend; npm test -- --run tests/sessions.test.jsx`
Expected: FAIL，页面控制器和 SessionSidebar 尚不存在。

- [ ] **Step 3: 实现后端权威的控制器初始状态**

```javascript
import { useCallback, useEffect, useRef, useState } from 'react';

import { chatApi } from '../services/chatApi';

export function useChatController() {
  const [sessions, setSessions] = useState([]);
  const [sessionCursor, setSessionCursor] = useState(null);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [messageCursor, setMessageCursor] = useState(null);
  const [run, setRun] = useState({
    phase: 'idle',
    sessionId: null,
    messageId: null,
    capabilityId: null,
    finalStatus: null,
  });
  const [error, setError] = useState(null);
  const abortRef = useRef(null);

  const loadSessions = useCallback(async ({ append = false } = {}) => {
    const page = await chatApi.listSessions({
      cursor: append ? sessionCursor : null,
    });
    setSessions((current) =>
      append ? [...current, ...page.items] : page.items,
    );
    setSessionCursor(page.next_cursor);
  }, [sessionCursor]);

  useEffect(() => {
    loadSessions().catch(setError);
  }, []);

  return {
    sessions,
    sessionCursor,
    activeSessionId,
    messages,
    messageCursor,
    run,
    error,
    abortRef,
    setError,
    setMessages,
    setMessageCursor,
    setRun,
    loadSessions,
    setActiveSessionId,
    setSessions,
  };
}
```

在同一 hook 中加入以下 Session 操作：

```javascript
const selectSession = useCallback(async (sessionId) => {
  if (run.phase === 'streaming') return;
  const page = await chatApi.listMessages({ sessionId, limit: 50 });
  setActiveSessionId(sessionId);
  setMessages(page.items);
  setMessageCursor(page.next_before);
  setError(null);
}, [run.phase]);

const newSession = useCallback(() => {
  if (run.phase === 'streaming') return;
  setActiveSessionId(null);
  setMessages([]);
  setMessageCursor(null);
  setError(null);
}, [run.phase]);

const renameSession = useCallback(async (sessionId, title) => {
  const renamed = await chatApi.renameSession(sessionId, title);
  setSessions((current) =>
    current.map((session) =>
      session.session_id === sessionId ? renamed : session,
    ),
  );
}, []);

const deleteSession = useCallback(async (sessionId) => {
  await chatApi.deleteSession(sessionId);
  if (sessionId === activeSessionId) {
    abortRef.current?.abort();
    setActiveSessionId(null);
    setMessages([]);
    setMessageCursor(null);
    setRun({
      phase: 'idle',
      sessionId: null,
      messageId: null,
      capabilityId: null,
      finalStatus: null,
    });
  }
  await loadSessions();
}, [activeSessionId, loadSessions]);

const loadOlderMessages = useCallback(async () => {
  if (!activeSessionId || !messageCursor) return;
  const page = await chatApi.listMessages({
    sessionId: activeSessionId,
    before: messageCursor,
    limit: 50,
  });
  setMessages((current) => [
    ...page.items,
    ...current.filter(
      (message) =>
        !page.items.some((older) => older.message_id === message.message_id),
    ),
  ]);
  setMessageCursor(page.next_before);
}, [activeSessionId, messageCursor]);
```

将这五个函数加入 hook 返回对象。每个 mutation 成功后使用 API 返回值或重新
加载列表，不在前端伪造后端最终状态。

- [ ] **Step 4: 使用 Conversations 组合 SessionSidebar**

```javascript
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { Button, Input, Modal, Space } from 'antd';
import { Conversations } from '@ant-design/x';
import { useState } from 'react';

export default function SessionSidebar({
  sessions,
  activeSessionId,
  hasMore,
  running,
  onSelect,
  onCreate,
  onLoadMore,
  onRename,
  onDelete,
}) {
  const [renameTarget, setRenameTarget] = useState(null);
  const [title, setTitle] = useState('');

  const menu = (conversation) => ({
    items: [
      { key: 'rename', label: '重命名', icon: <EditOutlined /> },
      { key: 'delete', label: '删除', icon: <DeleteOutlined />, danger: true },
    ],
    onClick: ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'rename') {
        setRenameTarget(conversation);
        setTitle(conversation.label);
      } else {
        Modal.confirm({
          title: '删除会话？',
          content: '会话消息、运行状态和反馈将被永久删除。',
          okText: '删除',
          cancelText: '取消',
          okButtonProps: { danger: true },
          onOk: () => onDelete(conversation.key),
        });
      }
    },
  });

  return (
    <aside className="session-sidebar" aria-label="会话列表">
      <Button
        type="primary"
        icon={<PlusOutlined />}
        disabled={running}
        onClick={onCreate}
        block
      >
        新建会话
      </Button>
      <Conversations
        items={sessions.map((session) => ({
          key: session.session_id,
          label: session.title,
          disabled: running && session.session_id !== activeSessionId,
        }))}
        activeKey={activeSessionId}
        onActiveChange={onSelect}
        menu={menu}
      />
      {hasMore && <Button onClick={onLoadMore}>加载更多</Button>}
      <Modal
        title="重命名会话"
        open={Boolean(renameTarget)}
        okText="保存"
        cancelText="取消"
        onCancel={() => setRenameTarget(null)}
        onOk={async () => {
          await onRename(renameTarget.key, title);
          setRenameTarget(null);
        }}
      >
        <Space direction="vertical" className="dialog-fields">
          <Input
            aria-label="会话标题"
            value={title}
            maxLength={100}
            onChange={(event) => setTitle(event.target.value)}
          />
        </Space>
      </Modal>
    </aside>
  );
}
```

- [ ] **Step 5: 组装双栏 App**

```javascript
export default function App() {
  const controller = useChatController();
  const running = controller.run.phase === 'streaming';

  return (
    <main className="app-shell">
      <SessionSidebar
        sessions={controller.sessions}
        activeSessionId={controller.activeSessionId}
        hasMore={Boolean(controller.sessionCursor)}
        running={running}
        onSelect={controller.selectSession}
        onCreate={controller.newSession}
        onLoadMore={() => controller.loadSessions({ append: true })}
        onRename={controller.renameSession}
        onDelete={controller.deleteSession}
      />
      <section className="chat-panel" aria-label="聊天内容">
        {controller.activeSessionId || controller.messages.length > 0
          ? <div>聊天内容</div>
          : <div className="empty-chat">开始新的对话</div>}
      </section>
    </main>
  );
}
```

- [ ] **Step 6: 完成响应式 CSS**

```css
.app-shell {
  display: grid;
  grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
}

.session-sidebar {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 16px;
  overflow: auto;
  background: #ffffff;
  border-right: 1px solid #e8edf3;
}

.chat-panel {
  min-width: 0;
  overflow: hidden;
}

.dialog-fields {
  width: 100%;
}
```

```css
@media (max-width: 720px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .session-sidebar {
    max-height: 38vh;
    border-right: 0;
    border-bottom: 1px solid #e8edf3;
  }
}
```

- [ ] **Step 7: 运行 Task 3 测试**

Run: `cd frontend; npm test -- --run tests/sessions.test.jsx`
Expected: PASS。

- [ ] **Step 8: 提交 Task 3**

```bash
git add frontend/src frontend/tests/sessions.test.jsx
git commit -m "feat: add chat session sidebar"
```

### Task 4: 流式聊天、Stop 与历史加载

**Files:**
- Create: `frontend/src/components/ChatWorkspace.jsx`
- Create: `frontend/src/components/MessageList.jsx`
- Modify: `frontend/src/hooks/useChatController.js`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/styles/app.css`
- Create: `frontend/tests/chat.test.jsx`

- [ ] **Step 1: 写流式累加和 Stop 失败测试**

```javascript
it('累加 message 事件并等待 stopped 终态', async () => {
  streamChat.mockImplementation(async function* () {
    yield {
      name: 'message',
      data: {
        session_id: 'session-1',
        message_id: 'assistant-1',
        capability_id: 'general_chat',
        delta: '你',
      },
    };
    yield {
      name: 'message',
      data: {
        session_id: 'session-1',
        message_id: 'assistant-1',
        capability_id: 'general_chat',
        delta: '好',
      },
    };
    yield {
      name: 'done',
      data: {
        session_id: 'session-1',
        message_id: 'assistant-1',
        capability_id: 'general_chat',
        status: 'completed',
      },
    };
  });

  const user = userEvent.setup();
  render(<App />);
  await user.type(screen.getByRole('textbox'), '你好');
  await user.click(screen.getByRole('button', { name: '发送' }));

  expect(await screen.findByText('你好')).toBeInTheDocument();
  expect(await screen.findByText('已完成')).toBeInTheDocument();
});
```

另写一个可控异步生成器：发送第一个 delta 后暂停，点击“停止生成”，断言
`chatApi.stop(sessionId)` 已调用，并且按钮保持 loading，直到流收到
`done(status="stopped")`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd frontend; npm test -- --run tests/chat.test.jsx`
Expected: FAIL，聊天工作区和流控制尚不存在。

- [ ] **Step 3: 实现 sendMessage 状态机**

```javascript
const sendMessage = useCallback(async (content) => {
  const normalized = content.trim();
  if (!normalized || run.phase === 'streaming') return;

  const controller = new AbortController();
  abortRef.current = controller;
  const human = {
    message_id: crypto.randomUUID(),
    role: 'user',
    content: normalized,
    runtime_status: null,
    capability_id: null,
    feedback: null,
  };
  setMessages((current) => [...current, human]);
  setRun({
    phase: 'streaming',
    sessionId: activeSessionId,
    messageId: null,
    capabilityId: null,
    finalStatus: null,
  });

  try {
    for await (const event of streamChat(
      {
        session_id: activeSessionId,
        message: { content: normalized },
      },
      controller.signal,
    )) {
      applyProductEvent(event);
    }
  } catch (caught) {
    if (caught.name !== 'AbortError') setError(caught);
  } finally {
    abortRef.current = null;
    await refreshFromServerAfterRun();
  }
}, [activeSessionId, run.phase]);
```

客户端临时 Human UUID 只用于 React key，不得发送给服务端，也不得在刷新后保留。
第一个包含 `session_id` 的 SSE 事件到达后替换 active Session；Run 结束后重新
读取历史，以服务端 HumanMessage UUID 覆盖临时消息。

- [ ] **Step 4: 实现产品事件归并**

```javascript
function applyProductEvent(event) {
  const data = event.data;
  setActiveSessionId((current) => current ?? data.session_id);
  if (event.name === 'message') {
    setMessages((current) =>
      upsertAssistantDelta(current, {
        messageId: data.message_id,
        delta: data.delta,
        capabilityId: data.capability_id,
      }),
    );
    setRun({
      phase: 'streaming',
      sessionId: data.session_id,
      messageId: data.message_id,
      capabilityId: data.capability_id,
      finalStatus: null,
    });
  } else if (event.name === 'error') {
    setError(new ApiError(data.code, data.message, data.retryable));
  } else if (event.name === 'done') {
    setRun({
      phase: 'idle',
      sessionId: data.session_id,
      messageId: data.message_id,
      capabilityId: data.capability_id,
      finalStatus: data.status,
    });
  }
}
```

- [ ] **Step 5: 实现 ChatWorkspace**

```javascript
import { Sender } from '@ant-design/x';
import { Alert } from 'antd';
import { useState } from 'react';

import MessageList from './MessageList';

export default function ChatWorkspace({
  messages,
  messageCursor,
  run,
  error,
  onLoadOlder,
  onSend,
  onStop,
}) {
  const [draft, setDraft] = useState('');
  const running = run.phase === 'streaming';

  return (
    <section className="chat-workspace" aria-label="聊天内容">
      {error && <Alert type="error" showIcon message={error.message} />}
      <MessageList
        messages={messages}
        hasOlder={Boolean(messageCursor)}
        onLoadOlder={onLoadOlder}
      />
      <Sender
        value={draft}
        onChange={setDraft}
        onSubmit={(value) => {
          onSend(value);
          setDraft('');
        }}
        onCancel={onStop}
        loading={running}
        disabled={!running && !draft.trim()}
        placeholder="输入消息，Enter 发送"
      />
    </section>
  );
}
```

- [ ] **Step 6: 实现 Stop 和卸载断开**

```javascript
const stop = useCallback(async () => {
  if (run.phase !== 'streaming' || !run.sessionId) return;
  try {
    await chatApi.stop(run.sessionId);
  } catch (caught) {
    setError(caught);
  }
}, [run]);

useEffect(() => () => {
  abortRef.current?.abort();
}, []);
```

Stop 成功后不主动 Abort SSE；继续读取直到 `done(stopped)`，保证界面展示后端
真实终态。只有组件卸载或页面断开才 Abort。

- [ ] **Step 7: 运行 Task 4 测试**

Run: `cd frontend; npm test -- --run tests/chat.test.jsx`
Expected: PASS，并覆盖 completed、unsupported、stopped、failed、
`SESSION_BUSY`、加载更早历史和新 Session 首事件回填。

- [ ] **Step 8: 提交 Task 4**

```bash
git add frontend/src frontend/tests/chat.test.jsx
git commit -m "feat: add streaming chat and stop controls"
```

### Task 5: Markdown、Feedback、Regenerate 与运行状态面板

**Files:**
- Modify: `frontend/src/components/MessageList.jsx`
- Create: `frontend/src/components/RuntimePanel.jsx`
- Create: `frontend/src/productStatus.js`
- Modify: `frontend/src/components/ChatWorkspace.jsx`
- Modify: `frontend/src/hooks/useChatController.js`
- Modify: `frontend/src/styles/app.css`
- Create: `frontend/tests/app.test.jsx`

- [ ] **Step 1: 写安全 Markdown 与操作资格失败测试**

```javascript
it('转义原始 HTML 并只给最新 completed 回答显示重新生成', async () => {
  chatApi.listMessages.mockResolvedValue({
    items: [
      {
        message_id: 'a1',
        role: 'assistant',
        content: '<script>window.pwned = true</script>旧回答',
        runtime_status: 'completed',
        capability_id: 'general_chat',
        feedback: null,
      },
      {
        message_id: 'a2',
        role: 'assistant',
        content: '[安全链接](https://example.com)',
        runtime_status: 'completed',
        capability_id: 'general_chat',
        feedback: 'like',
      },
    ],
    next_before: null,
  });

  render(<App />);
  await selectSession('session-1');

  expect(document.querySelector('script')).toBeNull();
  expect(screen.getByText(/<script>/)).toBeInTheDocument();
  expect(screen.getAllByRole('button', { name: '重新生成' })).toHaveLength(1);
  expect(screen.getByRole('link', { name: '安全链接' })).toHaveAttribute(
    'rel',
    expect.stringContaining('noopener'),
  );
});
```

另写测试覆盖 Feedback 的 like→dislike→cancel、unsupported/stopped 无 Feedback、
Regenerate 期间禁用发送，以及状态面板显示三个 ID 和最终状态。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd frontend; npm test -- --run tests/app.test.jsx`
Expected: FAIL，消息动作和运行状态面板尚未实现。

- [ ] **Step 3: 使用 XMarkdown 安全渲染 AI 内容**

```javascript
import { Actions, Bubble } from '@ant-design/x';
import { XMarkdown } from '@ant-design/x-markdown';
import { Button, Tag } from 'antd';

import { STATUS_LABELS } from '../productStatus';

function SafeMarkdown({ content, streaming }) {
  return (
    <XMarkdown
      content={content}
      escapeRawHtml
      openLinksInNewTab
      streaming={{ hasNextChunk: streaming }}
      components={{
        a: ({ href, children, ...props }) => (
          <a
            {...props}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
          >
            {children}
          </a>
        ),
      }}
    />
  );
}
```

不得使用 `dangerouslySetInnerHTML`。代码块和语法高亮使用 XMarkdown 内置能力，
不自研 Markdown parser。

- [ ] **Step 4: 实现 MessageList 与 Actions**

```javascript
export default function MessageList({
  messages,
  hasOlder,
  streamingMessageId,
  onLoadOlder,
  onFeedback,
  onRegenerate,
}) {
  const lastAssistant = [...messages]
    .reverse()
    .find((message) => message.role === 'assistant');
  const regeneratableId =
    lastAssistant?.runtime_status === 'completed'
      ? lastAssistant.message_id
      : null;

  return (
    <div className="message-list" aria-live="polite">
      {hasOlder && <Button onClick={onLoadOlder}>加载更早消息</Button>}
      {messages.map((message) => {
        const assistant = message.role === 'assistant';
        const completed = message.runtime_status === 'completed';
        return (
          <article key={message.message_id}>
            <Bubble
              placement={assistant ? 'start' : 'end'}
              content={
                assistant ? (
                  <SafeMarkdown
                    content={message.content}
                    streaming={message.message_id === streamingMessageId}
                  />
                ) : (
                  message.content
                )
              }
            />
            {assistant && (
              <div className="message-footer">
                <Tag>{STATUS_LABELS[message.runtime_status]}</Tag>
                {completed && (
                  <Actions.Feedback
                    value={message.feedback ?? 'default'}
                    onChange={(value) =>
                      onFeedback(
                        message.message_id,
                        value === 'default' ? 'cancel' : value,
                      )
                    }
                  />
                )}
                {message.message_id === regeneratableId && (
                  <Button
                    type="text"
                    onClick={() => onRegenerate(message.message_id)}
                  >
                    重新生成
                  </Button>
                )}
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 5: 实现 Feedback 与 Regenerate 控制器动作**

```javascript
const setFeedback = useCallback(async (messageId, action) => {
  const result = await chatApi.feedback(messageId, action);
  setMessages((current) =>
    current.map((message) =>
      message.message_id === messageId
        ? { ...message, feedback: result.feedback }
        : message,
    ),
  );
}, []);

const regenerate = useCallback(async (messageId) => {
  if (!activeSessionId || run.phase === 'streaming') return;
  const controller = new AbortController();
  abortRef.current = controller;
  setRun({
    phase: 'streaming',
    sessionId: activeSessionId,
    messageId: null,
    capabilityId: null,
    finalStatus: null,
  });
  try {
    for await (const event of streamRegeneration(
      activeSessionId,
      messageId,
      controller.signal,
    )) {
      applyProductEvent(event);
    }
  } catch (caught) {
    if (caught.name !== 'AbortError') setError(caught);
  } finally {
    abortRef.current = null;
    await refreshFromServerAfterRun();
  }
}, [activeSessionId, run.phase]);
```

Regenerate 使用独立 `AbortController`，不复用已结束普通聊天的 signal。

- [ ] **Step 6: 实现可折叠运行状态面板**

```javascript
import { Collapse, Descriptions, Tag } from 'antd';

import { STATUS_LABELS } from '../productStatus';
```

`frontend/src/productStatus.js`：

```javascript
export const STATUS_LABELS = {
  completed: '已完成',
  unsupported: '不支持',
  incomplete: '未完成',
  stopped: '已停止',
  failed: '失败',
};
```

`RuntimePanel.jsx`：

```javascript
export default function RuntimePanel({ run }) {
  return (
    <Collapse
      className="runtime-panel"
      items={[
        {
          key: 'runtime',
          label: '运行状态',
          children: (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="Session ID">
                {run.sessionId ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="Message ID">
                {run.messageId ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="Capability">
                {run.capabilityId ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag>
                  {run.phase === 'streaming'
                    ? '生成中'
                    : STATUS_LABELS[run.finalStatus] ?? '空闲'}
                </Tag>
              </Descriptions.Item>
            </Descriptions>
          ),
        },
      ]}
    />
  );
}
```

面板不得增加原始事件 JSON、提示词、节点名、checkpoint 或 tasks。

- [ ] **Step 7: 运行 Task 5 测试**

Run: `cd frontend; npm test -- --run tests/app.test.jsx tests/chat.test.jsx`
Expected: PASS。

Run: `cd frontend; rg -n "dangerouslySetInnerHTML|StateSnapshot|checkpoint|node_name|task_id" src`
Expected: 无匹配。

- [ ] **Step 8: 提交 Task 5**

```bash
git add frontend/src frontend/tests/app.test.jsx
git commit -m "feat: add message actions and runtime status"
```

### Task 6: 前端构建、浏览器验收与文档

**Files:**
- Create: `frontend/playwright.config.js`
- Create: `frontend/e2e/chat.spec.js`
- Create: `tests/e2e_support/__init__.py`
- Create: `tests/e2e_support/app.py`
- Create: `tests/fakes/stage_two_model.py`
- Modify: `tests/api/test_frontend_static.py`
- Modify: `README.md`
- Modify: `docs/tasks.md`

- [ ] **Step 1: 配置可选 Playwright 双服务**

```javascript
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig } from '@playwright/test';

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const projectDir = path.resolve(frontendDir, '..');

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command:
        'uv run uvicorn tests.e2e_support.app:app --host 127.0.0.1 --port 8000',
      cwd: projectDir,
      url: 'http://127.0.0.1:8000/health',
      reuseExistingServer: true,
      env: { ...process.env, RUN_POSTGRES_TESTS: '1' },
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      cwd: frontendDir,
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: true,
    },
  ],
});
```

`tests/e2e_support/app.py` 使用 `open_chat_service(Settings(), model=FakeModel())`
组装真实 FastAPI 和 PostgreSQL 生命周期。Fake Model 必须按固定 chunk 输出，并
提供一个等待事件，保证 Playwright 能可靠点击 Stop；不得读取百炼配置。

- [ ] **Step 2: 写完整浏览器流程**

```javascript
import { expect, test } from '@playwright/test';

test('完整聊天产品流程', async ({ page }) => {
  await page.goto('/chat');
  await page.getByRole('textbox').fill('介绍一下 LangGraph');
  await page.getByRole('button', { name: '发送' }).click();
  await expect(page.getByText('已完成')).toBeVisible();
  await expect(page.getByText('general_chat')).toBeVisible();

  await page.getByRole('button', { name: '重新生成' }).click();
  await expect(page.getByText('已完成')).toBeVisible();

  await page.getByRole('button', { name: '赞' }).click();
  await page.reload();
  await expect(page.getByRole('button', { name: '取消赞' })).toBeVisible();

  await page.getByRole('textbox').fill('生成一个慢速回答');
  await page.getByRole('button', { name: '发送' }).click();
  await page.getByRole('button', { name: '停止生成' }).click();
  await expect(page.getByText('已停止')).toBeVisible();

  await page.getByRole('button', { name: '会话操作' }).click();
  await page.getByText('删除').click();
  await page.getByRole('button', { name: '删除' }).click();
  await expect(page.getByText('开始新的对话')).toBeVisible();
});
```

业务操作触发器必须显式设置稳定中文 `aria-label`，Playwright locator 只使用
role 和该名称，不得依赖 Ant Design X 内部 CSS 类名。

- [ ] **Step 3: 运行前端默认门禁**

Run: `cd frontend; npm test -- --run`
Expected: 全部 Vitest / React Testing Library 测试 PASS。

Run: `cd frontend; npm run build`
Expected: 构建成功。

Run: `uv run pytest tests/api/test_frontend_static.py -q`
Expected: PASS。

- [ ] **Step 4: 检查资源拆分**

Run: `uv run python -c "from pathlib import Path; root=Path('src/agent_runtime/static'); html=(root/'index.html').read_text(encoding='utf-8'); assert '<script type=\"module\"' in html; assert '/assets/' in html; assert list((root/'assets').glob('*.js')); assert list((root/'assets').glob('*.css')); assert len(html) < 20000"`
Expected: HTML 仅保留入口结构，JS 和 CSS 为独立文件。

- [ ] **Step 5: 运行可选真实浏览器门禁**

先确保项目根目录 `.env` 的 `DATABASE_URL` 指向测试 PostgreSQL；该流程不需要
`DASHSCOPE_API_KEY`。

Run: `cd frontend; npx playwright install chromium`
Expected: Chromium 测试运行时安装成功。

Run: `cd frontend; npm run e2e`
Expected: 完整聊天产品流程 PASS，失败时保留 trace。

- [ ] **Step 6: 更新 README 与任务证据**

README 必须包含：

```text
uv sync
uv run python -m agent_runtime
打开 http://127.0.0.1:8000/chat

前端开发：
cd frontend
npm install
npm run dev
```

`docs/tasks.md` 记录 Vitest、build、静态路由、可选 Playwright 的实际测试数量
与结果。只有 S2-01～S2-09 全部完成时才进入 S2-10 总体验收。

- [ ] **Step 7: 最终全量验证**

Run: `$env:RUN_BAILIAN_SMOKE='0'; $env:RUN_POSTGRES_TESTS='0'; uv run pytest -q`
Expected: 默认 Python 测试全部 PASS。

Run: `cd frontend; npm test -- --run; npm run build`
Expected: 前端测试和构建全部 PASS。

Run: `git status --short`
Expected: 只包含本计划明确列出的 Stage 2 文件，不包含 `.env`、
`node_modules`、Playwright 报告或无关用户文件。

- [ ] **Step 8: 提交 Task 6**

```bash
git add frontend src/agent_runtime/static src/agent_runtime/api/routes/frontend.py src/agent_runtime/main.py tests README.md docs/tasks.md
git commit -m "feat: deliver stage two chat demo"
```

## Execution Order

必须先完整执行
`docs/superpowers/plans/2026-09-13-stage-2-backend.md`，确认 S2-01～S2-08
达到 DONE，再执行本计划。S2-10 验收完成且负责人书面确认前，不得进入 Stage 3。
