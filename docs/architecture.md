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

这些不属于 Stage 1 / 2。S3 只按第 15 节引入静态 Manifest、Registry 和最小权限；
动态挂载、管理、Remote Executor 等仍属于 Future。

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
LOG_DIR
```

`.env.example` 只保留字段名，`.env` 必须被 Git 忽略。百炼 API Key、Base
URL 和模型必须与所选地域及业务空间匹配。

Windows 本地开发的外部依赖统一由 Docker Desktop 和项目根目录
`compose.yaml` 提供。PostgreSQL 只绑定本机回环地址，并使用
`agent-runtime-postgres` 命名卷持久化；AgentRuntime 应用本身继续在主机通过
`uv` 启动。

## 业务日志

业务日志使用 Python 标准日志模块，由 `agent_runtime.core.logging` 统一配置。
控制台日志继续保留，同时按本地日期将 UTF-8 日志写入
`{LOG_DIR}/agent-runtime-YYYY-MM-DD.log`，`LOG_DIR` 默认是项目运行目录下的
`logs`。

应用生命周期、HTTP、Session、Active Run、Parent、Router 和两个 Child
Capability 在关键入口、出口、拒绝、取消和失败位置记录中文结构化事件。事件只携带
关联 ID、能力标识、状态、错误码、错误类型和耗时等控制面信息，不记录用户或模型
正文、Prompt、凭据、数据库连接串及逐 token 数据。日志是旁路，不进入 Parent /
Child State、Checkpoint 或 SSE 协议。`configure_logging` 只在进程启动或测试重建
应用时调用，业务模块只通过统一事件函数写日志，不在运行期重复配置 handler。

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

本节保留已经验收的 Stage 2 实现基线。Stage 2.5 开始后，进程内 Active Run、
POST 直连 SSE、同步 Stop 和“连接断开即停止”等冲突设计由第 14 节取代，不再作为
当前实现目标。

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

# 14. Stage 2.5 架构增量

Stage 2.5 把执行生命周期从 HTTP 连接和进程内 Registry 中抽离，建立持久 Run、
RuntimeEvent、Redis 实时事件、独立 SSE Gateway 和可恢复 Executor。Parent / Child
状态所有权、Parent 公共消息权威源和有限 `OUT_OF_SCOPE` 回流规则保持不变。

## 14.1 组件关系

```text
React / Ant Design X /chat
        ↓ Product HTTP
FastAPI Run API
        ↓
PostgreSQL Run / RuntimeEvent / Interrupt
        ↓                     ↘ durable public event
Single-process Coordinator      SSE Gateway ← Redis Stream
        ↓                            ↓ merge by seq
Run Executor → Parent Graph      Browser EventSource
        ↓
Parent / Child PostgreSQL Checkpointer
```

事实数据所有权：

```text
完整公共消息        → Parent messages
执行生命周期        → PostgreSQL Run
持久运行事件        → PostgreSQL RuntimeEvent
中断提交幂等        → PostgreSQL run_interrupts
短期实时公开事件    → Redis Stream
页面临时显示        → 非权威投影
```

S2.5 仍为单 Runtime 进程。PostgreSQL 负责跨进程重启后的恢复，但不提供多实例
Worker 竞争、Lease 或分布式任务调度语义。

## 14.2 Run 数据模型与状态机

Run 业务表建议至少包含：

```text
runs
├── run_id UUID PRIMARY KEY
├── request_id UUID UNIQUE NOT NULL
├── session_id UUID NOT NULL REFERENCES sessions
├── thread_id TEXT NOT NULL
├── parent_run_id UUID NULL REFERENCES runs
├── run_type TEXT NOT NULL
├── input_message_id UUID NOT NULL
├── response_message_id UUID NOT NULL
├── start_checkpoint_id TEXT NULL
├── input_payload JSONB NULL
├── request_fingerprint TEXT NOT NULL
├── status TEXT NOT NULL
├── recovery_attempts INTEGER NOT NULL DEFAULT 0
├── seq_high_watermark BIGINT NOT NULL DEFAULT 0
├── error_code TEXT NULL
├── error_message TEXT NULL
├── created_at / started_at / finished_at / updated_at
```

活动状态部分唯一索引覆盖：

```text
queued | running | recovering | interrupted | cancel_requested
```

并以 `session_id` 为键保证每个 Session 最多一个活动 Run。合法主状态转换：

```text
queued → running → completed | failed | interrupted | cancel_requested
queued → cancelled | failed
running → recovering            # 仅重启接管投影
recovering → running | failed | interrupted | cancel_requested
interrupted → running | cancelled
cancel_requested → cancelled
```

完成与取消竞争时，条件更新只允许首个终态成功；所有终态均不可再修改。Run
完成为 `unsupported` 时状态仍为 `completed`，公共消息保存 `unsupported`。

创建 Run 的业务事务负责：

```text
必要时创建 Session
→ 分配 input_message_id / response_message_id
→ 捕获 start_checkpoint_id
→ 写入 request_id / request_fingerprint / input_payload
→ 创建 queued Run
```

事务提交后才返回 HTTP 202。执行 Checkpoint 不属于这个事务。规范化请求指纹用于
终态清除 `input_payload` 后继续判断幂等冲突。

## 14.3 Coordinator 与 Executor

接口提交后进行一次低延迟的进程内唤醒。Coordinator 还必须：

- 定期扫描未被本地执行的 `queued` Run，补偿通知丢失
- 应用启动时扫描全部非终态 Run
- 以本地表或任务映射防止同一进程重复启动同一个 Run
- 不把该扫描器扩展为多实例 Worker 系统

状态接管：

```text
queued           → 领取为 running
running          → recovery_attempts + 1，进入 recovering
recovering       → recovery_attempts + 1，重新接管
cancel_requested → 完成取消投影
interrupted      → 不执行，继续等待 Resume / Cancel
```

初次执行不计恢复次数。每次恢复接管前原子递增，最多三次；第三次恢复失败后必须
形成非空 `incomplete` 公共消息并进入 `failed`。

## 14.4 RuntimeEvent 与 Sequencer

持久表建议包含：

```text
runtime_events
├── event_id UUID PRIMARY KEY
├── run_id UUID NOT NULL REFERENCES runs
├── seq BIGINT NOT NULL
├── event_type TEXT NOT NULL
├── source TEXT NOT NULL
├── visibility TEXT NOT NULL
├── payload JSONB NOT NULL
├── schema_version INTEGER NOT NULL
├── durability TEXT NOT NULL
├── created_at TIMESTAMPTZ NOT NULL
└── UNIQUE (run_id, seq)
```

`event_id` 使用 UUIDv4，只承担唯一标识。每个 Run 的 Sequencer 从 PostgreSQL
原子租用序号块，并串行处理所有事件发布。块内未使用序号可以永久形成缺号；任何
消费者只能比较大小，不能要求连续。

事件出口分为：

```text
public  → 固定公开 Schema → PG（durable）或 Redis（transient）→ SSE
internal → 内部 Schema → 仅允许受控持久化和服务端诊断
```

公共投影必须显式白名单。`source` 和可选内部 `node_id` 不允许成为前端对
LangGraph 拓扑的依赖；Prompt、Checkpoint、State、工具原始参数和结果不得进入
公开 payload。

持久公开事件写入流程：

```text
BEGIN
  条件更新 Run / Interrupt 状态
  INSERT RuntimeEvent
