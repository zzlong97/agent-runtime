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
DASHSCOPE_API_KEY
LLM_BASE_URL
LLM_MODEL
LOCAL_USER_ID
LOG_LEVEL
```

`.env.example` 只保留字段名，`.env` 必须被 Git 忽略。百炼 API Key、Base
URL 和模型必须与所选地域及业务空间匹配。

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

Stage 2 新增：

## Active Run

单实例进程内：

```text
active_runs[session_id] = asyncio.Task
```

用于：

- SESSION_BUSY
- stop
- Session 删除前停止执行

Stage 2 不引入 Redis。

## Message History Adapter

```text
Checkpoint History
→ 后端解析
→ Product Message DTO
→ Frontend
```

## Regenerate

```text
message_id
→ 定位相关历史 checkpoint
→ 从历史 checkpoint 恢复
→ fork 新分支
→ 继续执行
```

不复制一套业务消息历史实现版本管理。

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
