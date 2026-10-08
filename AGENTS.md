# AGENTS.md

本文件是 AgentRuntime 项目的 Agent / Codex 工程操作说明书。

任何自动化编码 Agent 在修改代码前都必须完整阅读本文件，以及：

- `docs/requirements.md`
- `docs/architecture.md`
- `docs/decisions.md`
- `docs/tasks.md`

如果当前实现与文档冲突，**先停止扩展实现，优先以文档中的已确认决策为准**。

---

# 1. 项目目标

AgentRuntime 是一个逐阶段建设的对话型 Agent Runtime。

当前目标不是构建完整 Agent Platform，而是严格按 Stage 推进。

最高优先级：

```text
简单
可运行
可验证
边界清晰
可继续演进
```

禁止为了“未来可能需要”提前引入复杂抽象。

Stage 1 固定运行基线：

```text
单用户可信环境
Python 3.12
uv 管理依赖
阿里云百炼 OpenAI 兼容接口
自动化测试默认使用 Fake Model
```

---

# 2. 文档优先级

发生冲突时，按以下优先级处理：

```text
1. docs/decisions.md
2. docs/requirements.md
3. docs/architecture.md
4. docs/tasks.md
5. README.md
6. 当前代码实现
```

如果文档之间发生明显冲突，不允许自行猜测，应将冲突记录到 `docs/tasks.md` 的 Blockers 区域并停止相关扩展。

---

# 3. Stage 约束

当前只能实现 `docs/tasks.md` 中标记为当前 Stage 的任务。

禁止：

- 当前 Stage 未完成就实现下一 Stage
- 顺手补齐“未来一定会用”的模块
- 为未来平台化提前创建大量 Service / Repository / Adapter 层
- 因个人偏好替换已确定技术方案

每个 Stage 完成后必须先通过验收，再进入下一 Stage。

---

# 4. Parent Graph 约束

Parent Graph 只负责：

- 会话级调度
- Router
- 当前 Capability 状态
- 完整公共 `messages`
- 本轮 Capability 拒绝集合
- Child Agent 调用
- `OUT_OF_SCOPE` 回流
- 控制结果处理

禁止 Parent Graph 保存：

- Child Agent 业务中间状态
- Child Agent 私有字段
- Child Agent 临时 JSON
- Child Agent 内部节点执行数据
- Child Agent 工具调用结果

Parent Graph 的控制面只能接收标准控制结果 `ChildResult`。

数据面必须将最终公共 `AIMessage` 写回 Parent `messages`。这不代表 Parent
可以读取或保存 Child 的业务私有 State。

---

# 5. Child Agent 约束

每个 Child Agent：

- 独立 State
- 独立 Checkpointer
- 独立 thread_id
- 自己维护能力边界
- 不直接读取其他 Child Agent 的 Checkpointer

Stage 1：

```text
general_chat
→ 普通聊天
→ 禁止翻译

en_to_zh
→ 仅英译汉
```

如果任务超出能力边界，必须返回：

```text
OUT_OF_SCOPE
```

禁止把 `OUT_OF_SCOPE` 当成运行失败。

范围判断必须发生在用户可见生成和 Child 业务状态持久化之前。

Child 拒绝任务时：

- 不输出任何用户可见 token；
- 不推进 Child 消息历史；
- 只返回 `ChildResult(status="rejected", control_signal="OUT_OF_SCOPE")`。

`general_chat` 默认简洁、直接回答。该风格约束不适用于 `en_to_zh`；
`en_to_zh` 必须忠实完整翻译当前 HumanMessage，不得为了简短而删减内容。

---

# 6. 公共消息与上下文规则

Parent `messages` 是完整公共对话的唯一权威来源。

每次调用 Child 前，由 Context Builder 从 Parent 生成 Child 本轮消息视图：

```text
当前 HumanMessage 之前的已完成轮次 < 10
→ 传递全部已完成轮次
→ 再加当前 HumanMessage

当前 HumanMessage 之前的已完成轮次 >= 10
→ 最近 5 个完整 Human + AI 轮次
→ 再加当前 HumanMessage
```

System Message 始终保留且不计入轮数。

只有成功完成的 `HumanMessage + AIMessage` 才算一个完整轮次。
`unsupported` 和 `incomplete` 消息保留在 Parent 公共历史中，但不计入完整
轮次，也不进入后续模型上下文。

上下文裁剪只影响模型输入，不允许删除 Parent 保存的完整公共历史。

Child 中的消息只是从 Parent 派生的执行快照，不能成为公共历史权威源。

---

# 7. Thread ID 规则

Stage 1 固定：