COMMIT
→ Redis XADD best effort
```

瞬时 `message.delta` 不写 PostgreSQL，只经 Sequencer 写 Redis。每次执行尝试开始
前先持久化 `message.started(response_message_id, attempt)`；恢复尝试的 `attempt`
递增，使页面可以抛弃旧草稿。

## 14.5 Redis Stream 与 SSE Gateway

Redis key 为 `runtime:events:{run_id}`，显式 Stream ID 为 `{seq}-0`。写入与刷新
30 分钟 TTL 应在同一个 Redis 事务或等价原子操作中完成。Redis 不使用持久卷，
任何错误均降级而不是令 Run 失败。

SSE Gateway 维护单调递增的 `last_emitted_seq`：

```text
读取 Last-Event-ID，否则读取 after_seq
→ 查询 PostgreSQL seq > cursor 的 durable public 事件
→ XREAD Redis seq > cursor 的实时事件
→ 合并、按 seq 排序、去重
→ 仅发送 seq > last_emitted_seq
→ Run 终态且无剩余事件时关闭
```

Gateway 不以数值缺号推断丢失。Redis 已经过期或不可用时，瞬时 delta 可以缺失；
终态事件仍从 PostgreSQL 到达，页面随后重新读取 Parent 历史替换本地草稿。

连接读取采用有限批次，上一批未写完前不预取下一批。写入超时或客户端断开只结束
该 Gateway 任务，不触碰 Executor。每 20 秒发送 SSE 注释心跳，不占用事件序号。

## 14.6 Checkpoint 与恢复对账

Run 的 `start_checkpoint_id` 是接受请求时捕获的确定起点。每个后续 Checkpoint
metadata 必须包含 `run_id`，最终消息还必须可以通过稳定 `response_message_id`
识别；中断快照必须能够定位稳定 `interrupt_id`。

终态与中断的写入顺序固定为：

```text
1. Checkpointer 持久化最终 AIMessage 或 interrupt snapshot
2. PostgreSQL 事务写 Run / Interrupt 投影与 durable RuntimeEvent
3. Redis best-effort publish
```

恢复器先读取该 Run 的精确最新 Checkpoint：

- 已有最终 `response_message_id`：只补齐消息终态、Run 和事件投影
- 已有待处理中断：只补齐 `run_interrupts`、Run 和事件投影
- 有中间 Checkpoint：从该精确 Checkpoint 恢复
- 没有 Run Checkpoint：从 `start_checkpoint_id + input_payload` 重放

同一稳定消息 ID 的重复写入必须按消息归并语义覆盖或去重，不得产生第二条公共
HumanMessage / AIMessage。所有可能产生外部副作用的节点在进入恢复范围前必须实现
幂等键或操作账本；否则执行器应拒绝自动恢复，而不是宣称恰好一次。

## 14.7 Cancel、Interrupt 与 Session 删除

Cancel API 只负责把请求持久化为 `cancel_requested` 并返回 202。Executor 先发出
协作式取消，超过配置的宽限时间后取消本地任务；最终 Checkpoint 和公共消息写入
完成后再提交 `run.cancelled`。重复 Cancel 读取当前状态，不重复执行副作用。

`run_interrupts` 建议使用：

```text
run_id + interrupt_id UNIQUE
status
interrupt_payload
resume_payload
resume_request_id
created_at / resumed_at / cancelled_at
```

另建只覆盖 pending 状态的唯一约束，确保每个 Run 最多一个待处理中断。Resume
在事务中锁定记录，校验恢复请求指纹并只允许一次状态转换。Checkpoint 中断快照
先于 `interrupt.required` 投影；Resume 调用 `Command(resume=...)` 时沿用原 Run，
不增加 HumanMessage。

Session 删除由 Session 级删除协调器形成进程内屏障：

```text
拒绝新 Run
→ Cancel active Run and wait terminal
→ collect run_ids
→ best-effort delete Redis keys
→ delete run_interrupts / runtime_events / runs
→ delete Child / Parent checkpoints and feedback
→ delete Session last
```

Redis 删除失败不阻塞 PostgreSQL 硬删除。Session 最后删除，使失败请求可以安全
重试。

## 14.8 Agent 执行边界

统一执行入口只传入：

```text
RunContext   → run_id、request_id、稳定消息 ID、取消与事件出口
AgentContext → capability_id、Child thread/config 等受控执行上下文
TaskInput    → 本次由 Parent 派生的输入视图
```

事件出口接受类型化事件并交给 Sequencer。Agent 不获得 Redis Client、SSE Response、
Session 删除能力或顶层 Run Repository，不能自行改变 Run 终态。现有 `ChildResult`
继续只承载控制状态；完整公共文本沿数据面写回 Parent。

开发环境 Fake Agent 可以在显式开关下产生正常、慢速、失败和中断场景，但必须经过
同一 Executor、Event、Redis、SSE 和 Checkpoint 链路，不能由前端伪造。

## 14.9 页面迁移与验收结构

前端从 POST SSE 改为：

```text
POST create Run → 202
→ GET active Run / event stream
→ message.started 清理草稿
→ message.delta 更新草稿
→ message.finalized / run.* terminal
→ GET Parent history 重新校准
```

Run 状态面板展示公开状态、Run ID、消息 ID、Capability、恢复尝试和中断操作，
不得显示内部事件正文。开发演示模式由后端配置控制，默认关闭。

测试层次：

```text
unit        → 状态机、Schema、Sequencer、幂等与事件过滤
PostgreSQL  → 事务、唯一约束、恢复对账与删除
Redis       → Stream ID、续传、TTL 与故障降级
integration → 进程重启、取消、中断恢复和非空终态
frontend    → 状态投影、重连、Cancel、Resume
Playwright  → 构建页面端到端真实链路
```

默认测试继续使用 Fake Model。真实百炼 smoke 只在负责人配置指定环境文件后显式
执行，不作为默认门禁。

---

# 15. Stage 3 Capability Runtime 架构增量

S3 在 S2.5 持久 Run 与恢复框架内增加静态 Capability Runtime。Registry 只负责
启动期发现和实例管理；Parent 继续负责会话级调度；Runtime 继续是 Run、事件、
权限、Task、并发、超时和恢复的唯一控制面。

## 15.1 总体结构与职责

```text
Local YAML files
      ↓
