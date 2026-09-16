# AgentRuntime Architecture

本文档描述 **How：AgentRuntime 应如何实现**。

需求边界见 `requirements.md`。
设计裁决见 `decisions.md`。

---

# 1. 总体架构

Stage 1 运行基线：

```text
单用户可信环境
Python 3.12
uv
阿里云百炼 OpenAI 兼容接口
Fake Model 自动化测试
```

Stage 1：

```text
Client
  ↓
FastAPI
  ↓
Chat Application Layer
  ↓
Parent LangGraph
  ├── Router
  └── Capability Dispatch
         ↓
     Child Agent
     ├── general_chat
     └── en_to_zh
         ↓
PostgreSQL Checkpointer
```

流式返回：

```text
Child Agent Stream
→ Parent Graph Stream
→ FastAPI Adapter
→ SSE
→ Client
```

控制结果：

```text
Child Agent
→ ChildResult
→ Parent Graph
```

---

# 2. 技术职责

## FastAPI

负责：

- HTTP API
- SSE
- Session 请求入口
- 请求参数校验
- 固定本地 user_id 注入
- API 错误转换
- LangGraph stream → 产品 SSE 协议

FastAPI 不负责：

- 业务意图判断
- 子 Agent 业务状态
- Child Graph 内部流程

## LangGraph Parent Graph

负责：

- 会话级调度
- 当前 Capability
- Router
- Child Agent 调用
- `OUT_OF_SCOPE` 回流

## LangChain create_agent

Stage 1 用于实现 `general_chat`。

目的是验证 Parent Graph 可以调度高层 Agent Harness，而不仅是 StateGraph。

## LangGraph StateGraph

Stage 1 用于：

- Parent Graph
- `en_to_zh`

目的是验证不同实现形式的 Capability 可以被同一个 Parent 调度。

## PostgreSQL

Stage 1 负责：

- Session 业务元数据
- Parent Checkpointer 持久化
- Child Checkpointer 持久化

当前只使用一个 PostgreSQL 数据库。

---

# 3. 推荐代码结构

```text
src/agent_runtime/
├── api/
│   ├── routes/
│   │   ├── health.py
│   │   └── chat.py
│   └── schemas/
│
├── core/
│   ├── config.py
│   ├── logging.py
│   └── errors.py
│
├── sessions/
│   ├── models.py
│   ├── repository.py
│   └── service.py
│
├── graph/
│   ├── state.py
│   ├── context.py
│   ├── router.py
│   ├── parent.py
│   └── child_result.py
│
├── capabilities/
│   ├── general_chat/
│   │   ├── agent.py
│   │   └── adapter.py
│   └── en_to_zh/
│       ├── state.py
│       ├── graph.py
│       └── adapter.py
│
├── persistence/
│   ├── database.py
│   └── checkpointers.py
│
├── streaming/
│   └── sse.py
│
└── main.py
```

Stage 1 不为了未来 Capability 平台提前创建：

```text
registry/
manifest/
permission/
admin/
remote_executor/
```

这些属于 Stage 3 或 Future。

---

# 4. Parent State

Stage 1 最小 Parent State：

```python
messages
resolved_capability_id
rejected_capability_ids
```

`messages` 是完整公共对话的唯一权威来源。

`resolved_capability_id` 表示当前 Session 已确定使用的 Capability。

后续消息优先恢复该状态，不重新进行 Router。

`rejected_capability_ids` 只用于当前 HumanMessage 的有限回流控制。每个新请求
开始时必须重置；它是 Parent 控制状态，不是 Child 业务状态。

## 4.1 Context Builder

Context Builder 从 Parent `messages` 派生 Child 本轮消息视图：

```text
当前 HumanMessage 之前的已完成轮次 < 10
→ 全部已完成轮次
→ 当前 HumanMessage

当前 HumanMessage 之前的已完成轮次 >= 10
→ 最近 5 个完整 Human + AI 轮次
→ 当前 HumanMessage
```

System Message 始终保留且不计入轮数。`unsupported` 和 `incomplete` 消息
保留在 Parent 公共历史中，但不计入完整轮次，也不进入后续模型上下文。
裁剪只影响模型输入，不修改 Parent 完整历史。

Child Checkpointer 中的消息只是派生执行快照。每次调用必须由 Adapter 按
Parent 上下文刷新，禁止与已有 Child 消息简单累加而造成重复。

---

# 5. Router 设计

Stage 1 Router 输入：

```text
最新 HumanMessage
+ 本轮尚未拒绝该消息的固定 Capability 描述
```

输出：

```text
capability_id
confidence
```

必须使用结构化输出模型，而不是自由文本解析。

