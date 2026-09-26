# AgentRuntime Architecture Decisions

本文件记录已经确认的关键设计裁决。

除非负责人明确重新裁决，否则后续 Agent / Codex 不得自行推翻。

---

## D-001：项目名称

**Decision**

```text
Project: AgentRuntime
Repository: agent_runtime
Python package: agent_runtime
```

**Status**：Accepted

---

## D-002：采用 FastAPI + LangGraph Parent Graph

**Decision**

FastAPI 负责 API / SSE，LangGraph Parent Graph 负责会话级 Agent 调度。

**Reason**

分离产品 API 协议与 Agent Runtime 执行协议。

**Status**：Accepted

---

## D-003：Stage 1 只使用两个演示 Agent

**Decision**

```text
general_chat
en_to_zh
```

**Reason**

第一阶段只验证 Parent Graph 的调度能力，不让复杂业务掩盖 Runtime 架构问题。

**Status**：Accepted

---

## D-004：general_chat 禁止翻译

**Decision**

`general_chat` 不允许处理任何翻译任务。

翻译请求必须返回：

```text
OUT_OF_SCOPE
```

**Reason**

确保两个演示 Agent 能力边界明确，用于验证 Capability 切换和重新路由。

**Status**：Accepted

---

## D-005：en_to_zh 只处理英译汉

**Decision**

只允许：

```text
英文 → 中文
```

其他任务均返回 `OUT_OF_SCOPE`。

**Status**：Accepted

---

## D-006：正常后续对话不重复 Router

**Decision**

首次对话 Router 确定 Capability 后，后续消息优先继续当前 Capability。

只有当前 Capability 返回 `OUT_OF_SCOPE` 时，才重新进入 Router。

**Reason**

减少重复意图识别，同时保持 Capability 自身能力边界自治。

**Status**：Accepted

---

## D-007：Parent Graph 不保存 Child 业务 State

**Decision**

Parent 保存完整公共对话和调度状态，`messages` 是公共对话的唯一权威来源。

Child 内部业务状态只存在自己的 Checkpointer。

Child 的消息视图由 Parent 公共消息派生，不构成第二套公共历史。

**Reason**

保持 Child Agent 解耦、可替换、可独立演进。

**Status**：Accepted

---

## D-008：Parent / Child 使用不同 Checkpointer

**Decision**

Parent 与每个 Child 使用独立 Checkpointer 状态空间。

**Reason**

避免父图污染子图业务状态，保证状态边界清晰。

**Status**：Accepted

---

## D-009：Child 使用独立 thread_id

**Decision**

```text
Parent thread_id = session_id
Child thread_id = {session_id}:{capability_id}
```

**Reason**

从运行时 ID 层明确隔离不同 Agent 的执行上下文，同时保持可追溯性。

**Status**：Accepted

---

## D-010：Stage 1 使用单 PostgreSQL 数据库

**Decision**

Parent Checkpointer、Child Checkpointer、Session 业务数据先放同一个 PostgreSQL 数据库实例。

**Reason**

第一阶段减少运维复杂度。

**Status**：Accepted

---

## D-011：流式协议使用 HTTP + SSE

**Decision**

第一阶段不使用 WebSocket。

**Reason**

当前交互模型是请求一次、服务端持续输出，SSE 更简单且足够。

**Status**：Accepted

---

## D-012：LangGraph Stream 不直接暴露给前端

**Decision**

FastAPI 将 LangGraph 内部 stream event 转换为稳定产品 SSE 协议。

Stage 1：

```text
message
error
done
```

**Reason**

避免前端与 LangGraph 节点、State、内部事件格式强耦合。

**Status**：Accepted

---

## D-013：Stream 与 ChildResult 分离

**Decision**

```text
Stream → 用户可见实时内容
最终 AIMessage → Parent 公共 messages
ChildResult → Parent 控制判断
```

**Reason**

既支持实时输出和公共消息持久化，又避免 Parent 接收 Child 完整业务 State。

**Status**：Accepted

---

