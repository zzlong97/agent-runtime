# AgentRuntime Requirements

本文档只描述 **What：系统需要实现什么，以及明确不实现什么**。

技术实现方式见 `architecture.md`。
设计原因见 `decisions.md`。
执行进度见 `tasks.md`。

---

# 1. 产品目标

AgentRuntime 用于承载对话型 Agent 任务执行。

核心目标：

- 统一对话入口
- 使用 Parent Graph 调度不同 Child Agent
- 保持 Parent / Child 状态隔离
- 支持多轮对话
- 支持 Capability 越界后重新路由
- 后续可演进为 Capability Runtime

---

# 2. 阶段划分

## Stage 1：最小 Agent Runtime

目标：验证主链路成立。

必须实现：

### 2.0 运行范围

- Stage 1 面向单用户可信环境
- `user_id` 由服务端固定配置提供
- API 不接受客户端任意指定 `user_id`
- 自动化测试默认使用 Fake Model
- 真实模型只用于可选 smoke test

### 2.1 FastAPI 基础服务

- FastAPI 应用可启动
- 提供 `/health`
- 提供 `/api/v1/chat/completions`
- `/api/v1/chat/completions` 使用 HTTP + SSE

最小请求：

```json
{
  "session_id": null,
  "message": {
    "content": "介绍一下 LangGraph"
  }
}
```

`session_id` 可选。`message.content` 必须是非空字符串。

### 2.2 Session 最小能力

Session 至少包含：

```text
session_id
user_id
title
created_at
updated_at
```

规则：

- 请求无 `session_id` 时自动创建 Session
- Session 创建发生在 Router 执行前
- 第一条 HumanMessage 必须被保留
- Router 失败不得删除 Session
- Router 失败不得删除第一条 HumanMessage
- title 直接截取第一条 HumanMessage
- Parent `messages` 是完整公共对话的唯一权威来源
- 上下文裁剪不得删除 Parent 保存的公共历史
- 每个 HumanMessage 和 AIMessage 由服务端分配稳定 UUID

### 2.3 Parent Graph

Parent Graph 至少具备：

```text
START
→ route
→ invoke_capability
→ END
```

Parent Graph State 至少包含：

```text
messages
resolved_capability_id
rejected_capability_ids
```

其中 `rejected_capability_ids` 只记录当前 HumanMessage 已拒绝的 Capability，
每个新请求开始时重置。

### 2.4 Router

Stage 1 Router：

- 只负责 `general_chat` 与 `en_to_zh` 二选一
- 只返回 Top1
- 输出必须结构化
- 只能从本轮未拒绝当前 HumanMessage 的 Capability 中选择
- 输出必须经过 Schema 和候选集校验
- 至少包含：

```text
capability_id
confidence
```

Stage 1 不根据 confidence 做 interrupt。

Router 输出非法或 Router 调用失败属于运行错误，不得解释为
`OUT_OF_SCOPE`。

### 2.5 general_chat

能力范围：

- 普通聊天
- 普通知识问答
- 简单咨询
- 默认简洁、直接回答

禁止：

- 任何翻译任务

遇到翻译请求：

```text
OUT_OF_SCOPE
```

### 2.6 en_to_zh

能力范围：

```text
英文 → 中文
```

必须忠实完整翻译当前 HumanMessage。历史上下文只用于代词、语境和术语
一致性，不得为了满足 `general_chat` 的简短风格而删减原文。

以下任务均返回：

```text
OUT_OF_SCOPE
```

包括：

- 普通聊天
- 中文翻英文
- 其他语言互译
- 代码生成
- 非翻译任务

### 2.7 Capability 连续执行

首次消息：

```text
Router
→ Capability
```

后续消息：

```text
如果当前 Capability 能处理
→ 继续当前 Capability
→ 不重新 Router
```

如果当前 Capability 返回：

```text
OUT_OF_SCOPE
```

则：

```text
返回 Parent Router
→ 对当前 HumanMessage 重新判断
→ 切换到新 Capability
```