```text
Parent:
thread_id = session_id

Child:
thread_id = {session_id}:{capability_id}
```

不允许自行改为所有图共用同一个 thread_id。

不允许提前引入 `task_id` 等多任务 thread 结构。

---

# 8. Streaming 约束

LangGraph 内部 stream event 不允许直接作为前端协议。

内部：

```text
messages
custom
updates
checkpoints
...
```

外部 SSE 由 FastAPI 统一适配。

Stage 1 对外最小 SSE：

```text
message
error
done
```

事件数据至少满足：

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

前端不得依赖：

- LangGraph node 名称
- 原始 StateSnapshot
- 原始 updates
- tasks
- 内部 checkpoint metadata

---

# 9. ChildResult 约束

Stage 1 使用极简结构：

```text
status
control_signal
```

例如：

```json
{
  "status": "completed",
  "control_signal": null
}
```

或：

```json
{
  "status": "rejected",
  "control_signal": "OUT_OF_SCOPE"
}
```

不要把用户可见完整文本复制进 ChildResult。

用户内容走 Stream。

最终完整公共 `AIMessage` 同时必须通过数据面写回 Parent `messages`。

---

# 10. Persistence 约束

使用 PostgreSQL。

本地外部依赖统一通过 Windows Docker Desktop 和项目根目录 `compose.yaml`
部署，不使用本机安装或便携版服务。PostgreSQL 使用名为
`agent-runtime-postgres` 的 Docker 命名卷持久化；除非负责人明确要求重置，
不得删除该卷。

如果 Docker Desktop 未启动或启动异常，只报告具体错误并通知负责人启动或恢复；
Agent 不得自行修复、重置 Docker Desktop，也不得移动或删除 Docker 内部 socket、
配置、虚拟磁盘或其他运行时文件。

Stage 1：

- Parent 使用持久化 Checkpointer
- Child 使用独立持久化 Checkpointer
- 同一个 PostgreSQL 数据库即可

不要提前做：

- 物理拆库
- Redis
- 多实例协调
- Run History

---

# 11. Session 约束

Stage 1 只实现最小 Session：

```text
session_id
user_id
title
created_at
updated_at
```

没有 `session_id` 时：

```text
先创建 Session
→ 保存第一条 HumanMessage
→ 再执行 Router
```

Router 失败不删除 Session，也不删除第一条 HumanMessage。

Session title：截取第一条 HumanMessage。

Stage 1 使用配置中的固定 `user_id`，API 不接受客户端任意指定用户。

每个 HumanMessage 和 AIMessage 必须由服务端分配稳定 UUID。

---

# 12. OUT_OF_SCOPE 有限回流

同一条 HumanMessage 对每个 Capability 最多尝试一次。

Router 只能从本轮尚未拒绝该消息的 Capability 中选择。

所有 Capability 均返回 `OUT_OF_SCOPE` 时：

```text
Parent 生成固定简短 unsupported 回复
→ 写入公共 messages
→ 由 Parent 发送 message 事件
→ SSE done(status="unsupported")
```

该结果不是运行失败，不发送 `error`。

---

# 13. Stage 1 禁止实现

严禁提前实现：

- Capability Manifest YAML
- Capability 动态 mount/unmount
- Capability 权限
- RBAC
- interrupt / resume
- 低置信度确认
- regenerate
- stop
- feedback
- pin
- 文件上传
- 文件解析
- RAG
- Redis
- 多实例
- Run History
- 管理 API
- 管理平台
- Remote Agent
- Deep Agents
- 复杂任务 Agent

---

# 14. Stage 2 约束

本节记录已验收的 Stage 2 历史基线。进入 Stage 2.5 后，与持久 Run、异步
Cancel、独立 SSE 和断线重连冲突的规则，以第 15 节和正式架构决策为准。

Stage 2 继续使用配置中的固定 `user_id`，不实现认证或多用户隔离。

产品接口必须遵守：

- Session 列表使用 `updated_at DESC, session_id DESC` 不透明游标分页
- 历史接口只返回当前活动 Parent 分支，不暴露原始 checkpoint
- 同一 Session 同时最多一个进程内 active Run
- Stop 等待停止持久化、SSE 生产端结束和 Run 清理后返回
- Regenerate 只允许活动分支最新的 completed AIMessage
- Feedback 只允许活动分支中的 completed AIMessage
- Session 删除时先停止 Run，最后删除 Session 行
- completed / unsupported / incomplete / stopped AIMessage 对用户可见，只有
  completed 完整轮次进入模型上下文
- 浏览器或 SSE 意外断开时停止 Run，并保存 incomplete AIMessage

