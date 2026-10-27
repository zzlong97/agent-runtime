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

本阶段继续使用单用户可信环境和固定 `user_id`，只支持单个 Runtime 进程。S2.5
设计时撤销了旧版 Stage 3 承诺；S2.5 验收后已经依据真实基线重新裁决为本文第 7
节的新 Stage 3，两者不得混用。

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

# 7. Stage 3：Capability Runtime

Stage 3 以已验收的 Stage 2.5 持久 Run、RuntimeEvent、恢复与独立 SSE 为基础，
把两个硬编码演示能力迁移到统一 Capability Runtime。S3 仍是单用户可信环境、固定
`user_id` 和单 Runtime 进程，不建设完整 Agent Platform。

## 7.1 Capability Manifest 与 Registry

S3 必须实现：

- 进程内 Capability Registry；应用启动后形成不可热变更的 Registry Snapshot
- 本地 YAML Manifest Source，并预留最小 `CapabilitySource` 抽象
- 严格、拒绝未知字段的统一 Manifest Schema
- `entrypoint` 动态导入统一 Capability 工厂
- 一个 `capability_id` 只允许一个活动实现
- 重复 `capability_id` 的全部冲突实现隔离，其他合法 Capability 正常启动
- 单个 Manifest、entrypoint、初始化或健康检查失败只隔离对应 Capability
- S3 不提供热加载、mount、unmount、DRAINING 或管理 API

Manifest 至少描述：

```text
manifest_schema_version
capability_id
name
description
enabled
entrypoint
version
state_scope
state_schema_version
compatible_state_schema_versions
allow_degraded
concurrency
execution
recovery_policy
side_effect_policy
```

Router 只能看到：

```text
capability_id
name
description
enabled
```

Prompt、Tool、Child State、Graph 拓扑、健康详情和运行策略均不得进入 Router 输入。

`capability_id` 不再限制为两个固定枚举。公开消息、历史接口和 RuntimeEvent 中的
该字段必须接受符合 Manifest ID 规则的字符串；既有字段结构与 RuntimeEvent
`schema_version=1` 保持不变。

## 7.2 统一 Capability 协议

Manifest `entrypoint` 必须指向统一工厂：

```text
create_capability(CapabilityBootstrapContext) -> Capability
```

Bootstrap Context 只提供受控基础设施，包括只读 Manifest、隔离 Child
Checkpointer、模型工厂、非敏感 Capability 配置视图和资源清理注册能力。禁止向
Capability 暴露 SSE、Redis、Session 生命周期、顶层 Run Repository 或 Run 终态
写入能力。

Capability 统一实现：

```text
initialize()
health_check() -> HealthResult
invoke(RunContext, AgentContext, TaskInput) -> AgentResult
```

底层实现可以使用 LangGraph SubGraph、`create_agent` 或自定义 Python Agent，但
其私有 State、Schema 和拓扑不得暴露给 Parent、Router 或 Registry。

最小 `AgentResult` 包含：

```text
status
content
metadata
```

要求：

- `completed` 必须返回非空 `content`
- `rejected` 必须零用户可见输出，并以 metadata 表达 `OUT_OF_SCOPE`
- `content` 是本次成功调用的最终规范文本；流式 EventOutlet 只提供实时投影
- Runtime 负责把最终完整 AIMessage 写入 Parent 权威历史
- Capability 失败使用 `CapabilityError`，取消继续传播协作式取消语义
- Task 终态意图通过结构化 metadata 表达，真正更新只能由 Runtime 执行

## 7.3 Router、权限与错误结果

Router 候选必须同时满足：

```text
Manifest enabled
+ Registry 加载成功
+ Health 可服务
+ 当前固定用户 allowed=true
+ 本轮尚未拒绝该 HumanMessage
```

Router 输出至少包含：

```text
capability_id
task_action = continue | new
```

没有明显新意图时优先沿用当前 Capability，但能力连续性不等于 Task 连续性。
Router / Parent 不得直接选择或修改 `task_id`。

权限使用 PostgreSQL `user_capability_permissions`，规则固定为：

```text
无记录       → deny
allowed=false → deny
allowed=true  → allow
```