有限回流规则：

- 同一条 HumanMessage 对每个 Capability 最多尝试一次
- Child 的范围判断发生在用户可见生成和业务状态持久化之前
- Child 拒绝时不得输出用户可见 token
- Child 拒绝时不得推进自己的消息历史
- Router 必须排除本轮已经拒绝的 Capability
- 所有 Capability 均拒绝后，Parent 返回固定简短的 unsupported 回复
- unsupported 回复写入 Parent 公共历史
- unsupported 回复由 Parent 作为 `message` 事件发送，随后发送
  `done(status="unsupported")`
- unsupported 不是运行错误

### 2.8 上下文窗口

一个完整轮次定义为成功完成的：

```text
HumanMessage + AIMessage
```

规则：

```text
当前 HumanMessage 之前的已完成轮次 < 10
→ 向 Child 传递全部已完成轮次
→ 再加当前 HumanMessage

当前 HumanMessage 之前的已完成轮次 >= 10
→ 向 Child 传递最近 5 个完整轮次
→ 再加当前 HumanMessage
```

System Message 始终保留且不计入轮数。

`unsupported` 和 `incomplete` 消息保留在 Parent 公共历史中，但不计入完整
轮次，也不进入后续模型上下文。裁剪只影响模型输入，不删除 Parent 公共
历史。

### 2.9 Streaming

Stage 1 SSE 至少包含：

```text
message
error
done
```

事件数据至少包含：

```text
message → session_id + message_id + delta
error   → session_id + code + message + retryable
done    → session_id + status
```

`done.status` 只允许：

```text
completed
unsupported
failed
```

已经建立 SSE 后发生运行错误时，必须发送 `error`，随后发送
`done(status="failed")`。

如果模型已经输出部分 token 后失败，Parent 保存聚合后的不完整 AIMessage
并标记为 `incomplete`；它保留在公共历史中，但不进入后续模型上下文。

### 2.10 Checkpointer

要求：

- Parent Graph 使用持久化 Checkpointer
- `general_chat` 使用独立 Checkpointer
- `en_to_zh` 使用独立 Checkpointer
- 数据持久化到 PostgreSQL
- 服务重启后同一 Session 可以恢复对话
- Parent 恢复后仍包含完整公共对话和当前 Capability
- Child 的消息视图由 Parent 上下文重新派生，不得与历史输入重复累加

### 2.11 Stage 1 验收场景

#### Case A：普通聊天

```text
用户：介绍一下 LangGraph
→ general_chat
→ 正常输出
```

#### Case B：普通聊天切换到翻译

```text
当前：general_chat

用户：Translate "How are you?" into Chinese
→ general_chat 返回 OUT_OF_SCOPE
→ Parent Router
→ en_to_zh
→ 输出中文
```

#### Case C：翻译连续对话

```text
当前：en_to_zh

用户：Good morning
→ 直接进入 en_to_zh
→ 不重新 Router
```

#### Case D：翻译切换到普通聊天

```text
当前：en_to_zh

用户：帮我介绍一下 FastAPI
→ en_to_zh 返回 OUT_OF_SCOPE
→ Router
→ general_chat
```

#### Case E：所有 Capability 均拒绝

```text
用户：把“你好”翻译成英文
→ 每个 Capability 最多尝试一次
→ Parent 返回固定 unsupported 回复
→ 不发送 error
→ done.status = unsupported
```

#### Case F：上下文裁剪

```text
完成 10 轮后
→ 仍传递全部 10 个完整轮次

提交第 11 轮 HumanMessage
→ 只传递最近 5 个完整轮次 + 当前 HumanMessage
→ Parent 完整历史不被删除
```

#### Case G：拒绝零副作用

```text
Child OUT_OF_SCOPE
→ 不产生 message 事件
→ 不推进该 Child 消息历史
→ 同一 HumanMessage 在 Parent 中只存在一次
```

---

# 3. Stage 1 明确不实现

Stage 1 不实现：

