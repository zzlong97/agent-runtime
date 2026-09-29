# AgentRuntime

AgentRuntime 是一个基于 **FastAPI + LangGraph 1.0+ + PostgreSQL** 构建的可扩展对话型 Agent Runtime。

项目目标不是一次性构建完整 Agent Platform，而是按阶段逐步实现：

1. 先验证主图能够稳定调度多个子 Agent；
2. 再补齐完整聊天产品能力；
3. 再把执行重构为持久、可重连、可取消、可中断和可恢复的 Run；
4. 后续阶段根据这套 Runtime 的真实结果重新设计。

---

## 1. 当前定位

当前项目已经验收通过 **Stage 1：最小 Agent Runtime** 和
**Stage 2：完整聊天产品能力与演示页面**。当前正按任务顺序建设
**Stage 2.5：持久化 Run 与可恢复 Runtime**；具体已验收边界、当前任务和最新证据
以 `docs/tasks.md` 为准。

项目继续面向单用户可信环境，使用 Python 3.12 和 `uv`。模型通过阿里云百炼
的 OpenAI 兼容接口接入；自动化测试默认使用 Fake Model，真实模型只用于
可选 smoke test。

已验收的 Stage 2 页面历史基线为：

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

当前后端已验收实现到 S2.5-07，Run 已从 POST SSE 和进程内 Registry 中解耦：

```text
POST 创建持久 Run → HTTP 202
             ↓
Single-process Coordinator / Executor
             ↓
Parent Graph → PostgreSQL Checkpoint
             ↓
PostgreSQL RuntimeEvent + Redis Stream
             ↓
GET SSE Gateway → API 客户端（/chat 页面迁移属于 S2.5-10）
             ↓
异步 Cancel + 非空终态 + Session 完整硬删除
             ↓
同 Run Interrupt / Resume
```

当前代码已具备 PostgreSQL 持久 Run、RuntimeEvent、Redis Stream 尽力发布、
HTTP 202 Run API、活动 Run 查询、单进程 Coordinator，以及合并 PostgreSQL 与
Redis 的独立 GET SSE、异步 Cancel、非空终态消息和 Session 完整硬删除。
同 Run Interrupt/Resume 已通过验收；崩溃恢复及页面迁移仍须按
`docs/tasks.md` 后续任务逐项实现。

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

已验收实现：

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

### Stage 2.5：持久化 Run 与可恢复 Runtime

当前已验收实现到 S2.5-07：

- PostgreSQL 持久 Run、幂等请求和数据库活动 Run 唯一约束
- 类型化 RuntimeEvent、每 Run Sequencer 和公开/内部事件隔离
- Redis Streams 短期公开实时事件与故障降级
- HTTP 202 Run API、活动 Run 查询和单进程 Coordinator
- 独立 GET SSE、PostgreSQL/Redis 事件合并、背压和断点续传
- 异步 Cancel、协作式/超时强制取消、首终态竞争和非空终态消息
- Session 删除屏障及 Run、RuntimeEvent、Redis、Checkpoint、Feedback 完整清理
- 单 pending Interrupt、同 Run Resume、恢复请求幂等和刷新后中断提示恢复

后续计划：

- Checkpoint-first 对账与最多三次崩溃恢复
- 最小 Agent 执行契约
- `/chat` 页面迁移和开发环境真实 Runtime 演示模式

### Future：候选方向，不构成阶段承诺

- 文件上传 / 文件服务 / RAG
- Capability Manifest / Registry
- 动态 `mount()/unmount()` / DRAINING
- Capability 权限 / RBAC
- Router confidence 与确认策略
- Capability Admin API
- 管理平台
- Remote Capability
- 多实例 Runtime / Worker Lease / 分布式任务队列
- Transactional Outbox
- Run History 产品能力与独立保留策略
- 多审批人和并行中断
- Observability / Evaluation
- Complex Agent
- Deep Agents
- Agent 串联 / 并联
- Context Engineering

---

## 5. 运行当前 Runtime

以下说明运行 S2.5-07 后端及其 PostgreSQL / Redis 开发依赖。当前
`/chat` 仍是已验收的 Stage 2 构建产物，尚未迁移到 HTTP 202 Run API；完整
页面迁移属于 S2.5-10。在该任务完成前，请使用下方 API 验证当前后端。

本地 PostgreSQL 和 Redis 由 Windows Docker Desktop 承载。先确认 Docker Desktop
已启动，然后在项目根目录启动依赖：

```powershell
docker compose up -d postgres redis
docker compose ps
```

PostgreSQL 使用 `agent-runtime-postgres` 命名卷持久化。普通停止和再次启动不会
删除数据。Redis 只保存 30 分钟短期公开实时事件，关闭 RDB/AOF 且不使用持久卷：

```powershell
docker compose stop postgres redis
docker compose up -d postgres redis
```

已提交的前端生产构建由 FastAPI 同源托管，不依赖 Node.js。数据库状态为
`healthy` 后启动服务：

```powershell
uv run --env-file .env python -m agent_runtime
```

普通消息必须提供全局唯一 `request_id`，成功提交后立即返回 HTTP
202 和公开 Run 摘要：

```powershell
$requestId = [guid]::NewGuid().ToString()
$body = @{
  request_id = $requestId
  message = @{ content = "你好" }
} | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
```