输出必须同时通过 Schema 校验和候选集校验。非法输出属于 Router 失败。

Stage 1 只允许：

```text
general_chat
en_to_zh
```

Router 不承担子 Agent 的最终能力边界判断。

---

# 6. Capability 调用模型

Parent Graph 不直接依赖 Child 内部 State。

通过 Adapter 做转换：

```text
Parent State
→ Capability Adapter
→ Capability 范围守卫
→ 接受后构造 Child Input
→ Child Agent
→ token delta 进入 Stream 数据面
→ 最终 AIMessage 写回 Parent messages
→ ChildResult 进入 Parent 控制面
```

Stage 1 两个 Child 可以使用不同内部实现和不同 State Schema。

范围守卫属于 Capability 自身边界，但必须发生在 Child 用户可见生成和业务
消息持久化之前。拒绝时不输出 token，也不推进 Child 消息历史。

`general_chat` 通过 System Prompt 要求简洁直接，输出 token 上限仅作为宽松
保护，不做二次模型压缩。

`en_to_zh` 只翻译当前 HumanMessage，历史上下文只辅助语境、代词和术语
一致性；不得为了简短而删减原文。

---

# 7. ChildResult

Stage 1：

```python
status: str
control_signal: str | None
```

建议状态：

```text
completed
rejected
failed
```

控制信号：

```text
OUT_OF_SCOPE
```

原则：

- `OUT_OF_SCOPE` = 能力边界不匹配
- `failed` = 实际执行失败
- `ChildResult` 只属于控制面，不包含完整用户可见回复
- 最终公共 `AIMessage` 通过数据面写回 Parent

二者不可混用。

---

# 8. OUT_OF_SCOPE 流程

```text
用户消息
→ 当前 Capability
→ Child 判断越界
→ ChildResult(OUT_OF_SCOPE)
→ 记录 rejected_capability_ids
→ Parent Router 只看未拒绝候选
→ 对同一条 HumanMessage 重新路由
→ 新 Capability
```

必须保证同一条用户消息不会因为回流被重复写入公共 messages。

需要通过测试验证。

每个 Capability 对同一条 HumanMessage 最多尝试一次。所有候选均拒绝时：

```text
Parent 生成固定简短 unsupported AIMessage
→ 写入公共 messages
→ 由 Parent 发送 message 事件
→ SSE done(status="unsupported")
```

unsupported 不是运行错误，不发送 `error`。

---

# 9. Thread / Checkpointer

## Parent

```text
thread_id = session_id
```

保存：

```text
messages
resolved_capability_id
Parent Graph runtime state
```

## Child

```text
thread_id = {session_id}:{capability_id}
```

例如：

```text
S001:general_chat
S001:en_to_zh
```

Child Checkpointer 与 Parent Checkpointer 必须逻辑独立。

Child A 与 Child B 也必须逻辑独立。

Stage 1 可以共享同一个 PostgreSQL 数据库实例。

Parent 恢复后必须保留完整公共消息和当前 Capability。Child 每次调用的消息
输入由 Parent 重新派生，独立 Checkpointer 不得成为第二套公共历史。

---

# 10. Session 创建流程

没有 `session_id`：

```text
POST /api/v1/chat/completions
→ 创建 Session
→ 生成 session_id
→ user_id = 配置中的固定本地用户
→ title = 第一条 HumanMessage 截取
→ 为 HumanMessage 分配稳定 UUID
→ 使用 session_id 作为 Parent thread_id
→ 执行 Parent Graph
```

如果 Router 或 Child 失败：

```text
Session 保留
HumanMessage 保留
```

---

# 11. SSE 架构

LangGraph 内部事件属于内部 Runtime 协议。

FastAPI 负责转换。

Stage 1 对外：

```text
event: message
data: {"session_id": "...", "message_id": "...", "delta": "..."}

event: error
data: {"session_id": "...", "code": "...", "message": "...", "retryable": false}

event: done
data: {"session_id": "...", "status": "completed|unsupported|failed"}
```

不要将 LangGraph 原始 node/update/checkpoint 直接暴露给前端。

尚未建立 SSE 时发现请求或 Session 不合法，使用标准 HTTP 4xx JSON。已经
建立 SSE 后发生运行错误，发送 `error`，随后发送 `done(status="failed")`。

模型已流出部分 token 后失败时，FastAPI 聚合已发送内容，Parent 保存带
`incomplete` 标记的 AIMessage。该消息保留在公共历史，但不进入后续模型
上下文，也不计作完整轮次。

---

# 12. Stage 1 配置与测试

## 配置