```text
Capability Manifest
动态 mount/unmount
用户 Capability 权限
RBAC
interrupt / resume
低置信度确认
TopN Router
regenerate
stop
feedback
pin
文件上传
文件解析
RAG
Redis
多实例部署
Run History
管理 API
管理平台
复杂任务 Agent
Deep Agents
Remote Agent
```

---

# 4. Stage 2：完整聊天产品能力与演示页面

Stage 2 目标：在不改变 Stage 1 Parent / Child 边界的前提下，把 Runtime Demo
升级成具备完整产品接口和可直接演示聊天页面的单用户应用。

Stage 2 已于 2026-09-23 验收。本节保留当时的验收基线；其中进程内 active Run、
POST 直连 SSE、同步 Stop 和断线即停止等规则，从 Stage 2.5 开始由第 6 节取代。

Stage 2 继续使用可信单用户环境：`user_id` 由服务端配置固定注入，所有 API
均不得接受客户端指定的 `user_id`。身份认证和多用户数据隔离不在本阶段实现。

## 4.1 Session 列表

```http
GET /api/v1/chat/sessions?cursor={cursor}&limit={limit}
```

要求：

- 使用游标分页，`limit` 默认 20，允许范围为 1～100
- 按 `updated_at DESC, session_id DESC` 稳定排序
- `cursor` 是后端生成的不透明值，客户端不得解析或拼装
- 响应包含 `items` 和可空的 `next_cursor`
- 每个 Session 项至少包含 `session_id`、`title`、`created_at`、`updated_at`

## 4.2 Session 改名

```http
PATCH /api/v1/chat/sessions/{session_id}/rename
```

请求体只包含 `title`。标题去除首尾空白后必须为 1～100 个字符。默认标题
仍由首条 HumanMessage 截取，不调用模型生成标题。

## 4.3 Session 删除

```http
DELETE /api/v1/chat/sessions/{session_id}
```

Stage 2 使用幂等硬删除，成功返回 HTTP 204；目标已不存在时也返回 204。

删除顺序：

```text
存在 active Run
→ 自动 stop 并等待运行结束
→ 删除相关 Child checkpoints
→ 删除 Parent checkpoints
→ 删除 Feedback
→ 最后删除 Session
```

中途清理失败时不得先删除 Session，使同一删除请求可以安全重试。

## 4.4 历史消息查询

```http
GET /api/v1/chat/sessions/{session_id}/messages?before={message_id}&limit={limit}
```

要求：

- 默认返回当前活动 Parent 分支的最新一页，`limit` 默认 50，允许范围为 1～100
- `before` 必须是当前 Session 活动历史中的 `message_id`
- 响应包含按对话时间正序排列的 `items` 和可空的 `next_before`
- Product Message 至少包含 `message_id`、`role`、`content`、
  `runtime_status`、`capability_id` 和当前 `feedback`
- AIMessage 的 `runtime_status` 允许 `completed`、`unsupported`、
  `incomplete`、`stopped`
- `unsupported`、`incomplete`、`stopped` 对用户可见，但不进入后续模型上下文
- 普通历史只返回最新活动分支；Regenerate 前的旧回答保留在旧 checkpoint 中，
  不通过普通历史接口返回
- 后端负责把 Parent 权威消息转换为前端 DTO，不得返回原始
  `StateSnapshot`、节点名称、任务信息或 checkpoint metadata

Stage 1 已持久化但没有 `runtime_status` 的正常 AIMessage 按 `completed` 兼容；
缺少 `capability_id` 时返回 `null`。

## 4.5 单 Session 单 Run

一个 Session 同一时间最多存在一个 active Run。创建新 HumanMessage 和建立
SSE 之前必须先占用 Session；重复请求返回 HTTP 409：

```text
SESSION_BUSY
```

Stage 2 使用单实例进程内协调，不引入 Redis。所有完成、失败、停止和客户端
断开路径都必须释放 active Run。

## 4.6 Stop

```http
POST /api/v1/chat/sessions/{session_id}/stop
```