LocalYamlCapabilitySource ── CapabilitySource protocol
      ↓ strict validation / collect all conflicts
Capability Registry Snapshot
      ├── RouterProjection(capability_id, name, description, enabled)
      ├── Capability factory → initialize → health snapshot
      └── RuntimePolicy(state / concurrency / recovery / side effect)
                                      ↓
Run Executor → Parent Graph → Router → Capability Invocation Gateway
                                      ├── permission + health recheck
                                      ├── invocation lifecycle event
                                      ├── Task / State Scope
                                      ├── concurrency + timeout
                                      ├── Operation Ledger
                                      └── Capability.invoke(...)
                                                    ↓
                       Child Checkpointer / Capability-owned stores
```

职责边界：

| 组件 | 负责 | 禁止 |
| --- | --- | --- |
| `CapabilitySource` | 返回原始 Manifest 文档和来源位置 | 导入代码、实例化 Capability、访问 Router |
| Manifest Validator | 严格 Schema 校验和跨字段校验 | 修复、补猜非法配置 |
| Registry | 冲突隔离、entrypoint 加载、实例生命周期、健康快照 | 执行顶层 Run、写 SSE、管理 Session |
| Router | 从允许的最小投影选择 `capability_id + task_action` | 读取 Prompt、State、Tool、entrypoint、健康详情 |
| Invocation Gateway | 权限复查、Task、State Scope、并发、超时、恢复与事件 | 理解 Capability 业务依赖 |
| Capability | 范围判断、业务执行、私有 State、业务副作用边界 | 直接访问 SSE/Redis、创建或结束顶层 Run、管理 Session |
| Parent Graph | 公共消息、有限回流、会话级控制 | 保存 Capability 私有业务 State |

Registry 在应用启动时一次性构建不可变 Snapshot。S3 没有文件监听、热重载、
mount/unmount 或 DRAINING；Manifest 文件变化必须重启应用后才生效。

## 15.2 Manifest 最终 Schema

本地 Manifest 与其嵌套对象全部使用 `extra=forbid`。所有下列字段均显式出现；尤其
`state_scope`、恢复与副作用策略不得通过默认值推断。

| 字段 | 类型与约束 | 消费方 | 用途 |
| --- | --- | --- | --- |
| `manifest_schema_version` | 整数，S3 固定 `1` | Runtime | Manifest 契约版本 |
| `capability_id` | 字符串，`^[a-z][a-z0-9_]{0,63}$` | Router + Runtime | 稳定能力标识 |
| `name` | 非空字符串，最长 80 | Router | 人类可读名称 |
| `description` | 非空字符串，最长 500 | Router | 路由选择说明，不得包含 Prompt |
| `enabled` | 布尔 | Router + Runtime | 静态启用开关 |
| `entrypoint` | `package.module:factory_name` | Runtime | 统一工厂导入位置 |
| `version` | SemVer 字符串 | Capability 自述 + Runtime | 当前实现版本 |
| `state_scope` | `invocation \| run \| session` | Runtime | Child State 共享范围 |
| `state_schema_version` | 1~64 位稳定版本标识 | Capability 自述 + Runtime | 当前私有 State Schema |
| `compatible_state_schema_versions` | 唯一字符串数组 | Capability 自述 + Runtime | 当前实现可读取的旧 State Schema |
| `allow_degraded` | 布尔 | Runtime | degraded 是否仍可服务 |
| `concurrency.mode` | `unlimited \| bounded` | Runtime | 进程内并发模式 |
| `concurrency.max_concurrency` | bounded 时正整数；unlimited 时 `null` | Runtime | 单 Capability 并发上限 |
| `concurrency.acquire_timeout_seconds` | 正数 | Runtime | 获取并发槽最大等待时间 |
| `execution.timeout_seconds` | 正数 | Runtime | 单 Invocation 执行时限 |
| `execution.cancel_grace_seconds` | 大于等于 0 | Runtime | 协作取消宽限时间 |
| `recovery_policy` | `automatic \| manual` | Runtime | 崩溃后能否自动重放 |
| `side_effect_policy` | `none \| idempotent \| unsafe` | Runtime | 外部副作用恢复约束 |

字段视图分层：

```text
Router 可见
  capability_id / name / description / enabled