权限在 Router 候选构造前过滤，并在 Invocation 开始前再次强校验，不使用缓存。
`general_chat` 与 `en_to_zh` 只是标准测试 Capability，不自动获得授权；测试和验收
显式写入 allow / deny 数据。

公开结果必须区分：

- 没有授权候选：Run `failed`，`CAPABILITY_PERMISSION_DENIED`，不可重试
- 有授权但 Capability 不可服务：Run `failed`，`CAPABILITY_UNAVAILABLE`，可重试
- 已调用的所有候选均返回 `OUT_OF_SCOPE`：Run `completed`，公共消息为
  `unsupported`

## 7.4 Invocation 与 RuntimeEvent

同一 Run 可以顺序调用多个 Capability，也可以多次调用同一 Capability。每个进入
调用生命周期的尝试使用唯一 `invocation_id`。S3 不建立 Invocation 表，生命周期
必须写入 internal durable RuntimeEvent：

```text
internal.capability.invocation.started
internal.capability.invocation.completed
internal.capability.invocation.failed
internal.capability.invocation.cancelled
```

同一 Run 同时最多存在一个未结束 Invocation。自动恢复必须从未结束的
`internal.capability.invocation.started` 复用原 `invocation_id`，不得因进程重启生成新
ID。
这些事件不得进入公开 SSE；现有公开事件白名单和字段结构保持不变。

## 7.5 Capability Task

S3 必须建立：

```text
capability_tasks
capability_task_contexts
```

Task 是单个 Capability 的长期任务标识。一个 Invocation 必须关联一个 Task，Task
可以跨 Run 延续；Task 与 Run 生命周期解耦，Run 失败或取消不得自动终止 Task。

Task 状态只允许：

```text
active
completed
failed
cancelled
```

同一 `session_id + capability_id` 可以存在多个 active Task，但 Context 只保存一个
`current_task_id`。S3 不提供选择非 current Task 的产品 API。

规则：

- `new` 创建 Task 并切换 current，二者必须在同一事务
- `continue` 使用 current Task
- `continue` 但 current 不存在时降级为 `new`，并记录 internal diagnostic
- Task 进入终态与清理其 current context 必须在同一事务
- Task 表不保存业务 payload
- Task 生命周期历史复用 internal durable RuntimeEvent，不建独立历史表
- 当前不增加 `workflow_id`，但未来增加 nullable `workflow_id` 不应要求重构

`task_action=new` 后若 Capability 合法返回 `OUT_OF_SCOPE`，Runtime 必须回滚本次
未被接受的新 Task，恢复原 current Task，并清理本次临时 Child thread。Capability
必须在范围判断前保持零公开输出、零 Operation 和零业务 State 推进；违反该规则
按执行失败处理，不执行拒绝回滚。

## 7.6 State Scope 与版本兼容

Manifest 必须显式声明且不得默认推断：

```text
state_scope = invocation | run | session
```

Runtime 使用带 namespace/version 的确定性 Child `thread_id`：

- `invocation`：每个 Invocation 独立
- `run`：同一 Run 内同一 Capability 共享
- `session`：通过 `task_id` 跨 Run 共享

Task 创建时固化 `state_schema_version`。Capability 实现升级后：

- 当前版本始终可继续
- 旧 Task 仅在其版本出现在 `compatible_state_schema_versions` 时可继续
- 不兼容时返回 `CAPABILITY_STATE_VERSION_INCOMPATIBLE`
- Task 保持原状态，不自动失败、迁移或改写版本
- S3 不允许同一 Capability 的多版本实现并存

### 7.6.1 Regenerate 安全边界

S3 不把 Parent Checkpoint Fork 等同于 Capability 私有 State Fork。Regenerate 仅允许
目标 Capability 同时满足：

```text
state_scope = invocation
side_effect_policy = none
```

提交前必须从原回答确定 Capability，并复查 Registry、Health、权限与 State Schema；
不重新调用 Router。合法请求复用当前 Task（不存在时按既定规则降级 new），创建新的
`invocation_id` 和独立 Child thread，输入继续来自 Parent Checkpoint Fork。

其他 Capability 在创建 Run 前返回 HTTP 409
`CAPABILITY_REGENERATE_UNSUPPORTED`。`general_chat` 与 `en_to_zh` 的 S3 Manifest
使用 `invocation + none`，继续承接现有 Regenerate 验收。