## D-014：OUT_OF_SCOPE 与失败严格区分

**Decision**

`OUT_OF_SCOPE` 表示能力边界不匹配。

执行异常属于 `failed/error`。

同一条 HumanMessage 对每个 Capability 最多尝试一次。拒绝时不产生用户可见
输出，也不推进 Child 消息历史。所有 Capability 都拒绝后返回
`unsupported`，不得继续循环 Router。

**Status**：Accepted

---

## D-015：Session 在 Router 前创建

**Decision**

无 `session_id` 时先创建 Session，再进入 Router。

Router 失败不删除 Session 和第一条 HumanMessage。

**Reason**

`session_id` 是整个执行链的根关联 ID。

**Status**：Accepted

---

## D-016：Session title 第一阶段截取首条用户消息

**Decision**

不调用模型生成标题。

模型标题生成延后。

**Status**：Accepted

---

## D-017：Stage 2 单 Session 单 Run

**Decision**

同一个 Session 同时最多允许一个 active Run。

Stage 2 active Run 使用单实例进程内存管理。

**Status**：Accepted（Stage 2 历史基线；Stage 2.5 由 D-043 取代进程内实现）

---

## D-018：Stage 2 Stop 只停止当前 Run

**Decision**

Stop 不删除 Session、不回滚历史。

已经流式输出的内容保留。

**Status**：Accepted（Stage 2 历史基线；Stage 2.5 由 D-047 取代同步 Stop）

---

## D-019：Regenerate 使用 Checkpoint Fork

**Decision**

重新生成通过 LangGraph checkpoint history / time travel 从历史状态重新执行。

不覆盖旧 checkpoint。

第一版不额外维护：

```text
branch_id
is_active
```

**Status**：Accepted

---

## D-020：message_id 优先直接使用 BaseMessage.id

**Decision**

当前不额外创建独立于 BaseMessage 的产品级消息 ID。

所有 HumanMessage 和 AIMessage 在进入持久化前必须由服务端保证存在稳定
UUID，不能依赖模型 Provider 一定返回消息 ID。

`message_id` 与 `checkpoint_id` 保持概念分离。

**Status**：Accepted

---

## D-021：Stage 3 才引入 Capability Manifest

**Decision**

Stage 1 / 2 固定两个 Capability，不提前做 Manifest。

Stage 3 Manifest 成为 Capability 强制接入契约。

**Status**：Deferred（原正式 Stage 3 已由 D-042 移除，只保留为 Future 候选）

---

## D-022：Stage 3 动态挂载先只做内部方法

**Decision**

Stage 3 实现内部：

```text
mount()
unmount()
```

不实现管理 API。

后续再演进为：

```text
内部方法
→ Admin API
→ 管理平台
```

**Status**：Deferred（原正式 Stage 3 已由 D-042 移除，只保留为 Future 候选）

---

## D-023：优雅卸载使用 DRAINING

**Decision**

```text
ACTIVE
→ DRAINING
→ 不再接收新任务
→ 当前任务结束
→ UNMOUNTED
```

普通卸载不强杀运行中的任务。

**Status**：Deferred（原正式 Stage 3 已由 D-042 移除，只保留为 Future 候选）

---

## D-024：Stage 3 用户权限先做用户级授权

**Decision**

使用用户-Capability 关联关系。

暂不做 RBAC。

权限每次执行前实时读取数据库，不加缓存。

**Status**：Deferred（原正式 Stage 3 已由 D-042 移除，只保留为 Future 候选）

---

## D-025：低置信度确认使用 LangGraph interrupt

**Decision**

Stage 3 使用：

```text
interrupt()
Command(resume=...)
```

不通过新增 HumanMessage 模拟确认。

**Status**：Deferred（低置信度触发策略只保留为 Future 候选）

---

## D-026：interrupt 后关闭原 SSE，resume 建新 SSE

**Decision**

```text
interrupt
→ SSE interrupted
→ 当前连接结束
→ /resume
→ 新 SSE
→ Command(resume)
```