Runtime 内部
  manifest_schema_version / entrypoint / state_scope / allow_degraded
  concurrency / execution / recovery_policy / side_effect_policy

Capability 自述、Router 不可见
  version / state_schema_version / compatible_state_schema_versions
```

S3 不开放任意 Manifest 顶层扩展字段，也不把业务 Prompt、Tool 或业务配置塞进
Manifest。Capability 专属非敏感配置由 composition root 形成只读配置视图，经
Bootstrap Context 注入；敏感值仍从既有安全配置入口获取且不得进入日志。

跨字段校验：

- `automatic + unsafe` 非法
- `bounded` 必须提供正整数 `max_concurrency`
- `unlimited` 的 `max_concurrency` 必须为 `null`
- `compatible_state_schema_versions` 去重；当前版本无需重复列入
- entrypoint 必须是合法模块路径和属性名，不接受文件系统路径或任意表达式

Manifest 建议与实现同目录：

```text
src/agent_runtime/capabilities/<capability_id>/manifest.yaml
```

`CapabilitySource` 只返回带来源标识的原始文档。Local Source 扫描固定受控目录，
按规范化路径排序以保证测试确定性。Validator 必须先收集全部文档，再按 ID 分组；
同一 ID 出现多次时整组隔离，不能保留“第一个”或“最后一个”。

## 15.3 entrypoint、Bootstrap 与 Capability 协议

entrypoint 指向同步工厂；异步资源初始化放在 `initialize()`：

```text
create_capability(
    bootstrap_context: CapabilityBootstrapContext
) -> Capability
```

Bootstrap Context 最小字段：

```text
manifest                 # 深度只读的已校验 Manifest
child_checkpointer       # 该 Capability 的隔离 Checkpointer 句柄
model_factory            # 支持生产模型与 Fake Model 注入
capability_config        # 非敏感只读配置视图
register_cleanup         # 向 Runtime 生命周期注册幂等异步清理
```

工厂必须返回实现以下协议的对象：

```text
initialize() -> Awaitable[None]
health_check() -> Awaitable[HealthResult]
invoke(
    run_context: RunContext,
    agent_context: AgentContext,
    task_input: TaskInput,
) -> Awaitable[AgentResult]
```

Registry 校验工厂签名、返回协议和初始化结果。资源清理按成功初始化的逆序运行；
失败 Capability 已注册的清理也必须执行。S3 不增加热卸载生命周期方法。

运行上下文扩展：

```text
RunContext
  run_id / request_id / stable message ids
  invocation_id
  cancellation
  typed EventOutlet
  idempotency_key(operation_key)
  operation(operation_key)

AgentContext
  session_id / capability_id
  task_id
  state_scope / state_schema_version
  deterministic child thread_id / child config

TaskInput
  deep-copied Parent-derived message snapshot
```

`AgentResult` 使用关闭 Schema：

```text
status = completed | rejected
content: string
metadata:
  control_signal = OUT_OF_SCOPE | null
  task_transition = keep_active | completed | failed | cancelled
