# AgentRuntime

AgentRuntime 是一个基于 **FastAPI + LangGraph 1.0+ + PostgreSQL** 构建的可扩展对话型 Agent Runtime。

项目目标不是一次性构建完整 Agent Platform，而是按阶段逐步实现：

1. 先验证主图能够稳定调度多个子 Agent；
2. 再补齐完整聊天产品能力；
3. 再扩展为 Capability Runtime；
4. 后续再演进到管理平台、复杂任务编排和工业级治理能力。

---

## 1. 当前定位

当前项目已完成 **Stage 1：最小 Agent Runtime**，正在进入
**Stage 2：完整聊天产品能力与演示页面**。

项目继续面向单用户可信环境，使用 Python 3.12 和 `uv`。模型通过阿里云百炼
的 OpenAI 兼容接口接入；自动化测试默认使用 Fake Model，真实模型只用于
可选 smoke test。

Stage 1 已证明以下 Runtime 主链路成立，Stage 2 在其外增加完整产品 API 和
React 演示页面：

```text
React / Ant Design X Chat Demo
        ↓
FastAPI
        ↓
Parent LangGraph
        ↓
Router
        ↓
Capability / Child Agent
   ├── general_chat
   └── en_to_zh
        ↓
LangGraph Stream
        ↓
FastAPI SSE
```

Stage 1 不提前建设 Capability 平台、权限系统、动态挂载、HITL、文件能力、复杂 Agent 编排等后续功能。

---

## 2. Stage 1 演示 Agent

### general_chat

职责：

- 普通聊天
- 普通知识问答
- 简单咨询
- 默认简洁、直接回答

边界：

- **禁止处理任何翻译任务**
- 遇到翻译请求必须返回 `OUT_OF_SCOPE`

建议实现：LangChain `create_agent`。

### en_to_zh

职责：

- 仅处理英文到中文翻译
- 忠实完整翻译当前用户消息，不因“简短回答”要求删减原文

边界：

- 普通聊天：`OUT_OF_SCOPE`
- 中文翻英文：`OUT_OF_SCOPE`
- 代码生成：`OUT_OF_SCOPE`
- 其他非英译汉任务：`OUT_OF_SCOPE`

建议实现：LangGraph `StateGraph`。

---

## 3. 核心架构原则

### 主图轻量化

Parent Graph 只负责：

- 会话级调度
- 当前 Capability 状态
- Router
- 子 Agent 调用
- `OUT_OF_SCOPE` 回流
- 控制结果处理

Parent Graph 不保存：

- 子图业务中间状态
- 子图临时变量
- 子图内部节点数据
- 子图私有任务状态

### 父子 Checkpointer 隔离

```text
Parent Checkpointer
→ 完整公共 messages（公共对话唯一权威源）
→ resolved_capability_id
→ 本轮 Capability 拒绝集合
→ 主图执行状态

Child Checkpointer
→ 子 Agent 私有状态和派生消息快照
```

子 Agent 使用独立 `thread_id`：

```text
parent thread_id = session_id
child thread_id  = {session_id}:{capability_id}
```

调用 Child 前由 Context Builder 从 Parent 构造完整 Message 上下文：当前
HumanMessage 之前不足 10 个已完成轮次时传递全部已完成轮次；已有 10 个
已完成轮次后，只传最近 5 个完整轮次和当前 HumanMessage。裁剪只影响
模型输入，不删除 Parent 的完整历史。

### Stream 与控制结果分离

```text
Stream
→ 给用户看

ChildResult
→ 给 Parent Graph 做控制判断
```

子 Agent 的实时文本通过 LangGraph stream 进入 FastAPI，再转换成 SSE。

子 Agent 执行结束后只向父图返回极薄的控制结果，例如：

```json
{
  "status": "rejected",
  "control_signal": "OUT_OF_SCOPE"
}
```