**Reason**

人工等待不应依赖 HTTP 连接长期存活。

**Status**：Superseded by D-048；“中断后关闭连接”的原则继续保留

---

## D-027：文件能力只规划，不进入当前实施阶段

**Decision**

Stage 1、Stage 2 和当前 Stage 2.5 均不实现文件上传、解析、RAG。

只在 Future 中保留架构规划。

**Status**：Accepted

---

## D-028：复杂任务编排不进入 Parent Graph

**Decision**

未来复杂任务能力作为独立 Complex Task Capability。

其内部负责串联、并联其他 Capability / SubAgent。

**Reason**

保证 Parent Graph 长期保持轻量。

**Status**：Accepted

---

## D-029：Stage 1 使用单用户可信环境

**Decision**

Stage 1 不实现认证和用户切换。`user_id` 由服务端配置提供，API 不接受客户
端任意指定用户。

**Reason**

当前阶段只验证 Runtime 主链路，认证和 RBAC 不属于同一个问题。

**Status**：Accepted

---

## D-030：Stage 1 使用 10/5 上下文规则

**Decision**

当前 HumanMessage 之前不足 10 个已完成轮次时，向 Child 传递全部已完成
轮次和当前 HumanMessage；已有 10 个已完成轮次后，只传递最近 5 个完整
Human + AI 轮次和当前 HumanMessage。System Message 始终保留。

上下文裁剪只影响模型输入，不删除 Parent 公共历史。`unsupported` 和
`incomplete` 消息不计入完整轮次，也不进入后续模型上下文。

**Reason**

Stage 1 先使用可预测、可测试的简单规则，复杂 token、摘要和长期记忆策略
延后设计。

**Status**：Accepted

---

## D-031：使用百炼 OpenAI 兼容接口

**Decision**

Stage 1 使用阿里云百炼提供的 OpenAI 兼容接口。API Key、Base URL 和模型名
通过环境变量配置，并与地域及业务空间匹配。

项目固定使用 Python 3.12，并通过 `uv` 管理依赖。

**Status**：Accepted

---

## D-032：自动化测试默认使用 Fake Model

**Decision**

Router、Capability、流式、回流和上下文规则的自动化测试使用 Fake Model。
真实百炼模型调用仅作为可选 smoke test，不作为默认测试或 CI 前置条件。

**Reason**

避免自动化测试受网络、费用和非确定性模型输出影响。

**Status**：Accepted

---

## D-033：不同 Capability 使用不同输出风格

**Decision**

`general_chat` 通过 System Prompt 尽可能简洁直接回答，不做二次模型压缩，
输出 token 上限只作为宽松保护。

`en_to_zh` 以忠实完整翻译为最高优先级，不受简短回答风格约束。

**Status**：Accepted

---

## D-034：Stage 1 SSE 使用固定产品字段

**Decision**

```text
message → session_id + message_id + delta
error   → session_id + code + message + retryable
done    → session_id + status
```

`done.status` 只允许 `completed`、`unsupported`、`failed`。

**Reason**

新建 Session 和流式失败时，客户端仍能获得稳定关联 ID，同时避免依赖
LangGraph 内部事件。

**Status**：Accepted

---

## D-035：Stage 2 继续使用固定单用户边界

**Decision**

Stage 2 继续运行在单用户可信环境。服务端从配置注入固定 `user_id`，API 不
接受客户端指定用户。身份认证和多用户数据隔离延后。

**Reason**

Stage 2 聚焦完整聊天产品闭环，不同时扩大为账号与权限平台。

**Status**：Accepted

---

## D-036：Stage 2 使用游标分页和活动 Parent 分支历史

**Decision**

Session 列表按 `updated_at DESC, session_id DESC` 使用不透明游标分页。
消息历史使用 `before=message_id`，返回当前活动 Parent 分支的消息并保持页内
时间正序。

`completed`、`unsupported`、`incomplete`、`stopped` AIMessage 均可在
公共历史中展示；只有成功完成的 HumanMessage + AIMessage 轮次进入模型上下文。