## 7.7 Health、并发与超时

健康状态只允许：

```text
healthy
degraded
unhealthy
```

应用启动时强制检查每个已加载 Capability，随后使用 Runtime 配置的
HealthSnapshot TTL；TTL 过期后在需要服务时刷新。具体依赖检查由 Capability 自治，
Runtime 只消费标准 HealthResult。

服务规则：

- `healthy` 可服务
- `degraded + allow_degraded=true` 可服务并记录内部告警
- `degraded + allow_degraded=false` 不可服务
- `unhealthy` 不可服务

并发和超时由 Runtime 按 Manifest 统一执行。达到并发上限后有限等待，获取超时
返回 `CAPABILITY_BUSY`。执行超时先协作式取消，经过 `cancel_grace_seconds` 仍未
退出时强制取消本地 asyncio Task，并返回 `CAPABILITY_TIMEOUT`。

## 7.8 Recovery、Side Effect 与 Operation Ledger

Manifest 必须显式声明：

```text
recovery_policy = automatic | manual
side_effect_policy = none | idempotent | unsafe
```

合法组合：

```text
automatic + none       → 允许自动恢复
automatic + idempotent → 允许自动恢复，但所有副作用必须使用 Operation Ledger
automatic + unsafe     → Manifest 非法并隔离
manual + 任意策略      → 不自动重新执行
```

`manual` Capability 发生崩溃恢复拒绝时，Run 必须保存非空 `incomplete` 消息并以
`CAPABILITY_MANUAL_RECOVERY_REQUIRED` 进入 failed；Task 与 pending Operation
保持不变。该错误只适用于 `recovery_policy=manual`，不得影响
`automatic + none/idempotent` 的自动恢复。S3 不提供 manual recovery 产品入口。

S3 必须建立 `capability_operations`。Capability 自治定义业务副作用边界和稳定
`operation_key`，Runtime 只提供：

```text
ctx.idempotency_key(operation_key)
ctx.operation(operation_key)
Operation Ledger
```

幂等键必须由 `run_id + invocation_id + operation_key` 确定性生成。`ctx.operation()`：

- 进入前创建或复用 `pending`
- 已 `succeeded` 时禁止再次执行副作用
- 已 `failed` 时默认禁止自动重试
- 正常完成时标记 `succeeded`
- 普通异常不自动推断失败，结果未知时保持 `pending`
- 只有 Capability 能确定业务操作失败时，才显式标记 `failed`

Ledger 不保存业务响应 payload。Capability 必须通过自己的私有 State 或外部系统
查询恢复所需业务结果。S3 不实现 Saga、补偿事务或人工 Operation 处置界面。

## 7.9 数据所有权与删除

数据所有权保持：

```text
Parent Checkpoint             → 完整公共消息
Capability Child Checkpoint   → 私有业务 State
capability_tasks              → Task 生命周期和最近执行投影
capability_task_contexts      → 当前 Task 指针
capability_operations         → 外部副作用幂等账本
RuntimeEvent                  → Invocation / Task 生命周期历史
Vector / Document Store       → Capability 自有 RAG 数据
```

Session 删除继续遵守 S2.5 屏障和最后删除 Session 的规则，并增加清理：

```text
capability_task_contexts
capability_operations
capability_tasks
所有新命名规则及遗留命名规则的 Child Checkpoint
```

当前不保留独立审计副本。

## 7.10 现有 Capability 迁移与验收

`general_chat` 与 `en_to_zh` 必须各自提供标准 Manifest 和统一 entrypoint 工厂，
通过 Registry、权限、健康、Task、Invocation 和 Runtime 调用，不保留 Parent 中按
固定 ID 分支的调用代码。它们仅作为测试与回归样例，不视为生产能力。

S3 验收至少覆盖：

- Manifest 严格校验、重复 ID 全部隔离及局部故障隔离
- Registry Router 投影、开放 capability_id 与权限双检
- Task 创建、继续、降级 new、终态及 OUT_OF_SCOPE 回滚
- 三种 State Scope、确定性 thread_id 和版本不兼容拒绝
- Health TTL、degraded 策略、并发等待与两阶段超时取消
- automatic 恢复复用 invocation_id 和幂等键
- manual 恢复安全失败
- Operation pending / succeeded / failed / 结果未知语义
- Regenerate 仅允许 invocation + none，拒绝路径不创建 Run 或 Task
- 两个现有测试 Capability 的完整聊天、回流、恢复和页面链路
- Session 删除新增数据与动态 Child Checkpoint 清理
- Stage 1、Stage 2 和 Stage 2.5 全量回归