控制面保持极薄；数据面会把最终完整 `AIMessage` 写回 Parent `messages`，
同时将 token delta 转换为 SSE。

### 有限 OUT_OF_SCOPE 回流

同一条 HumanMessage 对每个 Capability 最多尝试一次。拒绝时 Child 不产生
用户可见输出，也不推进自己的消息历史。两个 Capability 均拒绝后，Parent
返回固定的 unsupported 回复并通过 `message` 事件发送，然后正常结束，
不把它当成运行错误。

---

## 4. 阶段规划

### Stage 1：最小 Agent Runtime

实现：

- FastAPI
- PostgreSQL
- Session 最小实体
- Parent LangGraph
- Router Top1
- `general_chat`
- `en_to_zh`
- `OUT_OF_SCOPE`
- Parent / Child Checkpointer 隔离
- 独立 Child thread
- Parent 完整公共消息权威源
- 10/5 上下文裁剪
- 有限 `OUT_OF_SCOPE` 回流
- HTTP + SSE
- 同一 Session 状态恢复
- Fake Model 自动化验收

### Stage 2：完整聊天产品能力

实现：

- 游标分页的 Session 列表
- Session 改名和幂等硬删除
- 当前活动分支历史消息查询
- 单 Session 单 Run
- 可等待且幂等的 Stop
- 最新完成回答的 Regenerate / Checkpoint Fork
- 活动分支完成消息的 Feedback
- React + Vite + Ant Design X 完整聊天演示页面
- 独立 HTML、JavaScript/JSX 和 CSS 构建资源
- pytest、Vitest 和可选 Playwright 分层验收

### Stage 3：Capability Runtime 平台化

实现：

- Capability Manifest
- Capability Registry 正式化
- 内部动态 `mount()/unmount()`
- 优雅卸载
- 用户 Capability 权限
- Router confidence
- 低置信度确认
- `interrupt + Command(resume=...)`
- `/resume`

### Future：只规划，不实现

- 文件上传 / 文件服务 / RAG
- Capability Admin API
- 管理平台
- RBAC
- Remote Capability
- Redis 多实例协调
- Run History
- Observability / Evaluation
- Complex Agent
- Deep Agents
- Agent 串联 / 并联
- Context Engineering

---

## 5. 文档导航

- [AGENTS.md](./AGENTS.md)：Codex / Agent 工程操作规范
- [docs/requirements.md](./docs/requirements.md)：需求与阶段边界
- [docs/architecture.md](./docs/architecture.md)：技术架构与实现约束
- [docs/decisions.md](./docs/decisions.md)：已裁决的架构决策
- [docs/tasks.md](./docs/tasks.md)：任务执行与验收状态
- [Stage 2 设计](./docs/plans/2026-09-13-stage-2-chat-product-design.md)：
  已确认的产品接口、运行语义与聊天页面设计
- [Stage 2 后端计划](./docs/superpowers/plans/2026-09-13-stage-2-backend.md)：
  S2-01～S2-08 的测试驱动实施步骤
- [Stage 2 前端计划](./docs/superpowers/plans/2026-09-13-stage-2-frontend.md)：
  S2-09 页面和 S2-10 集成验收步骤

---

## 6. 当前开发规则

任何开发开始前必须先阅读：

1. `AGENTS.md`
2. `docs/requirements.md`
3. `docs/architecture.md`
4. `docs/decisions.md`
5. `docs/tasks.md`

当前只允许执行 `docs/tasks.md` 中标记的 **Stage 2 当前任务**。

未通过 Stage 2 验收前，不允许提前实现 Stage 3 / Future 能力。

真实百炼联调时在项目根目录 `.env` 配置 `DASHSCOPE_API_KEY`、
`LLM_BASE_URL` 和 `LLM_MODEL`；不得提交真实密钥。

Stage 2 页面生产构建由 FastAPI 同源提供。运行已提交的演示资源只需要
Python / uv；修改或重新构建 `frontend/` 时才需要 Node / npm。