使用项目根目录 `.env`，至少包含：

```text
DATABASE_URL
POSTGRES_USER
POSTGRES_PASSWORD
POSTGRES_DB
POSTGRES_PORT
DASHSCOPE_API_KEY
LLM_BASE_URL
LLM_MODEL
LOCAL_USER_ID
LOG_LEVEL
```

`.env.example` 只保留字段名，`.env` 必须被 Git 忽略。百炼 API Key、Base
URL 和模型必须与所选地域及业务空间匹配。

Windows 本地开发的外部依赖统一由 Docker Desktop 和项目根目录
`compose.yaml` 提供。PostgreSQL 只绑定本机回环地址，并使用
`agent-runtime-postgres` 命名卷持久化；AgentRuntime 应用本身继续在主机通过
`uv` 启动。

## 测试

默认自动化测试使用 Fake Model，覆盖 Router、范围守卫、有限回流、上下文
10/5 规则、消息去重、父子 Checkpointer/thread_id 隔离、SSE 协议和重启
恢复。真实百炼调用只作为可选 smoke test，不作为默认测试或 CI 前置条件。

正式实现前先完成一个最小技术验证，证明：

```text
Parent 可调用 create_agent Child 和 StateGraph Child
→ Child 使用独立 Checkpointer / thread_id
→ Child token 可穿透 Parent stream
→ 最终 AIMessage 可写回 Parent
→ 重启后状态可恢复
```

---

# 13. Stage 2 架构增量

Stage 2 在现有 Runtime 外增加产品服务和演示前端，不改变 Parent Graph 与 Child
Agent 的状态所有权。

## 13.1 组件关系

```text
React / Ant Design X /chat
        ↓ 同源 HTTP + SSE
FastAPI Product API
   ├── Session Service
   ├── History Adapter
   ├── Feedback Store
   └── Active Run Registry
        ↓
Parent Graph（公共消息权威源）
        ↓
固定 Child Capabilities
        ↓
PostgreSQL Session / Feedback / Checkpoints
```

前端只消费 Product DTO 和产品 SSE。不得让前端读取、缓存或解释原始
`StateSnapshot`、LangGraph 节点、tasks、checkpoint metadata 或 Child 私有状态。

## 13.2 Active Run

单实例进程内使用：

```text
active_runs[session_id] = ActiveRun
```

`ActiveRun` 只保存：

```text
session_id
response_message_id
producer_task
cancel_reason
terminal_future
event_queue
```

它不保存 Parent / Child 业务 State。Registry 的增删查必须由同一个异步锁保护，
并通过每次占用生成的内部 token 防止旧任务误删同 Session 的新 Run。

已有 Session 的请求顺序：

```text
校验 Session 和 Parent 状态
→ reserve active Run
→ 创建 HumanMessage
→ 建立 SSE
→ 启动独立 producer task
```

新 Session 的请求顺序：

```text
服务端生成 session_id
→ reserve active Run
→ 创建 Session
→ 保存第一条 HumanMessage
→ 建立 SSE
```

占用失败时在 SSE 前返回 HTTP 409 `SESSION_BUSY`，且不得新增 HumanMessage。
Stage 2 不引入 Redis，因此只支持单应用实例的并发互斥。

## 13.3 SSE Producer 与停止

Graph 执行放在独立 producer task 中，SSE 响应从该 Run 的 event queue 消费产品
事件。这样 Stop 接口可以取消执行并等待终态，而不是依赖另一个 HTTP 生成器的
调用栈。

```text
Graph delta
→ producer 聚合 partial text
→ Product message event
→ event queue
→ SSE response
```

producer 必须在一个统一的终态处理区完成：

```text
completed / unsupported
→ 确认 Parent 最终消息
→ done

用户 Stop
→ 取消 graph task
→ 保存 stopped AIMessage
→ done(stopped)

客户端断开
→ 取消 graph task
→ 保存 incomplete AIMessage（允许空内容）

运行异常
→ 有部分输出时保存 incomplete AIMessage
→ error
→ done(failed)

所有路径
→ 更新 Session.updated_at
→ 完成 terminal_future
→ release active Run
```

Stop 等待 `terminal_future` 后返回。无 active Run 时直接返回当前无运行状态。
服务端只能保证 producer 和 SSE 服务端迭代结束，不能保证网络客户端已读取最后
一个事件。

## 13.4 公共消息元数据

Stage 2 新生成的公共 AIMessage 在 `additional_kwargs` 中保存：

```json
{
  "runtime_status": "completed|unsupported|incomplete|stopped",
  "capability_id": "general_chat|en_to_zh|null"
}
```