```

组合约束：

- `completed`：`content` 去除空白后非空，`control_signal=null`
- `rejected`：`content` 为空，`control_signal=OUT_OF_SCOPE`，Task 不得终态转换
- 失败抛出 `CapabilityError`；取消传播 `asyncio.CancelledError`
- `task_transition` 默认语义为显式 `keep_active`，避免把 Run 完成误作 Task 完成

EventOutlet delta 是实时投影，`AgentResult.content` 是成功结果的规范全文。Runtime
用后者构造唯一最终 AIMessage 并写 Parent；metadata 永不直接进入公开事件。若
Capability 已产生公开 delta 后再返回 rejected，Runtime 视为契约违规。

## 15.4 Registry 状态、HealthSnapshot 与 Router 投影

Registry Snapshot 对每个来源记录以下静态加载状态之一：

```text
active
disabled
invalid_manifest
duplicate_id
entrypoint_failed
initialize_failed
```

Health Service 在静态 Entry 上叠加 `healthy / degraded / unhealthy` 动态快照；
`unhealthy` 与不允许服务的 degraded 不改变 Registry Snapshot，只影响当次候选。
只有静态 `active` 且健康可服务的实例进入服务候选。隔离原因写中文业务日志和内部
诊断，不写公开 SSE，不包含异常堆栈、凭据或 Manifest 敏感内容。

`HealthSnapshot` 至少包含：

```text
capability_id
status = healthy | degraded | unhealthy
checked_at
expires_at
summary_code
```

TTL 使用 Runtime 全局配置，不扩张 Manifest。启动时强制检查；TTL 内复用快照；
过期后的首个需要者在单飞锁内刷新，其他并发调用等待同一结果，避免健康检查风暴。
健康详情只供内部日志与诊断，Router 只得到可服务候选。

Router 请求由 Runtime 每轮构造：

```text
Registry serviceable projection
∩ user permissions
- rejected_capability_ids
```

结构化 Router 结果：

```text
capability_id
task_action = continue | new
confidence                 # 继续记录，不触发低置信度 interrupt
```

Parent 可在无明显新意图时把当前 Capability 作为优先提示，但仍必须重新经过 Registry、
健康和权限过滤。Parent 保存 `resolved_capability_id` 和本轮 `task_action`，不保存
task_id 或 Capability 私有 State。

## 15.5 Run、Invocation 与 Task 关系

```text
Session 1 ── N Run
Session 1 ── N CapabilityTask
CapabilityTask 1 ── N Invocation
Run 1 ── N Invocation（S3 顺序执行，同时最多一个 open）
Invocation 1 ── N CapabilityOperation
Session + Capability 1 ── 0..1 current Task Context
```

所有 Capability Invocation 都关联 Task，`state_scope` 只决定私有 State 的共享键，
不决定是否存在 Task。这样 invocation/run scope 仍可表达长期业务生命周期和副作用
归属，但其 Child State 不跨越相应边界共享。

调用顺序：

```text
1. invoke 前权限复查、Registry/Health/State Version 校验
2. 生成或从 open started event 恢复 invocation_id
3. durable 写 internal.capability.invocation.started
4. 在 acquire_timeout 内获取该 Capability 并发槽
5. 事务解析 continue/new Task，写最近执行投影和 Task 内部事件
6. 生成确定性 Child thread_id，调用 Capability
7. 应用 AgentResult 的 Task 意图和最终公共消息
8. durable 写 completed / failed / cancelled Invocation 事件
9. 释放并发槽
```

`CAPABILITY_BUSY` 发生时已有 started/failed Invocation 事件，但可以没有 task_id；
并发槽未取得前不创建新 Task。Run 内一次只允许一个 open Invocation，使恢复器可以
从最新未配对的 started 事件唯一复用 `invocation_id`。

Task internal durable 事件至少包括：

```text
internal.capability.task.created
internal.capability.task.current_changed
internal.capability.task.completed
internal.capability.task.failed
internal.capability.task.cancelled
internal.capability.task.continue_degraded_to_new
internal.capability.task.rejected_rolled_back
```

这些事件使用固定内部 Schema，经 Run Sequencer 分配 seq，不进入 Redis 或 SSE。

### OUT_OF_SCOPE 的可回滚 Task

`task_action=new` 调用前，Task Service 在一个事务内保存原 current 指针、创建
provisional Task 并切换 current。若合法返回 rejected：

```text
验证无公开输出、无 Operation、无 Child Checkpoint
→ 同一事务删除 provisional Task
→ 恢复原 current；原本为空则删除 context 行
→ 写 invocation completed(outcome=rejected)
→ 清理 provisional Child thread
```

拒绝后只保留 Invocation 与 provisional Task 创建/回滚的内部诊断，不保留 Task 行、
current 指针或 Child State。Runtime 只检查 provisional thread 是否存在 Checkpoint，
不得读取 Capability 私有 payload。若已产生 Operation、公开输出或 Child Checkpoint
后拒绝，按 `CAPABILITY_EXECUTION_FAILED` 结束，不回滚可能关联外部事实的 Task。

## 15.6 State Scope 与 thread_id

Child thread ID 使用以下规范化格式：

```text
capability:v1:{session_id}:{capability_id}:schema:{state_schema_version}:
  invocation:{invocation_id}

capability:v1:{session_id}:{capability_id}:schema:{state_schema_version}:
  run:{run_id}

capability:v1:{session_id}:{capability_id}:schema:{state_schema_version}:
  task:{task_id}
```

实际值为单行，不包含空格和换行。`v1` 是 thread 命名算法版本，不是 Capability
实现版本。thread_id 不包含 `version`，否则兼容旧 State Schema 的新实现无法继续
原 Task。

State Scope 映射：

| scope | 共享键 | 生命周期 |
| --- | --- | --- |
| `invocation` | `invocation_id` | 每次调用独立 |
| `run` | `run_id + capability_id` | 同一 Run 内同能力复用 |
| `session` | `task_id` | Task 跨 Run 继续 |

Task 固化创建时的 State Schema 版本。continue 时有效兼容集合为：

```text
{manifest.state_schema_version}
∪ manifest.compatible_state_schema_versions
```

不兼容时在调用前失败，不创建新 Task、不切换 current、不修改旧 Task，也不自动
回退为 new。旧的 `{session_id}:{capability_id}` checkpoint 不迁移；两个演示能力
可从 Parent 公共历史构建新快照。Session 删除必须同时清理新旧命名 thread。

### 15.6.1 Regenerate 安全门禁

Parent Regenerate 只能回到 Parent Checkpoint，不能自动 fork Capability 私有 State。
S3 因此只允许以下组合：

```text
state_scope = invocation
side_effect_policy = none
```

提交顺序：

```text
校验仍是活动分支最新 completed AIMessage
→ 从原 AIMessage 确定 capability_id
→ 读取 Registry Manifest 并校验 invocation + none
→ 复查 enabled / Health / Permission / State Schema
→ 捕获 Parent start_checkpoint_id
→ 创建 regenerate Run
```

不重新调用 Router。执行时以 `task_action=continue` 复用 current Task；旧 Session 尚无
current Task 时沿用既定降级 new 规则。每次 Regenerate 使用新的 invocation_id 和
invocation-scope Child thread，输入来自 Parent Checkpoint Fork，不读取原 Invocation
的 Child State。

不满足安全组合时，在 Run 创建前返回 HTTP 409
`CAPABILITY_REGENERATE_UNSUPPORTED`，不创建 Run、Task、Invocation 或 Operation。
S3 不增加 Manifest `supports_regenerate` 字段，也不实现 Child Checkpoint Fork。

## 15.7 数据模型

S3 延续当前 Repository 自建表与幂等 `setup()` 方式，不在本阶段额外引入 Alembic。
所有时间使用 `TIMESTAMPTZ`，所有状态使用 CHECK 约束，所有写操作使用参数化 SQL。

### capability_tasks

| 字段 | PostgreSQL 类型 | 可空 | 约束与用途 |
| --- | --- | --- | --- |
| `task_id` | UUID | 否 | 主键，服务端 UUIDv4 |
| `session_id` | UUID | 否 | FK `sessions(session_id)`，所属 Session |
| `capability_id` | VARCHAR(64) | 否 | Manifest ID 格式 CHECK；不对本地 Registry 建 FK |
| `state_schema_version` | VARCHAR(64) | 否 | 创建时固化，非空 CHECK |
| `status` | TEXT | 否 | `active/completed/failed/cancelled` CHECK |
| `last_run_id` | UUID | 是 | FK `runs(run_id)`；最近执行投影 |
| `last_invocation_id` | UUID | 是 | 最近 Invocation 投影；无 Invocation 表，不设 FK |
| `created_at` | TIMESTAMPTZ | 否 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 最近投影更新时间 |
| `ended_at` | TIMESTAMPTZ | 是 | active 必须为空，终态必须非空 |

约束与索引：

```text
UNIQUE (task_id, session_id, capability_id)  # 供 Context 复合 FK
INDEX (session_id, capability_id, status, updated_at DESC)
INDEX (last_run_id) WHERE last_run_id IS NOT NULL
CHECK ((status='active' AND ended_at IS NULL)
    OR (status<>'active' AND ended_at IS NOT NULL))