页面刷新恢复时使用返回的 `session_id` 查询当前活动 Run；空闲时返回
JSON `null`：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8000/api/v1/chat/sessions/{session_id}/active-run"
```

使用创建 Run 时返回的 `run_id` 连接独立 SSE；`id` 等于 Run 内事件 `seq`，
序号允许因租约产生合法缺号：

```powershell
curl.exe -N `
  "http://127.0.0.1:8000/api/v1/chat/runs/{run_id}/events?after_seq=0"
```

浏览器自动重连可发送 `Last-Event-ID`；它与 `after_seq` 同时存在时优先：

```powershell
curl.exe -N `
  -H "Last-Event-ID: 7" `
  "http://127.0.0.1:8000/api/v1/chat/runs/{run_id}/events?after_seq=2"
```

Gateway 以有限批次合并 PostgreSQL durable event 与 Redis 实时事件，并按 `seq`
排序去重。20 秒心跳使用无 `id` 的 SSE 注释帧；客户端断开或写入超时只关闭该
SSE 连接，不会取消仍在执行的 Run。

显式取消使用独立 Run 接口。接口在持久化取消意图后返回 HTTP 202；重复调用幂等，
返回 PostgreSQL 中的当前权威状态：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/chat/runs/{run_id}/cancel"
```

可通过 `.env` 的 `RUN_CANCEL_GRACE_SECONDS` 配置协作式取消宽限秒数。超过宽限期
只强制取消当前进程内任务，不回滚已经发生的外部副作用。Session 删除会等待活动
Run 进入终态；Redis Stream 清理失败时由 TTL 兜底，不阻塞 PostgreSQL 硬删除。

应用会把生命周期、HTTP、Session、Run、Parent、Router 和 Capability 的关键
入口、出口及失败事件写入当天日志。默认文件是
`logs/agent-runtime-YYYY-MM-DD.log`；可在 `.env` 中通过 `LOG_DIR` 修改目录，
通过 `LOG_LEVEL` 修改级别。PowerShell 可实时查看当天日志：

```powershell
Get-Content ".\logs\agent-runtime-$(Get-Date -Format yyyy-MM-dd).log" -Wait
```

业务日志包含 `session_id`、`message_id`、`capability_id`、状态和耗时等定位字段，
不会记录对话正文、模型输出正文、Prompt 或凭据。

只有明确需要清空全部本地会话、反馈和 Checkpoint 时才删除命名卷：

```powershell
docker compose down --volumes
```

只有修改前端源码后才需要重新安装依赖并构建：

```powershell
cd frontend
npm install
npm run build
```

构建产物会写入 `src/agent_runtime/static/`，并由 FastAPI 通过 `/chat` 和
`/assets/*` 提供。

---

## 6. 分层验收

默认后端验收使用 Fake Model，不读取真实模型密钥，也不访问模型网络：

```powershell
uv run pytest -q
```

前端组件、构建和静态资源边界验收：

```powershell
cd frontend
npm install
npm test
npm run build
cd ..
uv run pytest -q tests/test_static_chat.py
```

PostgreSQL 集成测试需要根目录 `.env` 中的 `DATABASE_URL` 指向已创建且可连接的
测试数据库。Redis 集成测试需要 `REDIS_URL` 指向根 Compose 服务；两类测试分别
通过环境变量显式启用：

```powershell
$env:RUN_POSTGRES_TESTS='1'
$env:RUN_REDIS_TESTS='1'
uv run pytest -q
```

可选浏览器端到端验收使用同一 PostgreSQL，并注入确定性 Fake Model。测试服务
为每次运行生成独立端口和唯一的 `stage-two-playwright-*` 测试用户；启动时清理
本运行遗留数据，并在每个用例结束及正常关闭时再次只清理该运行的数据：

```powershell
cd frontend
npx playwright install chromium
npm run test:e2e
```

如果环境已安装受 Playwright 支持的系统浏览器，也可显式指定通道，例如 Windows
上的 Edge：

```powershell
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
npm run test:e2e
```

并发或固定环境也可通过 `PLAYWRIGHT_E2E_PORT` 和 `PLAYWRIGHT_E2E_RUN_ID`
显式指定互不冲突的端口与运行标识。

真实百炼调用仍仅通过独立 smoke test 显式启用，不属于默认或端到端验收门禁。

---

## 7. 文档导航

- [AGENTS.md](./AGENTS.md)：Codex / Agent 工程操作规范
- [docs/requirements.md](./docs/requirements.md)：需求与阶段边界
- [docs/architecture.md](./docs/architecture.md)：技术架构与实现约束
- [docs/decisions.md](./docs/decisions.md)：已裁决的架构决策
- [docs/tasks.md](./docs/tasks.md)：任务执行与验收状态

---

## 8. 当前开发规则

任何开发开始前必须先阅读：

1. `AGENTS.md`
2. `docs/requirements.md`
3. `docs/architecture.md`
4. `docs/decisions.md`
5. `docs/tasks.md`

当前只允许执行 `docs/tasks.md` 中标记的 **Stage 2.5 当前任务**。

S2.5-00 架构基线未验收前，不允许开始 S2.5-01；任何 S2.5 任务都不得顺手实现
Future 候选能力。原 Stage 3 已移出正式阶段，后续范围必须在 S2.5 完成后重新设计。

真实百炼联调时在项目根目录 `.env` 配置 `DASHSCOPE_API_KEY`、
`LLM_BASE_URL` 和 `LLM_MODEL`；不得提交真实密钥。

Stage 2 页面生产构建由 FastAPI 同源提供。运行已提交的演示资源只需要
Python / uv；修改或重新构建 `frontend/` 时才需要 Node / npm。