这些字段是产品消息元数据，不是 Child 私有 State。Parent 仍是完整公共消息的
唯一权威源。Stage 1 正常 AIMessage 缺少 `runtime_status` 时按 `completed`
读取；缺少 `capability_id` 时返回 `null`。

## 13.5 Session 与消息分页

Session cursor 使用 Base64 URL-safe 编码的版本化 JSON，只包含
`updated_at + session_id`。数据库使用同一组合条件继续稳定降序查询，并读取
`limit + 1` 条判断是否存在下一页。

消息历史不新增第二套消息表：

```text
Parent 最新活动 checkpoint
→ 读取权威 messages
→ 过滤并转换 Product Message DTO
→ 按 before=message_id 在内存切片
→ 附加当前活动消息的 Feedback
→ 按时间正序返回
```

`before` 必须出现在当前活动消息列表中。最新活动 checkpoint 自然代表
Regenerate 后的新分支，因此普通历史不需要产品级 `branch_id`。

## 13.6 Regenerate

Regenerate 的目标只能是当前活动消息列表中最后一个 AIMessage，并且其
`runtime_status` 必须为 `completed`。

```text
message_id
→ 验证当前活动分支与最新 completed 条件
→ 遍历 Parent aget_state_history
→ 找到回答执行前且 next 指向 Capability 调用的 checkpoint
→ aupdate_state 创建 fork checkpoint
→ 使用返回的 fork config，以输入 None 继续 Parent
→ 产生新 response_message_id
```

Fork checkpoint 必须已经包含原 HumanMessage 和原 Parent 路由状态。执行仍经过
Parent 控制面，不允许 API 直接调用 Child。原 checkpoint、原 AIMessage 和原
Feedback 不删除；普通历史随最新 checkpoint 切换到新分支。

## 13.7 Feedback 与删除

Feedback 使用独立业务表：

```text
message_feedback
├── user_id
├── session_id
├── message_id
├── value = like | dislike
└── updated_at

PRIMARY KEY (user_id, message_id)
```

写入前必须通过 History Adapter 确认目标位于当前活动分支且是
`completed` AIMessage。`session_id` 只用于 Session 级清理和索引，不改变
`user_id + message_id` 的唯一产品语义。`cancel` 删除该唯一键记录。

Session 删除以 Session 行作为可重试锚点：

```text
stop_and_wait
→ delete {session_id}:general_chat
→ delete {session_id}:en_to_zh
→ delete parent {session_id}
→ delete feedback
→ delete session last
```

任一步失败均返回稳定应用错误；因为 Session 最后删除，客户端可以使用同一
Session ID 重试。不存在或不属于固定用户的 Session 对 DELETE 统一返回 204。

## 13.8 前端与静态资源

前端源码位于 `frontend/`，使用 React JavaScript/JSX、Vite、Ant Design 和
Ant Design X。业务代码按组件、API 服务和 CSS 分离，不建立自研通用组件库。

```text
frontend/index.html
frontend/src/main.jsx
frontend/src/App.jsx
frontend/src/components/*
frontend/src/services/chatApi.js
frontend/src/services/sseClient.js
frontend/src/styles/*
```

Vite 输出到 Python 包内的 `src/agent_runtime/static/`：

```text
static/index.html
static/assets/*.js
static/assets/*.css
```

构建产物提交到仓库并由 FastAPI 同源提供：`/chat` 返回入口 HTML，
`/assets/*` 返回带内容哈希的静态资源。运行已构建 Demo 不要求 Node；开发和
重新构建使用 npm，并通过 `package-lock.json` 固定依赖。

页面采用左侧 Session 列表、右侧聊天区和可折叠运行状态面板。运行状态面板只
展示 `session_id`、`message_id`、`capability_id`、流式状态和最终状态。

POST SSE 使用 `fetch + ReadableStream` 解析。前端收到事件后可进行即时 UI
投影，但在切换 Session、刷新页面和 Run 结束后必须重新以 API 数据为准。

---

# 14. Stage 3 架构增量

Stage 3 新增：

```text
Capability Manifest
Capability Registry
User Capability Permission
Dynamic mount/unmount
Graceful draining
Router confidence
interrupt/resume
```

目标架构：

```text
Manifest
→ Registry
→ Permission Filter
→ Router
→ Capability
```

Stage 3 仍只支持 Local Capability。

---

# 15. Future 边界

未来复杂任务 Agent 不应把串并联逻辑塞入 Parent Graph。

正确演进：

```text
Parent Router
→ Complex Task Capability
→ 内部负责多个 Capability / SubAgent 编排
```

这样 Parent Graph 长期保持轻量稳定。