```

外键使用 `ON DELETE RESTRICT`；Session 删除服务显式按顺序清理，不依赖隐式级联。
`last_run_id` 同样使用 RESTRICT，删除任务后才可删除其 Run。Task 不存业务 payload，
当前不增加 `workflow_id`；未来可直接增加 nullable UUID 列。

### capability_task_contexts

| 字段 | PostgreSQL 类型 | 可空 | 约束与用途 |
| --- | --- | --- | --- |
| `session_id` | UUID | 否 | 所属 Session |
| `capability_id` | VARCHAR(64) | 否 | 所属 Capability |
| `current_task_id` | UUID | 否 | 当前 active Task |
| `updated_at` | TIMESTAMPTZ | 否 | 指针更新时间 |

约束与索引：

```text
PRIMARY KEY (session_id, capability_id)
UNIQUE (current_task_id)
FOREIGN KEY (current_task_id, session_id, capability_id)
  REFERENCES capability_tasks(task_id, session_id, capability_id)
  ON DELETE RESTRICT
```

没有 current Task 时不保存空值行，而是删除 Context 行。Repository 在写入时同时
确认目标 Task 为 active。新建 Task + upsert Context、Task 终态 + delete Context
均使用单事务和行锁。

### capability_operations

| 字段 | PostgreSQL 类型 | 可空 | 约束与用途 |
| --- | --- | --- | --- |
| `operation_id` | UUID | 否 | 主键，服务端 UUIDv4 |
| `run_id` | UUID | 否 | FK `runs(run_id)` |
| `invocation_id` | UUID | 否 | 对应 Invocation；无独立 FK |
| `task_id` | UUID | 否 | FK `capability_tasks(task_id)` |
| `capability_id` | VARCHAR(64) | 否 | 冗余校验与查询维度 |
| `operation_key` | TEXT | 否 | Capability 提供的稳定非空业务操作键 |
| `idempotency_key` | CHAR(64) | 否 | Runtime 生成的小写 SHA-256 |
| `status` | TEXT | 否 | `pending/succeeded/failed` CHECK |
| `created_at` | TIMESTAMPTZ | 否 | 首次登记时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 状态更新时间 |

约束与索引：

```text
UNIQUE (run_id, invocation_id, operation_key)
UNIQUE (idempotency_key)
INDEX (task_id, updated_at DESC)
INDEX (run_id, invocation_id)
```

外键使用 RESTRICT。Runtime 通过 Task 复合查询校验 capability_id 一致，不允许
Capability 直接写表。Ledger 不保存外部响应、异常详情或补偿数据。

### user_capability_permissions

| 字段 | PostgreSQL 类型 | 可空 | 约束与用途 |
| --- | --- | --- | --- |
| `user_id` | TEXT | 否 | 固定服务端用户标识，非空 CHECK |
| `capability_id` | VARCHAR(64) | 否 | Manifest ID；本地 Registry 无数据库 FK |
| `allowed` | BOOLEAN | 否 | true 允许、false 显式拒绝 |
| `created_at` | TIMESTAMPTZ | 否 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 最近更新时间 |

约束与索引：

```text
PRIMARY KEY (user_id, capability_id)
INDEX (capability_id, allowed)
```

无用户表和 Capability 表，因此 S3 不增加对应 FK。无记录即 deny；不自动给两个
演示 Capability 授权。测试通过 Repository fixture 显式写 allow/deny 并清理。
权限行不属于 Session，删除 Session 时不得删除；只允许由权限 Repository 的显式
授权、拒绝或撤销操作修改。

### 删除顺序

Session 删除屏障取得并等待活动 Run 终态后：

```text
收集 run_id / task_id / 全部新旧 Child thread_id
→ delete capability_task_contexts
→ delete capability_operations
→ delete capability_tasks
→ delete run_interrupts / runtime_events / runs
→ delete dynamic + legacy Child checkpoints
→ delete Parent checkpoint / feedback
→ delete Session last
```

每一步幂等；失败时 Session 行仍作为重试锚点。Redis Stream 继续 best effort 清理。

## 15.8 Task Service 事务语义

`new`：

```text
SELECT context FOR UPDATE
→ INSERT active task
→ UPSERT current context
→ 更新 task.last_run_id / last_invocation_id
→ INSERT internal task events
→ COMMIT
```

`continue`：

```text
SELECT context + task FOR UPDATE
→ 无 current：按 new 创建并写 continue_degraded_to_new
→ 有 current：校验 active / capability / state schema compatibility
→ 更新最近执行投影
→ COMMIT
```

Task transition：

```text
SELECT task/context FOR UPDATE
→ active 才允许转 completed/failed/cancelled
→ 写 ended_at
→ 仅当 context 仍指向该 task 时删除 context
→ INSERT internal task terminal event
→ COMMIT
```

Run failed/cancelled、CapabilityError、Timeout 和 Cancel 默认都不触发 Task transition；
只有成功校验的 `AgentResult.metadata.task_transition` 可以请求 Runtime 更新 Task。

## 15.9 并发、超时与取消

每个 Registry Entry 拥有进程内并发控制器：

- `unlimited` 不分配 Semaphore
- `bounded` 使用容量固定的 asyncio Semaphore
- 等待超过 `acquire_timeout_seconds` 返回 `CAPABILITY_BUSY`
- 等待取消时立即退出，不泄漏槽位

取得槽位后创建 Task 并执行 Invocation。执行超过 `timeout_seconds`：

```text
向本 Invocation 的 AgentCancellation 发出 cooperative cancel
→ 等待 cancel_grace_seconds
→ 仍运行则 cancel 本地 asyncio Task
→ durable internal.capability.invocation.failed(CAPABILITY_TIMEOUT)
→ Runtime 形成非空 incomplete 消息并使 Run failed
```

显式 Run Cancel 使用 `internal.capability.invocation.cancelled`，不映射为 timeout。Semaphore
释放放在唯一 finally 路径。所有控制器只保证单进程并发；S3 不宣称跨实例限制。

## 15.10 Operation Ledger 与恢复

确定性幂等键：

```text
sha256(canonical(run_id, invocation_id, operation_key))
```

canonical 编码必须固定字段顺序、UTF-8 和分隔方式，并通过测试向量锁定。恢复必须
先从 durable open Invocation 事件恢复原 invocation_id，再计算幂等键。

`ctx.operation(operation_key)` 返回异步 Context Manager：

```text
__aenter__
  → 原子读取或创建 ledger
  → succeeded：返回 should_execute=false，禁止重复副作用
  → failed：抛出不可自动重试的 CapabilityError
  → pending：返回相同 idempotency_key, should_execute=true