Stop 只停止当前 Run，不删除 Session、不回滚已有历史。接口必须等待以下工作
完成后再返回：

```text
取消执行
→ 将已输出内容以 runtime_status=stopped 写回 Parent
→ 结束 SSE 生产端
→ 清理 active Run
```

即使尚未产生文本，也保存带稳定 UUID 的 `stopped` AIMessage，使本轮在历史中
可解释。没有 active Run 时幂等成功，历史保持不变。Stop 不是运行失败，不发送
`error` 事件。

## 4.7 Regenerate

```http
POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate
```

要求：

- 只允许重新生成当前活动分支中最新的 `completed` AIMessage
- 使用 checkpoint history 定位该回答生成前、已经包含原 HumanMessage 和
  Parent 路由状态的 checkpoint
- 使用 LangGraph time travel 从该 checkpoint fork 新执行分支，并沿 Parent
  流程继续执行；客户端不能指定或绕过 Capability
- 原 HumanMessage 保持原 `message_id`，新 AIMessage 使用新的服务端 UUID
- 不覆盖原回答，不删除原 checkpoint，不复制 HumanMessage
- 新分支启动后成为活动分支；停止或失败时不自动回滚旧回答
- 第一版不额外维护产品级 `branch_id` / `is_active`，不提供分支浏览或切换 UI
- 接口使用与普通聊天相同的产品 SSE 协议

## 4.8 Feedback

```http
POST /api/v1/chat/messages/{message_id}/feedback
```

请求动作只允许：

```text
like
dislike
cancel
```

只有当前活动分支中的 `completed` AIMessage 可以反馈。以
`user_id + message_id` 作为唯一键，`like` / `dislike` 覆盖旧值，`cancel`
删除当前值，只保留最终反馈。旧分支反馈可以保留，但普通历史不可见。

## 4.9 Stage 2 SSE 协议

外部 SSE 继续只使用：

```text
message
error
done
```

Stage 2 字段：

```text
message → session_id + message_id + capability_id + delta
error   → session_id + code + message + retryable
done    → session_id + message_id + capability_id + status
```

`capability_id` 允许 `general_chat`、`en_to_zh` 或 `null`；`done.status` 允许
`completed`、`unsupported`、`stopped`、`failed`。建立 SSE 后的运行错误仍按
`error → done(failed)` 结束。

浏览器刷新或 SSE 连接意外断开时终止本轮执行，并保存
`runtime_status=incomplete` 的 AIMessage；即使尚未产生文本也保留该状态，
避免留下客户端无法观察或无法解释的后台 Run。

## 4.10 完整聊天演示页面

Stage 2 接口完成后必须提供可直接打开的 `/chat` 页面，不接受仅有后端接口的
验收结果。

前端要求：

- 使用 React + Vite + Ant Design / Ant Design X
- 使用第三方开源消息气泡、输入框、按钮、弹窗、提示和 Markdown 组件，
  不从零开发通用 UI 组件
- HTML、JavaScript/JSX、API 客户端和 CSS 分文件维护，禁止全部内联在一个
  HTML 页面中
- 左侧显示会话列表；右侧显示历史、聊天输入和流式回复；提供可折叠运行状态
  面板
- 支持新建会话、游标加载、选择、改名、二次确认删除、流式聊天、Stop、
  Regenerate、like、dislike 和 cancel
- 状态面板展示 `session_id`、`message_id`、`capability_id`、流式状态与最终
  状态，但不展示提示词、LangGraph 节点或 checkpoint
- 使用安全 Markdown 和代码高亮；禁用未经处理的原始 HTML，并安全处理外链
- 前端不成为消息或运行状态的第二权威源，刷新后必须以 API 返回状态为准
- Vite 构建产物必须保持独立 HTML、带内容哈希的 JS 和 CSS 文件，不使用 CDN
- FastAPI 同源提供页面和静态资源；生产演示只依赖 Python / uv，Node / npm
  只用于前端开发和构建

## 4.11 Stage 2 验收策略

