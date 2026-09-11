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

# 14. 开发流程

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
```

禁止一次提交跨多个未关联 Task 的大范围重构。

---

# 15. 修改文档规则

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

# 16. 完成定义

任务不是“代码已写完”即完成。

必须同时满足：

```text
实现完成
+ 自动化测试通过
+ 验收标准通过
+ tasks.md 已更新
+ 未引入当前 Stage 之外的功能
```