normal __aexit__
  → 本次确实执行、未显式标记 failed 且 Capability 正常返回：标记 succeeded
  → 已 succeeded 或未执行副作用时保持原状态

exception __aexit__
  → 默认不改 pending
  → Capability 已通过 handle.mark_failed() 明确确认失败时保留 failed
```

Capability 自治决定哪些代码构成一个业务副作用操作。Runtime 不分析工具参数、
不推断异常是否代表外部失败、不保存业务响应。看到已 succeeded 时，Capability 必须
从私有 State 或外部系统查询恢复结果，不能重复调用副作用。

恢复决策：

| recovery_policy | side_effect_policy | 行为 |
| --- | --- | --- |
| automatic | none | 沿用 S2.5 Checkpoint 自动恢复 |
| automatic | idempotent | 自动恢复，复用 Invocation 与 Operation 幂等键 |
| automatic | unsafe | Manifest 加载失败 |
| manual | 任意 | 崩溃后不重放，Run 安全失败 |

manual 安全失败只在崩溃恢复扫描触发：写非空 incomplete AIMessage，提交
`run.failed(code=CAPABILITY_MANUAL_RECOVERY_REQUIRED,retryable=false)`，保留 Task
和 pending Operation。它不影响 automatic 路径，也不替代正常运行中的 Cancel、
Timeout 或 CapabilityError。

## 15.11 Runtime 错误映射

Capability 内部统一错误：

```text
CapabilityError
  code
  message
  retryable
  details