- 后端默认 pytest 使用 Fake Model，不依赖百炼密钥和外部网络
- 前端使用 Vitest + React Testing Library 验证组件与交互
- `npm run build` 验证静态产物拆分，并验证 FastAPI 可以提供 `/chat` 与资源文件
- 提供可选 Playwright + PostgreSQL + Fake Model 浏览器端到端验收
- 真实百炼只作为独立 smoke test；执行前由负责人在项目指定配置文件中配置，
  不作为默认或 CI 门禁

---

# 5. Stage 2 明确不实现

Stage 2 不实现：

```text
Capability Manifest
动态挂载
Capability 权限
管理 API
管理平台
interrupt / resume
文件上传
Deep Agents
复杂任务编排
Remote Capability
```

---

# 6. Stage 2.5：持久化 Run 与可恢复 Runtime

Stage 2.5 是 Stage 2 到后续 Runtime 扩展之间的基础设施重构阶段。它以已经验收的
Stage 2 产品能力为起点，将一次对话执行从 SSE 连接和进程内 Registry 中解耦，形成
可持久、可重连、可取消、可中断和可恢复的 Run。

本阶段继续使用单用户可信环境和固定 `user_id`，只支持单个 Runtime 进程。原
Stage 3 不再是已承诺的正式阶段；其中仍有价值的能力只作为 Future 候选，待
Stage 2.5 完成后依据真实基线重新设计。

## 6.1 持久化 Run

每次普通消息或 Regenerate 必须先创建持久化 Run。Run 至少包含：

```text
run_id
request_id
session_id
thread_id
parent_run_id
run_type = normal | regenerate
input_message_id
response_message_id
start_checkpoint_id
input_payload
request_fingerprint
status
recovery_attempts
seq_high_watermark
error_code
error_message
created_at
started_at
finished_at
updated_at
```

规则：

- `request_id` 全局唯一，由客户端以 UUID 提供
- `run_id`、消息 ID 等业务标识由服务端生成
- 相同 `request_id` 与相同规范化请求返回原 Run，不重复执行
- 相同 `request_id` 对应不同请求时返回 HTTP 409
- `parent_run_id` 只在能够定位来源 Run 时记录；Stage 2 历史不补建 Run
- `start_checkpoint_id` 仅允许在全新空 Session 中为空
- 普通 Run 的恢复输入只包含重放所需的最小数据，不包含 System Prompt、完整模型
  请求、凭据或无关上下文
- Run 终止时清除 `input_payload` 中的正文和恢复数据，只保留请求指纹、稳定 ID 和
  非敏感执行摘要
- `input_payload` 不得通过公开 API、SSE 或业务日志返回

活动状态：

```text
queued
running
recovering
interrupted
cancel_requested
```

终态：

```text
completed
failed
cancelled
```

终态不可修改。PostgreSQL 必须保证同一 Session 同时最多存在一个活动 Run。
`unsupported` 属于 Run 的 `completed`，区别保存在公共消息状态中。

## 6.2 异步 Run API

普通消息与 Regenerate 不再通过原 POST 响应直接承载 SSE。成功接受请求后返回
HTTP 202，至少包含：

```text
run_id
session_id
response_message_id
status
```

Stage 2.5 产品接口至少包括：

```http
POST /api/v1/chat/completions
POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate
GET  /api/v1/chat/sessions/{session_id}/active-run
GET  /api/v1/chat/runs/{run_id}/events
POST /api/v1/chat/runs/{run_id}/cancel
POST /api/v1/chat/runs/{run_id}/resume
```

Session 当前活动 Run 接口只返回公开摘要，用于页面刷新后的恢复，不返回
Checkpoint、输入正文或内部执行状态。原 Session Stop 接口不再作为公开产品接口；
Session 删除仍在内部取消并等待活动 Run。

## 6.3 RuntimeEvent

RuntimeEvent 至少包含：

```text
event_id = UUIDv4
run_id
seq
event_type
source
visibility = public | internal
payload
schema_version
durability = durable | transient
created_at
```