Stage 2 SSE 继续只使用 `message`、`error`、`done`，但产品字段扩展为：

```text
message → session_id + message_id + capability_id + delta
error   → session_id + code + message + retryable
done    → session_id + message_id + capability_id + status
```

`done.status` 允许 `completed`、`unsupported`、`stopped`、`failed`。

Stage 2 必须交付 `/chat` 完整演示页面。前端使用 React + Vite + Ant Design /
Ant Design X，并满足：

- 使用第三方开源通用组件，不从零开发消息气泡、输入框、弹窗等组件
- HTML、JavaScript/JSX、API 客户端和 CSS 分文件维护
- 禁止将全部页面逻辑和样式内联到一个 HTML
- 构建产物保持独立 HTML、带内容哈希的 JS 和 CSS
- 页面不得依赖 LangGraph 节点、StateSnapshot、tasks 或 checkpoint metadata

---

# 15. Stage 2.5 约束

Stage 2.5 继续使用单用户可信环境，只支持单个 Runtime 进程。禁止把持久化 Run
误解为已经支持多实例 Worker、Lease 或分布式任务队列。

必须遵守：

- PostgreSQL Run 是执行生命周期权威源，同一 Session 最多一个活动 Run
- 普通消息和 Regenerate 先返回 HTTP 202 Run 摘要，再通过独立 GET SSE 获取事件
- 客户端 `request_id` 必填且全局唯一；冲突内容复用同一 ID 返回 409
- Run 类型只允许 `normal`、`regenerate`；恢复不得创建新的 retry Run
- Parent `messages` 继续是完整公共消息唯一权威源
- `input_payload` 只用于活动 Run 恢复，终态时清除正文，不得公开或写入业务日志
- 所有事件通过 Run Sequencer 分配持久 `seq`；允许缺号，不允许重复或倒退
- 公开事件必须使用固定 Schema 与白名单，不得暴露 Prompt、Checkpoint、State、
  节点或工具原始参数和结果
- Redis 只承载短期公开实时事件，不是事实权威，也不是应用启动硬依赖
- SSE 或浏览器断开不取消 Run；只有显式 Cancel、Session 删除或执行失败停止执行
- Cancel 异步返回 202；Session 删除必须等待活动 Run 终止，并最后删除 Session
- 最终 AIMessage 或 Interrupt Checkpoint 必须先于 Run 终态 / 中断投影持久化
- 恢复只承诺至少一次；无幂等保障的外部副作用不得进入自动恢复
- 每个 Run 同时最多一个 pending Interrupt；Resume 沿用同一 Run 且不得新增
  HumanMessage
- completed / unsupported / incomplete / stopped 公共消息必须非空，只有 completed
  完整轮次进入后续模型上下文
- Agent 不能直接访问 SSE、Redis、Session 生命周期或顶层 Run 终态

Stage 2.5 必须同步升级 `/chat` 页面，并提供默认关闭、显式启用的开发环境真实
Runtime 演示模式。继续使用第三方开源组件并保持 HTML、JS/JSX、API Client 和
CSS 分文件构建。

Stage 2.5 禁止提前实现：

- Capability Manifest / 动态 Registry
- mount / unmount / DRAINING
- Capability 权限 / RBAC
- 多 Agent 调度或 Remote Agent
- 多实例 Runtime / Worker Lease
- Transactional Outbox
- 完整 Run History 产品界面
- 多审批人或并行中断
- 文件、RAG 和管理平台

---

# 16. Stage 3 约束

Stage 3 是静态 Capability Runtime 阶段，继续使用单用户可信环境和单 Runtime
进程。S3-01 只允许维护架构与任务文档，负责人验收并完成双远程同步前不得进入
S3-02 功能编码。

必须遵守：

- Capability 来自严格本地 YAML Manifest 和启动期进程内 Registry Snapshot
- `entrypoint` 指向接收 `CapabilityBootstrapContext` 的统一工厂
- Router 只读取 `capability_id`、`name`、`description`、`enabled`
- 公开 `capability_id` 使用符合 Manifest ID 规则的字符串，不再固定两个枚举
- Capability 统一返回 `AgentResult(status, content, metadata)`；Runtime 将最终全文
  写回 Parent，Capability 不能直接写 Parent、SSE、Redis 或顶层 Run 终态
- 每个 Invocation 使用稳定 UUID；生命周期只写 internal durable RuntimeEvent
- 每个 Invocation 关联 Capability Task；Run 失败或取消不自动结束 Task
- `state_scope` 必须显式为 invocation、run 或 session，Child thread_id 由 Runtime
  按 namespace/version 确定性生成