Regenerate 的旧分支仍存在于 checkpoint history，但普通历史接口不提供分支
浏览、切换或产品级 `branch_id`。

**Reason**

保持 Parent `messages` 的权威性，同时提供稳定、可扩展且不泄漏 LangGraph
内部结构的产品查询协议。

**Status**：Accepted

---

## D-037：Stage 2 Stop 是可等待的幂等产品终态

**Decision**

Graph Run 使用进程内 Registry 和独立 producer task 管理。同一 Session 在建立
SSE 前完成占用，重复请求返回 HTTP 409 `SESSION_BUSY`。

Stop 取消当前 Run，将部分或空输出保存为 `runtime_status=stopped`，结束 SSE
生产端并释放 active Run 后才返回。没有 active Run 时幂等成功。

Stage 2 的 `done.status` 增加 `stopped`。Stop 不发送 `error`，客户端连接
意外断开则终止执行并按 `incomplete` 处理。

**Reason**

调用方在 Stop 返回后可以立即开始新请求，同时每个已经接受的 HumanMessage 都
有可解释的公共结果。

**Status**：Accepted（Stage 2 历史基线；Stage 2.5 由 D-047 取代）

---

## D-038：Regenerate 只处理最新完成回答

**Decision**

只允许重新生成当前活动分支中最新的 `completed` AIMessage。通过 LangGraph
checkpoint history 定位回答前状态并创建 fork，新分支沿 Parent 流程继续执行。

原 HumanMessage 和 message_id 不复制；新 AIMessage 使用新 UUID。新分支启动后
成为活动分支，失败或停止不自动回滚；旧 checkpoint 不删除但不通过普通历史
返回。

**Reason**

该约束能够提供用户可理解的“重新回答”，又避免 Stage 2 引入完整分支产品模型。

**Status**：Accepted

---

## D-039：Feedback 只作用于活动分支完成消息

**Decision**

只有当前活动分支中的 `completed` AIMessage 可以接收 `like`、`dislike`
或 `cancel`。以 `user_id + message_id` 唯一约束保存最终反馈；cancel 删除
当前反馈。记录额外保存 `session_id` 作为 Session 硬删除的清理索引，但不改变
唯一产品语义。

**Reason**

避免对 unsupported、incomplete、stopped 或不可见旧分支消息产生含义不清的
反馈记录。

**Status**：Accepted

---

## D-040：Stage 2 必须交付独立资源的聊天演示页面

**Decision**

Stage 2 接口完成后使用 React + Vite + Ant Design / Ant Design X 交付 `/chat`
演示页面。使用开源第三方通用 UI 与 Markdown 组件，项目只编写业务组合组件。

HTML、JavaScript/JSX、API 客户端和 CSS 分文件维护；Vite 输出独立且带内容哈希
的 JS / CSS 资源到 Python 包内静态目录，FastAPI 同源提供页面和资源。禁止把
全部实现放入单个 HTML，也不使用 CDN。

页面使用左侧会话列表、右侧聊天区和可折叠产品运行状态面板。面板可展示
`session_id`、`message_id`、`capability_id` 与终态，但不得展示提示词、
节点或 checkpoint。

**Reason**

完整页面让 Runtime 的路由、流式、停止、重新生成和反馈能力可直接观察与验收，
同时第三方组件降低无必要的 UI 自研成本。

**Status**：Accepted

---

## D-041：Stage 2 使用分层自动化验收

**Decision**

后端使用 pytest + Fake Model；前端使用 Vitest + React Testing Library；另提供
显式启用的 Playwright + PostgreSQL + Fake Model 端到端验收。真实百炼继续只
用于独立 smoke test。

**Reason**

默认验证保持确定、快速、无费用，端到端测试同时证明构建后的真实页面能够承接
完整产品接口。

**Status**：Accepted

---

## D-042：Stage 2.5 成为当前正式阶段并移除原 Stage 3 承诺

**Decision**