不引入独立 `trace_id`。链路关联分别使用 `request_id`、`run_id` 和 `session_id`。
`session_id` 通过 Event → Run 查询，不在事件中重复作为权威关联字段。

事件规则：

- 每个 Run 使用持久化序号高水位分段预留 `seq`
- 所有并发事件通过单个 Run Sequencer 串行分配序号
- 序号不得重复或倒退，但允许因分段预留而存在缺号
- Run 状态变化与对应持久 RuntimeEvent 必须在同一 PostgreSQL 事务中提交
- `payload` 必须按事件类型使用固定 Schema，不允许把任意内部 JSON 原样透传
- `node_id`、Checkpoint、Prompt、模型完整请求、工具参数和原始结果只能属于内部信息
- SSE 只输出经过白名单和字段过滤的 `public` 事件

最小公开事件集合：

```text
run.started           durable
message.started       durable
message.delta         transient
message.finalized     durable
interrupt.required    durable
interrupt.resumed     durable
run.completed         durable
run.cancelled         durable
run.failed            durable
```

`message.started` 在初次生成和每次恢复生成前发送，包含稳定
`response_message_id` 和递增的 `attempt`。页面收到更大的 `attempt` 时必须清空旧的
未完成文本，防止恢复前后的增量混合。每个 Run 必须且只能产生一个终态
`run.*` 事件。

完整 AIMessage 不复制到 RuntimeEvent；`message.finalized` 只携带消息 ID、状态、
Capability 等公开摘要，完整内容继续以 Parent `messages` 为唯一权威来源。

## 6.4 Redis 与 SSE 续传

Redis Streams 只承载短期公开实时事件：

```text
key = runtime:events:{run_id}
Redis Stream ID = {seq}-0
```

Redis 通过根目录 `compose.yaml` 和 Windows Docker Desktop 部署，只绑定本地环境，
不配置持久化命名卷。每次成功写入 Stream 都刷新 30 分钟 TTL，终态事件成功写入
后再次刷新。

发布顺序：

```text
持久事件与 Run 状态提交 PostgreSQL
→ 尽力发布 Redis
```

S2.5 不实现 Transactional Outbox。Redis 启动失败或运行中断不得阻止 Run 执行：
持久事件由 PostgreSQL 提供，瞬时 `message.delta` 允许丢失，终态后页面从 Parent
历史读取完整回复。Redis 恢复后不补建已经丢失的瞬时增量。

SSE 规则：

- SSE `id` 等于事件 `seq`
- 浏览器自动重连使用 `Last-Event-ID`
- 页面刷新或主动重连可使用 `after_seq`；两者同时存在时以 `Last-Event-ID` 为准
- 网关始终合并 PostgreSQL 持久事件和 Redis 实时事件，按 `seq` 严格递增并去重
- 数字不连续是合法情况，不能仅根据缺号判断丢失
- SSE 心跳每 20 秒使用注释帧，不分配 `seq`，也不持久化
- Run 已终止且没有更新事件时，SSE 正常关闭
- 网关有限批次读取事件，不允许每连接使用无界队列
- 慢客户端或写入超时时只关闭 SSE，不取消 Run

## 6.5 Checkpoint、终态与崩溃恢复

Parent 继续使用 `thread_id=session_id`。每个由 Run 产生的 Checkpoint 必须在
metadata 中关联 `run_id`，恢复时使用该 Run 的精确最新 Checkpoint，不使用模糊的
Session 最新状态，也不在 Run 中保存 `last_checkpoint_id`。

持久化顺序：

```text
最终 AIMessage 或 interrupt Checkpoint
→ Run 状态、Interrupt 投影与持久 RuntimeEvent 的 PostgreSQL 事务
→ Redis 尽力发布
```

不允许在最终消息或中断 Checkpoint 尚未持久化前对外宣告 Run 已完成或已中断。
如果进程在 Checkpoint 后、Run 投影前崩溃，恢复流程必须通过稳定
`response_message_id`、`interrupt_id` 和 Checkpoint metadata 补齐投影，不得重新
调用已经完成的模型节点。