```

`details` 只允许内部受控 JSON，不直接公开或写业务日志。Runtime 使用白名单映射为
Run 级错误；未知异常统一映射 `CAPABILITY_EXECUTION_FAILED`。至少支持：

```text
CAPABILITY_PERMISSION_DENIED
CAPABILITY_BUSY
CAPABILITY_TIMEOUT
CAPABILITY_UNAVAILABLE
CAPABILITY_STATE_VERSION_INCOMPATIBLE
CAPABILITY_REGENERATE_UNSUPPORTED
CAPABILITY_MANUAL_RECOVERY_REQUIRED
CAPABILITY_EXECUTION_FAILED
```

权限为空与健康不可用发生在 Capability 业务执行前：前者不可重试，后者可重试；
所有 Capability 实际返回 OUT_OF_SCOPE 才走 completed/unsupported。
`CAPABILITY_REGENERATE_UNSUPPORTED` 在 regenerate Run 创建前以 HTTP 409 返回，
因此不产生 `run.failed`。

## 15.12 RuntimeEvent 边界

Invocation 事件 payload 最小字段：

```text
invocation_id
capability_id
task_id                 # admission 或 busy 阶段允许 null
requested_task_action
effective_task_action   # Task 解析前允许 null
state_scope
state_schema_version    # Task 解析前允许 null
outcome / error_code    # 仅终结事件
```

Task 和 Invocation 事件均为 `visibility=internal`、`durability=durable`、固定内部
Schema。它们经 Run Sequencer 获得 seq 并持久化 PostgreSQL，但不发布 Redis，
不进入公开事件 Schema。公开事件仍为 S2.5 白名单。

公开 `capability_id` 从固定 Literal 放宽为 Manifest ID 规则约束的字符串，字段名称、
null 语义与事件 `schema_version=1` 不变。前端将其作为普通标签展示，不维护能力
枚举或据此分支业务逻辑。

## 15.13 现有 Capability 迁移

迁移完成后 composition root 不再：

- 构造两个固定 Adapter 并按 ID `if/elif` 分发
- 在 Parent、Router、历史、恢复或公开 Schema 中维护两个 ID 的硬编码集合
- 以 `{session_id}:{capability_id}` 作为新 Child thread ID
- 用固定能力集合判断自动恢复安全性

`general_chat` 与 `en_to_zh` 各自提供 Manifest 和 entrypoint 工厂，继续使用原有
范围判断与业务实现，但返回 AgentResult。默认测试显式授权两者；权限拒绝测试在
Router 前和 invoke 前分别修改权限记录。旧 Parent 公共历史继续有效，旧 Child
Checkpoint 不自动迁移，只在 Session 删除时清理。两个 Manifest 均声明
`state_scope=invocation` 与 `side_effect_policy=none`，继续支持既有 Regenerate。

## 15.14 Workflow 扩展点

Task 只属于一个 Capability，S3 不增加 `workflow_id`、父子 Task、依赖边或编排
状态。当前主键和 Repository API 使用 Task 自身 ID，不把 `(session, capability)`
误作唯一 Task，因此未来可以直接增加 nullable `workflow_id` 及关联表，而无需改变
已有 Task 标识或 State Scope。

未来 Workflow / Complex Task 仍作为独立 Capability，其内部编排多个 SubAgent；
Parent Graph 不承担串并联计划。S3 的 Registry、Task、Operation 和权限不能被包装成
提前实现的 Workflow Engine。

## 15.15 架构调整清单

| 问题 | 采用方案 | 原因 | 是否影响既有架构 |
| --- | --- | --- | --- |
| 公开 capability_id 固定两个枚举 | 放宽为受 Manifest ID 规则约束的字符串，事件版本仍为 1 | 否则新增 Manifest 无法通过产品链路 | 向后兼容扩展公开值域，不改字段结构 |
| 测试 Capability 初始权限 | 不自动授权；测试显式写 allow/deny | 它们不是生产能力，保持默认拒绝 | 不改变权限模型 |
| 无 Router 候选的终态 | 区分 permission denied、unavailable、全部 OUT_OF_SCOPE | 保留授权、运行故障和业务越界语义 | 新增稳定错误码，不改事件结构 |
| Ledger 无逐操作完成协议 | 使用 `ctx.operation()`；未知异常保持 pending，确定失败显式标记 | Invocation 终态无法表示多个副作用结果 | 扩展 RunContext，不改变业务所有权 |
| entrypoint 无依赖注入契约 | 固定为接收 Bootstrap Context 的工厂 | 支持 Fake Model、隔离 Checkpointer 与统一生命周期 | 明确 Manifest 接入契约 |
| Capability 资源关闭 | 通过 Bootstrap Context 注册幂等清理，逆序执行 | S3 无热卸载，不需要额外 shutdown 方法 | 实现级补全 |
| manual policy 无人工入口 | 崩溃后安全 failed，保留 Task/Operation | 防止 Run 永久占用 Session | 仅限定 manual 崩溃恢复 |
| new Task 后 Capability 拒绝 | 回滚 provisional Task 并恢复原 current | 避免错误路由污染任务连续性 | 保持 OUT_OF_SCOPE 零业务副作用 |
| Invocation 无独立表却需稳定恢复 | 以唯一 open started durable event 恢复 invocation_id | 保证幂等键跨重启稳定 | 使用既有 RuntimeEvent 权威链路 |
| S3-00 的 Invocation 事件名未带现有内部前缀 | 落库规范化为 `internal.capability.*` | 保持 S2.5 内部事件校验与公开白名单隔离 | 只调整内部命名，不改事件语义或 SSE |
| 多 active Task 但仅一个 current | S3 只允许继续 current，其他 active Task 暂不可选择 | 已确认不增加任务选择 API | 记录阶段限制，不改模型 |
| 健康 TTL 放置位置未定 | 使用 Runtime 全局配置，不新增 Manifest 字段 | TTL 是平台刷新策略而非业务声明 | 实现级补全 |
| Capability 专属 Manifest 扩展未定义 | S3 禁止任意字段，专属配置经 Bootstrap Context 注入 | 保持统一严格 Schema | 不扩大 Manifest 范围 |
| 数据库迁移工具未规划 | 延续 Repository 幂等 setup，不引入 Alembic | 与现有基线一致，避免阶段外重构 | 实现级选择 |
| 旧 Child thread 命名不兼容 | 不迁移旧私有状态；删除时兼容清理，输入从 Parent 重建 | 两个演示能力可安全重新派生 | 保留 Parent 权威历史 |
| Parent Regenerate 不会 fork Child State | S3 只允许 invocation + none，其余提交前 409 | 防止未来私有状态和副作用被错误重放 | 保留两个测试能力的既有行为 |

## 15.16 S3 非目标

S3 不实现热加载、动态 mount/unmount、DRAINING、Remote Capability、多实例 Registry
同步、多版本实现共存、Workflow、多 Agent 编排、`workflow_id`、RBAC/ABAC、组织或
租户、State 自动迁移、长期审计、Saga/补偿、分布式并发、Worker Lease、管理 API、
管理平台、文件或 RAG 产品能力。

---

# 16. Future 边界

S3 只交付启动期静态 Manifest / Registry 和最小用户权限。动态挂载、DRAINING、
Router confidence 确认、RBAC 和管理平台仍是 Future 候选。多实例 Runtime 必须先
设计 Worker Lease、任务所有权和故障接管，不能直接复用 S2.5/S3 单进程控制器。

未来复杂任务 Agent 仍不应把串并联逻辑塞入 Parent Graph：

```text
Parent Router
→ Complex Task Capability
→ 内部负责多个 Capability / SubAgent 编排
```

这样 Parent Graph 长期保持轻量稳定。后续阶段的名称、顺序和验收范围必须在
Stage 3 完成后重新裁决。