Stage 2 验收后先实施 Stage 2.5，将持久 Run、RuntimeEvent、Redis 实时事件、SSE
重连、Cancel、Interrupt/Resume 和崩溃恢复建设为后续扩展的 Runtime 基础。

原 Stage 3 不再是已承诺的正式阶段。Manifest、Registry、动态挂载、权限、
DRAINING 和 Router confidence 只保留为 Future 候选，必须在 Stage 2.5 完成后
重新设计阶段范围。

**Reason**

先建立可靠执行生命周期，避免后续 Capability 平台继续依赖进程内 Run 和 HTTP
连接生命周期。

**Status**：Accepted

---

## D-043：Run 是持久执行生命周期权威源

**Decision**

普通消息和 Regenerate 必须先在 PostgreSQL 创建 Run，再异步执行。Run 使用客户端
提供的全局唯一 `request_id` 实现幂等，服务端分配 `run_id`、输入消息 ID 和回复
消息 ID。同一 Session 通过数据库部分唯一约束最多存在一个活动 Run。

Run 类型仅保留 `normal` 与 `regenerate`；崩溃恢复沿用原 Run，不创建 `retry`。
Stage 2 历史不补建 Run。活动期间保存最小 `input_payload` 和请求指纹；终态时清除
恢复正文，只保留指纹和非敏感摘要。

Run 状态为：

```text
queued | running | recovering | interrupted | cancel_requested
completed | failed | cancelled
```

终态不可修改，`unsupported` 对应 Run `completed`。

**Reason**

执行生命周期必须独立于 SSE 连接和进程内 Registry，并能够在请求提交后崩溃的
窗口中恢复。

**Status**：Accepted

---

## D-044：RuntimeEvent 使用持久序号并隔离公开与内部事件

**Decision**

每个 Run 使用 PostgreSQL 高水位分段预留 `seq`，所有事件经 Run Sequencer 串行
编号。序号允许缺号，但不得重复或倒退。`event_id` 使用 UUIDv4；不引入独立
`trace_id`。

RuntimeEvent 明确区分 `public` 与 `internal`。公开 payload 必须使用事件级固定
Schema 和白名单过滤，不能暴露节点、Prompt、Checkpoint、State、工具原始参数或
结果。完整 AIMessage 仍只保存在 Parent 公共历史。

公开事件固定为：

```text
run.started
message.started
message.delta
message.finalized
interrupt.required
interrupt.resumed
run.completed
run.cancelled
run.failed
```

`message.delta` 为 transient，其余为 durable。`message.started` 携带执行
`attempt`，恢复时用于清除旧草稿。每个 Run 只能产生一个终态事件。

**Status**：Accepted

---

## D-045：Run API 与 SSE 解耦，Redis 只承载短期公开实时事件

**Decision**

创建普通消息或 Regenerate 成功后返回 HTTP 202 和 Run 摘要；客户端再通过
`GET /api/v1/chat/runs/{run_id}/events` 获取 SSE。页面刷新后可查询 Session 当前
活动 Run。

SSE `id` 等于 `seq`，续传使用 `Last-Event-ID` 或 `after_seq`。Gateway 始终合并
PostgreSQL durable event 与 Redis Stream，按序去重；使用有限批次和写入超时，
慢连接关闭但不取消 Run。

Redis Stream ID 使用 `{seq}-0`。Redis 不持久化、不作为硬依赖，每次写入刷新
30 分钟 TTL。Redis 故障时 transient delta 可以丢失，持久终态由 PostgreSQL
补发，最终全文从 Parent 历史读取。S2.5 不实现 Transactional Outbox。

**Status**：Accepted

---

## D-046：Checkpoint 先落地，Run 投影随后提交并支持对账恢复

**Decision**

Parent 继续使用 `thread_id=session_id`。每个 Run Checkpoint metadata 关联
`run_id`，Run 保存确定的 `start_checkpoint_id`，但不保存
`last_checkpoint_id`。