如果 Run 尚无自己的 Checkpoint，则使用 `start_checkpoint_id + input_payload`
重新开始。接口返回 202 前，只要求新 Session（如有）、Run、幂等字段、恢复输入
和稳定消息 ID 已在同一业务事务中持久化，不要求执行 Checkpoint 已经生成。

执行协调规则：

- Run 提交后立即尝试唤醒本地执行器
- 单进程轻量协调器定期补偿仍为 `queued` 且未被本地执行的 Run
- 应用启动时扫描并处理非终态 Run
- `queued` 正常执行；`running`、`recovering` 进入恢复；`cancel_requested` 完成
  取消；`interrupted` 继续等待用户操作
- 初次执行不计入 `recovery_attempts`；每次重启接管前递增
- 最多允许三次恢复，第三次仍失败则进入 `failed`

S2.5 只承诺“至少执行一次”，不承诺“恰好执行一次”。可能产生外部副作用的
Tool 或 SubAgent 必须先提供幂等键或操作账本，否则不得进入自动恢复流程。通用
补偿事务不在本阶段实现。

## 6.6 Cancel、断线与公共消息终态

SSE 或浏览器断开不再取消 Run。刷新、关闭页面或网络中断后，Run 继续执行；只有
显式 Cancel、Session 删除或执行器失败才停止 Run。

Cancel 规则：

- 接受取消并持久化 `cancel_requested` 后立即返回 HTTP 202
- 执行器先协作式取消，超过可配置宽限时间后强制取消本地任务
- `queued` 或 `interrupted` 可以直接进入 `cancelled`
- Cancel 与正常完成并发时由首个数据库终态获胜
- 重复 Cancel 返回当前状态，保持幂等
- 不承诺回滚已经发生的外部副作用

公共消息不得以空内容结束：

- `completed` 必须有非空回复，否则按失败处理
- `unsupported` 使用 Parent 生成的固定非空提示
- 失败时保存部分输出或固定失败提示，状态为 `incomplete`
- 取消时保存部分输出或固定停止提示，状态为 `stopped`
- 所有结果使用预分配的 `response_message_id`
- 只有 `completed` HumanMessage + AIMessage 完整轮次进入后续模型上下文

## 6.7 Interrupt / Resume

S2.5 使用最小 `run_interrupts` 持久化投影。至少记录：

```text
run_id
interrupt_id
status
interrupt_payload
resume_payload
resume_request_id
created_at
resumed_at
cancelled_at
```

规则：

- `(run_id, interrupt_id)` 唯一
- 每个 Run 同时最多一个待处理中断，但允许顺序发生多次中断
- Resume 必须携带准确 `interrupt_id` 和恢复请求 ID
- 同一恢复请求重复提交返回原结果，不同内容复用同一请求 ID 返回 HTTP 409
- 已恢复、已取消或不属于该 Run 的中断不能再次恢复
- Resume 通过事务和行锁保证只成功一次，不创建新的 HumanMessage
- `interrupt.required` 持久化并发送后，当前 SSE 正常关闭
- Run 保持 `interrupted` 并继续占用 Session，直到 Resume 或 Cancel
- Resume 沿用原 `run_id`，页面使用 `last_seq` 重新连接
- 多审批人、审批收件箱、超时策略和并行中断留到后续阶段

## 6.8 最小 Agent 执行契约

现有 Child Agent 通过统一边界接收：

```text
RunContext
AgentContext
TaskInput
```

Agent 只通过注入的事件出口发送已定义事件，不能直接访问 SSE、Redis、浏览器连接、
Session 生命周期或顶层 Run 状态。最终控制结果继续遵守极薄 `ChildResult` 约束，
完整用户可见消息仍写回 Parent。

本阶段只使用现有 Capability 和 Fake Agent 验证契约，不实现动态 Registry、Manifest、
权限系统、多 Agent 调度或远程 Agent。

## 6.9 Session 删除与数据保留