- State Schema 不兼容时拒绝 continue，不自动迁移或修改旧 Task
- Regenerate 只允许 `state_scope=invocation + side_effect_policy=none`；其他组合必须
  在创建 Run 前返回 `CAPABILITY_REGENERATE_UNSUPPORTED`
- 权限无记录即 deny，并在 Router 前和 invoke 前双重实时检查
- `general_chat` 和 `en_to_zh` 只是标准测试 Capability，不自动授权；测试显式配置
  allow/deny
- Health、单进程并发、等待超时和执行超时全部由 Runtime 治理
- automatic + idempotent 的副作用必须使用 `ctx.operation()` 和 Operation Ledger
- 业务副作用边界、稳定 operation_key 和确定失败判断由 Capability 自治；未知结果
  保持 pending
- manual policy 只在崩溃恢复拒绝时安全失败，不得影响 automatic 恢复路径
- `task_action=new` 后合法 OUT_OF_SCOPE 必须回滚未接受 Task 并恢复原 current Task
- Session 删除必须清理 Task Context、Operation、Task 和全部新旧 Child Checkpoint

Stage 3 禁止实现：

- 热加载、动态 mount/unmount、DRAINING
- Remote Capability、Remote Agent、多实例 Registry 同步
- 多版本 Capability 实现共存
- Workflow、多 Agent 编排、workflow_id
- RBAC、ABAC、组织或租户权限
- State 自动迁移、Saga 或补偿事务
- 分布式 Capability 并发、Worker Lease、分布式任务队列
- Capability Admin API、管理平台、文件或 RAG 产品能力

---

# 17. 开发流程

每次任务执行：

```text
1. 阅读 docs/tasks.md
2. 确认当前 Task
3. 阅读相关 requirements / architecture / decisions
4. 仅实现该 Task
5. 补测试
6. 执行验收
7. 更新 tasks.md 状态
8. 记录验证结果
9. 负责人验收通过后，提交当前任务代码
10. 将提交推送到 GitHub 和 Gitee 两个远程仓库
```

禁止一次提交跨多个未关联 Task 的大范围重构。

进入下一阶段前，当前阶段必须已经完成提交，且同一提交已成功推送到 GitHub 和
Gitee。任一远程推送失败时，不得开始下一阶段，应先修复同步问题并确认两个远程
指向同一提交。

---

# 18. 修改文档规则

Codex 可以：

- 更新 `docs/tasks.md` 状态
- 增加验收记录
- 增加发现的问题

Codex 不可以未经负责人明确裁决直接修改：

- 已确认的架构决策
- Stage 边界
- 核心技术选型
- Parent / Child 状态隔离原则

如果需要调整，先在 `docs/tasks.md` 中记录 Proposed Change，不直接实施。

---

# 19. 完成定义

任务不是“代码已写完”即完成。

必须同时满足：

```text
实现完成
+ 自动化测试通过
+ 验收标准通过
+ tasks.md 已更新
+ 未引入当前 Stage 之外的功能
+ 负责人验收通过后已提交代码
+ GitHub 与 Gitee 已同步到同一提交
```

---

# 20. 代码文本与 Schema 描述

- 新增或修改的代码注释、docstring、模型提示词和用户可见提示信息使用中文
- 代码标识符继续使用清晰、规范的英文命名
- Pydantic Schema 的每个参数必须使用中文 `description` 明确说明用途、允许值
  和阶段性限制，禁止使用空泛或缺失的字段描述

---

# 21. 业务日志规范

业务日志是贯穿所有 Stage 的工程规范，不属于独立产品能力。新增或修改业务链路时，
必须在关键入口、关键出口、拒绝、取消和失败位置记录中文业务事件。

统一使用 `agent_runtime.core.logging.log_business_event`，日志按本地日期写入：

```text
{LOG_DIR}/agent-runtime-YYYY-MM-DD.log
```

默认 `LOG_DIR=logs`。业务日志至少按场景记录可用的 `session_id`、`message_id`、
`capability_id`、状态、错误码、错误类型和耗时，便于串联 HTTP、Session、Run、
Parent、Router 与 Child Capability 的业务流转。

禁止记录：

- API Key、认证头、密码、数据库连接串等凭据
- 用户消息正文、模型输出正文、System Prompt 或完整请求体
- 每个流式 token / delta
- Parent / Child 私有 State、Checkpoint 内容或工具原始结果

日志只作为进程外可观测性旁路，不得写入 Parent / Child State，也不得改变 SSE、
API 或 Checkpointer 契约。新增日志字段时必须继续通过统一脱敏入口输出。