## 7.11 Stage 3 明确不实现

```text
热加载 / 动态 mount-unmount / DRAINING
Remote Agent / Remote Capability
多实例 Registry 同步
多版本 Capability 实现共存
Workflow / 多 Agent 编排 / workflow_id
RBAC / ABAC / 组织或租户权限
长期审计留存
State 自动迁移
Saga / 补偿事务
分布式 Capability 并发控制
Worker Lease / 分布式任务队列
Capability Admin API / 管理平台
文件、RAG 产品能力
```

---

# 8. Future：候选方向，不构成阶段承诺

Stage 3 完成后，必须根据实际实现和验收结果重新设计后续阶段。以下内容只表示
可能有价值，不代表已经确认的 Stage 4 范围或顺序。

## 8.1 Capability Runtime 候选

- Capability 动态 mount / unmount
- DRAINING 优雅卸载
- Capability Admin API 与后续 RBAC
- Router confidence 与确认策略
- 多 Agent / Complex Task 编排
- Remote Capability / Remote Agent（规划见第 9 节，仍未排期）

## 8.2 Runtime 治理候选

- 多实例 Runtime、Worker Lease 与分布式任务队列
- Transactional Outbox
- Run History 产品能力与独立保留策略
- 多审批人、审批收件箱、超时策略和并行中断
- Observability、分布式 Trace 与 Evaluation
- 高级 SSE 流控与自适应限速

## 8.3 文件能力候选

文件上传、对象存储、解析、摘要、Embedding 和 RAG 均未进入已确认阶段；其
生命周期、消息引用和 Capability 责任边界必须在未来单独设计。

## 8.4 其他平台候选

- Capability Admin API
- 管理平台
- 多租户
- Capability 配置数据库化
- Deep Agents
- Context Engineering

---

# 9. Remote Agent 候选阶段：已完成规划，未授权实施

本节是 Stage 3 之后的候选阶段基线。负责人已同意完成规划，但尚未决定它与其他候选
能力的实施优先级；因此本节不修改 Stage 3 禁止范围，也不授权编码、联调、远程 Agent
改造、提交或推送。后续只有负责人明确选定本候选阶段并批准开始后才能实施。

## 9.1 目标与产品形态

远程 Agent 独立部署，通过标准远程协议接入 AgentRuntime，并作为普通 Capability
进入现有 Router、Run、独立 SSE、公共消息和 `/chat` 页面，不增加旁路聊天入口。
首个真实接入对象为：

```text
D:\workspace\semantic-pattern-mining\semantic-pattern-mining
```

接入完成后，用户必须能在现有聊天页面观察远程执行、补充文本参数、取消执行和查看
最终非空回答；不能只完成不可见的后端调用。

AgentRuntime 团队不得修改该远程 Agent 仓库。Agent 侧适配由对方开发团队完成，
当前团队只负责 AgentRuntime 侧通用远程接入设施、Fake A2A 测试与集成验收。

## 9.2 协议与接入规范

远程接入采用 A2A 1.0 协议语义和 AgentRuntime 强制兼容 Profile，不把目标 Agent
当前私有 `POST /api/agent/invoke` 当作正式 Runtime 契约，也不再自创一套同类协议。
首期 Profile 固定：

- 使用静态配置的 Agent Card URL 完成启动期发现；不做动态注册或热加载。
- 只实现 HTTP+JSON/REST 绑定；不实现 JSON-RPC、gRPC 和 Push Notification。
- `SendMessage`、`GetTask`、`CancelTask` 为必选；首期以 `GetTask` 有界轮询获取状态，
  不承诺远程 token 流式输出。
- Runtime 以非阻塞方式 SendMessage；长任务必须尽快返回可查询的 `taskId/contextId`，
  不允许一直占用初次 HTTP 连接直到业务完成。