Session 使用彻底硬删除：

```text
阻止创建新 Run
→ 取消并等待活动 Run 结束
→ 收集该 Session 的 run_id
→ 删除 Interrupt / RuntimeEvent / Run
→ 删除 Feedback 与 Child / Parent Checkpoint
→ 最后删除 Session
```

Redis Stream 尽力删除；Redis 不可用时由 TTL 兜底，不阻塞 Session 删除。
PostgreSQL 持久 RuntimeEvent 在 S2.5 不做全局定时清理，只随 Session 删除。

## 6.10 `/chat` 演示页面

Stage 2.5 的后端接口完成后，必须同步升级已有 `/chat` 页面：

- 展示 `queued`、`running`、`recovering`、`interrupted`、`cancel_requested` 和终态
- 支持 Run 事件连接、断线重连、刷新恢复、Cancel 和 Resume
- 终态后重新读取 Parent 公共历史，前端不成为第二权威源
- 继续使用 React、Vite、Ant Design / Ant Design X 和第三方开源通用组件
- HTML、JavaScript/JSX、API 客户端和 CSS 继续分文件构建
- 不展示 Prompt、节点、Checkpoint、工具原始参数或内部事件

开发环境可以显式启用确定性 Runtime 演示模式，以真实 Run、SSE、Interrupt 和
Resume 链路演示正常完成、等待确认、失败和慢速输出。演示模式默认关闭，不进入
`general_chat` / `en_to_zh` 路由，也不构成正式业务 Capability；前端不得伪造结果。

## 6.11 Stage 2.5 验收策略

- 后端默认使用 Fake Model，真实百炼仍只作为独立可选 smoke test
- PostgreSQL 集成验收覆盖 Run、Event、Interrupt、幂等和活动 Run 唯一约束
- Redis 验收覆盖正常流式、启动时不可用、运行中中断和 TTL
- 恢复验收覆盖 Run 入库后未启动、流式输出中重启、Checkpoint 后投影前崩溃和
  三次恢复上限
- SSE 验收覆盖 PG/Redis 合并、游标续传、合法缺号、去重和慢客户端
- 页面验收覆盖刷新重连、Cancel、Interrupt/Resume、失败及终态历史回读
- Session 删除验收覆盖活动 Run、Interrupt、Event、Checkpoint 和 Redis 清理
- 测试不得依赖真实模型输出的确定性

## 6.12 Stage 2.5 明确不实现

```text
多 Runtime 实例
Worker Lease
分布式任务队列
Transactional Outbox
通用副作用补偿事务
完整 Run History 产品界面
多审批人或并行中断
Capability Manifest / 动态 Registry
动态 mount / unmount
Capability 权限 / RBAC
多 Agent 编排
Remote Agent
文件与 RAG
```

---

# 7. Future：候选方向，不构成阶段承诺

Stage 2.5 完成后，必须根据实际实现和验收结果重新设计后续阶段。以下内容只表示
可能有价值，不代表已经确认的 Stage 3 范围或顺序。

## 7.1 Capability Runtime 候选

- Capability Manifest
- Capability Registry
- 本地动态 mount / unmount
- DRAINING 优雅卸载
- Capability 权限与后续 RBAC
- Router confidence 与确认策略
- 多 Agent / Complex Task 编排
- Remote Capability / Remote Agent

## 7.2 Runtime 治理候选

- 多实例 Runtime、Worker Lease 与分布式任务队列
- Transactional Outbox
- Run History 产品能力与独立保留策略
- 多审批人、审批收件箱、超时策略和并行中断
- Observability、分布式 Trace 与 Evaluation
- 高级 SSE 流控与自适应限速

## 7.3 文件能力候选

文件上传、对象存储、解析、摘要、Embedding 和 RAG 均未进入已确认阶段；其
生命周期、消息引用和 Capability 责任边界必须在未来单独设计。

## 7.4 其他平台候选

- Capability Admin API
- 管理平台
- 多租户
- Capability 配置数据库化
- Deep Agents
- Context Engineering