最终 AIMessage 或 Interrupt Checkpoint 必须先持久化；随后在一个 PostgreSQL
事务中更新 Run / Interrupt 投影和 durable RuntimeEvent；最后尽力发布 Redis。
恢复器若发现 Checkpoint 已经包含稳定回复或中断 ID，只补齐投影，不重复调用
已经完成的节点；没有 Run Checkpoint 时才从
`start_checkpoint_id + input_payload` 重放。

S2.5 使用单 Runtime 进程和轻量 Coordinator。初次执行不计恢复次数，重启接管
最多三次。恢复语义为至少一次；有外部副作用的 Tool / SubAgent 未提供幂等键或
操作账本时不得自动恢复。

**Status**：Accepted

---

## D-047：Cancel 异步化且连接断开不再停止 Run

**Decision**

SSE、浏览器或网络断开不取消 Run。Cancel 持久化为 `cancel_requested` 后返回
HTTP 202，执行器先协作式取消，超时后强制取消本地任务。重复 Cancel 幂等，完成
与取消竞争时首个数据库终态获胜；Session 删除仍等待 Run 真正终止。

原 Session Stop 不再作为公开产品接口。任何终态都必须形成非空、可解释的公共
回复：失败保存部分输出或固定 `incomplete` 提示，取消保存部分输出或固定
`stopped` 提示，unsupported 使用 Parent 固定提示。只有 completed 完整轮次进入
后续模型上下文。

**Status**：Accepted

---

## D-048：Interrupt / Resume 使用同一持久 Run

**Decision**

S2.5 实现最小 `run_interrupts`。`(run_id, interrupt_id)` 唯一，每个 Run 同时
最多一个 pending Interrupt，但允许顺序多次中断。Resume 携带 interrupt ID 和
恢复请求 ID，通过事务、行锁和请求指纹保证只成功一次，不创建 HumanMessage。

`interrupt.required` 发出后关闭当前 SSE，Run 保持 `interrupted` 并占用 Session。
Resume 沿用原 `run_id`，客户端从最后 `seq` 建立新 SSE。多审批、审批收件箱、
超时和并行中断延期。

**Status**：Accepted

---

## D-049：S2.5 只建立最小 Agent 执行契约

**Decision**

现有 Child Agent 通过 `RunContext + AgentContext + TaskInput` 统一调用，并只通过
注入的类型化事件出口发送事件。Agent 不直接访问 SSE、Redis、Session 生命周期或
顶层 Run Repository，也不能自行修改 Run 终态。

最终控制结果继续使用极薄 `ChildResult`，完整公共消息走 Parent 数据面。本阶段
只用现有 Capability 和 Fake Agent 验证契约，不实现 Manifest、动态 Registry、
权限、多 Agent 调度或远程 Agent。

**Status**：Accepted

---

## D-050：Session 硬删除覆盖全部 Runtime 数据

**Decision**

删除 Session 时先阻止新 Run，取消并等待活动 Run，随后删除该 Session 的
Interrupt、RuntimeEvent、Run、Feedback、Child / Parent Checkpoint，最后删除
Session。Redis key 尽力删除，失败时由 TTL 兜底，不阻塞 PostgreSQL 删除。

S2.5 不设置独立 RuntimeEvent 全局保留期，持久事件只随 Session 删除；独立审计
与保留策略留到后续阶段。

**Status**：Accepted

---

## D-051：S2.5 必须升级聊天页面并提供真实链路演示模式

**Decision**

已有 `/chat` 页面必须同步迁移到异步 Run API，展示 Run 状态、重连、Cancel、
Interrupt/Resume 和终态历史回读。继续使用 React、Vite、Ant Design / Ant
Design X 和第三方开源组件，保持 HTML、JS/JSX、API Client 与 CSS 分离。

开发环境可以通过显式配置启用确定性 Fake Agent 演示场景，但必须经过真实 Run、
Event、Redis、SSE、Interrupt 和 Resume 链路。演示模式默认关闭，不进入正式
Capability 路由，前端不得伪造执行结果。

**Status**：Accepted