- 初次发送的 A2A `messageId` 由本地 `invocation_id` 确定性生成；重复提交相同内容必须
  返回同一远程 Task，不得重复执行业务；相同 ID 对应不同内容必须冲突拒绝。
- Runtime 必须持久记录本地 Invocation 与远程 `taskId/contextId` 的绑定，恢复不得
  创建新的本地 Run 或新的远程业务任务。
- 远程 `input-required` 映射为现有 Run Interrupt；恢复沿用同一 Run、Invocation、
  远程 Task 和 context，不新增 HumanMessage。
- 远程终态必须返回至少一个非空文本 Artifact；结构化数据可由 Agent 自治保存，
  首期不进入 Parent 公共消息或产品 API。
- 只有显式、受控的 `OUT_OF_SCOPE` 控制元数据可映射为本地合法拒绝；普通远程拒绝、
  协议错误或权限错误均不得伪装成 unsupported。

首期远程 Capability 只允许 `state_scope=invocation`。Runtime 只发送当前
HumanMessage 的文本，不发送 System Message、完整 Parent 历史、Checkpoint、私有
State 或工具数据。远程 Agent 在同一 A2A Task 内维护补参上下文；跨 Run 的远程会话
连续性留待后续单独设计。

## 9.3 权威源与生命周期

本地 PostgreSQL Run 继续是 AgentRuntime 执行生命周期权威源，Parent `messages`
继续是完整公共消息唯一权威源；远程 A2A Task 只是对应 Invocation 的外部执行实体。
远程 Agent 自治保存其业务 State、Checkpoint、任务内部消息、结构化结果和业务数据，
不得写入本地 Parent、SSE、Redis、Session 或顶层 Run 终态。

本地 Run 只有在远程终态已确认、最终文本已写回 Parent 后才能 completed。网络超时或
进程崩溃导致结果未知时，不得猜测远程失败或重新创建任务；必须凭持久绑定执行
`GetTask`，必要时使用同一 `messageId` 幂等重送。无法确认的外部操作保持 pending，
本地 Run 保持 `recovering` 或 `cancel_requested` 并继续阻塞同 Session 新 Run，不得
宣称 failed、cancelled 或 completed。只有确认远程 failed/cancelled/completed 后才
进入对应本地终态。

S2.5 的 3 次自动恢复上限继续适用于本地执行，但不得把仍有未确认远程 Task 的 Run
强制置为 failed。该类 Run 允许 `recovery_attempts > 3`，按有界退避继续 GetTask 或
幂等重送，直到远程终态可确认；公开字段仍表示实际接管次数，不再对远程 Run 限制为 3。

显式 Cancel 和 Session 删除都必须先对已绑定的活动远程 Task 执行 `CancelTask` 并
确认远程终态。远程状态仍未知时，Session 删除必须拒绝并保留本地绑定，不得先删除
恢复证据。远程 Task 和业务数据的长期删除由远程 Agent 自身策略负责；首期不提供
跨系统数据删除协议。

## 9.4 健康、安全与错误

- 启动健康检查以 Agent Card 可读取、Schema 合法且存在兼容接口为最低条件；运行期
  继续使用现有 Health TTL、并发、获取超时和执行超时治理。
- 远程 URL 只允许 HTTP/HTTPS，不允许 URL 中携带凭据；非开发环境应使用 HTTPS。
- 首期认证只支持显式 `none` 和静态 Bearer Token。`none` 只允许显式开发配置；
  Token 只能由环境配置引用，不得写入 Manifest、RuntimeEvent、日志或公开响应。
- Agent Card 返回的调用地址必须与已配置来源同源，或由配置显式允许；不得无条件
  跟随 Agent Card 指向任意地址。
- 远程不可用、认证失败、协议不兼容和任务失败必须使用不同稳定错误码；状态未知与
  取消未确认只能作为非终态诊断，不能触发 `run.failed`。只有可安全重试的网络/服务
  暂态错误可标记 retryable。

## 9.5 文本补参与页面要求

现有 Resume API 的对象载荷保持兼容，但公开 Interrupt 需要增加关闭的展示类型：

```text
confirmation → prompt + approved(bool)
text         → prompt + answer(non-empty string)
```

`/chat` 页面必须使用现有第三方组件支持两种类型。远程 Agent 缺少日期等参数时使用
`text`，用户提交后沿用同一 Run 恢复；每个 Run 同时仍最多一个 pending Interrupt。
首期不实现动态表单、多字段 Schema、文件补充或并行审批。

## 9.6 首个远程 Agent 的外部适配要求

`semantic-pattern-mining` 对方团队至少交付：

- A2A 1.0 Agent Card 和 HTTP+JSON/REST 服务端接口；原私有接口可保留，但 Runtime
  不依赖它。
- 持久 A2A Task、`taskId/contextId` 查询、可靠 Cancel，以及进程重启后的任务查询。
- 非阻塞接收长任务并尽快返回 Task；不得把当前同步长连接简单换路径后冒充 A2A。
- 按 `messageId + 请求指纹` 实现幂等提交；相同 ID 不重复执行，不同内容返回冲突。
- 把现有 `needs_input` 转换为非空文本 `input-required`，并在同一 Task/context 接收
  用户补充内容。
- completed 时生成非空用户可读文本 Artifact；当前结构化 `data` 留在 Agent 侧。
- 最终文本必须明确本次实际采用的日期范围和结果摘要，使未写入 Parent 的 Resume 文本
  不会造成后续公共对话缺失关键执行参数。
- 明确任务失败、取消、拒绝和认证错误，不把未知结果报告成确定失败。

## 9.7 接入成本统计

验收报告必须区分“一次性 Runtime 通用建设成本”和“单个 Agent 适配成本”，至少记录：

- 两个仓库各自的基线提交、最终提交、修改文件数和有效代码/测试行数。
- 新增依赖、Manifest/环境配置、服务端口和部署步骤。
- Runtime 通用协议、生命周期、页面和测试投入；Agent 侧协议门面、持久任务、幂等、
  Cancel、补参与 Artifact 改造投入。
- Fake A2A 测试、真实联调、故障演练所需用例数与人工步骤。
- 双方团队实际记录的工时；未由团队记录的工时不得由 Runtime 团队猜测。
- 后续第二个同类 Agent 仅需承担的增量配置、Agent 适配与契约测试项。

## 9.8 验收标准

候选阶段被正式启用后，至少满足：

- RA-AC-01：合法远程 Manifest 启动加载并进入 Router；非法或不可达远程能力被隔离，
  不影响本地 Capability。
- RA-AC-02：Fake A2A 的 completed Task 在现有 `/chat` 页面形成唯一非空 AIMessage，
  capability_id、Run 终态与 SSE 语义保持现有契约。
- RA-AC-03：`input-required` 产生文本补参界面，Resume 不新增 Run/HumanMessage，且
  同一远程 Task/context 完成。
- RA-AC-04：显式 Cancel 能取消远程 Task；无法确认时保持 cancel_requested，不得错误
  标记 cancelled 或 failed，也不得放开同 Session 新 Run。
- RA-AC-05：在“远程已受理、本地绑定前”和“绑定后、终态前”分别模拟 Runtime
  崩溃，并连续恢复超过 3 次；不得产生第二个远程业务任务或因次数上限误判 failed。
- RA-AC-06：重复初次提交和重复 Resume 均幂等；同 ID 不同内容稳定冲突。
- RA-AC-07：认证失败、协议错误、远程任务失败、暂时不可用和状态未知映射准确，
  日志和事件不泄露正文、凭据、Checkpoint 或工具原始结果。
- RA-AC-08：Session 删除会先确认远程终态；未知状态时保留 Session 和恢复证据。
- RA-AC-09：真实 `semantic-pattern-mining` 完成“参数齐全直接执行”“缺少日期后补参”
  “执行中取消”“Runtime 重启恢复”四条端到端场景。
- RA-AC-10：交付接入成本报告，并通过 Stage 3 全量回归及远程阶段新增自动化测试。

## 9.9 明确不实现

首期不实现动态 Remote Registry、热加载、远程管理 API、A2A Push Notification、
JSON-RPC/gRPC 绑定、远程 token 流式转发、跨 Run 远程上下文、远程文件/二进制
Artifact、OAuth2/mTLS、跨系统数据删除、Workflow、多 Agent 编排、多实例 Worker、
远程 Agent 管理页面或对目标 Agent 业务算法的修改。
