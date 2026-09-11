# AgentRuntime Tasks

本文档是项目唯一任务状态记录入口。

Codex 每完成一个任务必须更新本文件。

状态枚举：

```text
TODO
IN_PROGRESS
BLOCKED
DONE
VERIFIED
```

---

# 当前工作状态

```text
Current Stage: Stage 1
Current Task: S1-02
Last Verified Task: S1-01
Last Verified Commit: 0e146511526bdaeac97dd63e36772bfd618a7f36
Blockers: B-001, B-002
```

---

# Blockers

## B-001：S1-02 验收依赖尚未允许实现的后续任务

**Status:** ACTIVE

S1-02 当前要求一次性验收以下行为，但对应生产执行路径属于后续任务：

- “API 不接受客户端任意指定 user_id”依赖 S1-09 HTTP 请求入口；
- “第一条 HumanMessage 在 Router 前进入 Parent 持久化状态”依赖 S1-03
  Parent State / Graph 和 S1-08 Parent PostgreSQL Checkpointer；
- “Router 失败不会删除 Session / 第一条 HumanMessage”依赖 S1-04 Router 及其
  与上述持久化路径的集成。

在 S1-02 新建独立消息表会形成第二套公共历史，违反 D-007；提前实现 Parent
Graph、Router 或 Checkpointer 又违反当前 Task 边界。因此无法在不偏离已确认
架构的前提下把 S1-02 当前全部 Acceptance 标记通过。

需要负责人裁决 S1-02 的验收归属后继续。

## B-002：本地 PostgreSQL 尚未运行且容器创建未获权限

**Status:** ACTIVE

2026-09-11 检查结果：本机没有 PostgreSQL 服务、进程或已有容器监听 5432；
本机已有 `postgres:16-alpine` 镜像。创建仅绑定 `127.0.0.1:5432` 的本地容器、
持久化卷和 `agent_runtime` 数据库的权限请求被拒绝，因此没有产生容器、数据卷
或数据库变更。

# Proposed Changes

## PC-001：重新分配 S1-02 跨任务验收项

**Status:** AWAITING_DECISION

建议保持现有架构不变，仅调整任务验收归属：

- S1-02 实现并验收 Session 五字段实体、PostgreSQL 元数据持久化、固定配置
  user_id 的应用服务入口、Session UUID 和 HumanMessage UUID 分配，以及明确的
  title 截取长度；
- S1-03 / S1-08 验收首条 HumanMessage 进入真实 Parent Checkpointer，并保证
  写入发生在 Router 前；
- S1-04 与 S1-08 集成后验收 Router 失败不删除已持久化数据；
- S1-09 验收 HTTP 请求 Schema 不接受 user_id。

还需明确 `title` 截取的最大字符数；当前文档只写“截取”，没有可确定测试的
长度规则。

---

# Stage 1：最小 Agent Runtime

## S1-00 架构基线确认

**Status:** VERIFIED

**Dependencies:** None

### Work

- 将负责人确认的状态所有权、10/5 上下文、有限回流和零输出拒绝写入文档
- 明确百炼 OpenAI 兼容接口、Python 3.12、`uv` 和 Fake Model 测试基线
- 修正 Stage 1 任务依赖与验收缺口
- 由负责人复核更新后的文档

### Acceptance

- [x] Requirements / Architecture / Decisions / Tasks / AGENTS / README 一致
- [x] 已记录所有已确认架构裁决
- [x] 未修改业务代码
- [x] 负责人完成书面复核

### Verification

- 2026-09-11：负责人书面确认“文档验收通过”
- Reviewed Commit：`e4da555b768e1ad57f87ef58b4959292b712e187`
- Result：架构基线已验收；下一允许任务为 `S1-01`

---

## S1-01 工程初始化

**Status:** VERIFIED

**Dependencies:** S1-00

### Work

- 创建 `src/agent_runtime` 包
- 创建 `pyproject.toml` 和 `uv.lock`
- 固定 Python 3.12 和兼容的核心依赖版本
- FastAPI 应用入口
- 基础配置模块
- 日志基础配置
- PostgreSQL 连接配置
- 基础异常结构
- `.env.example` 与忽略真实 `.env` 的 Git 配置
- `/health`

### Acceptance

- [x] 应用可启动
- [x] `/health` 返回 200
- [x] 配置可从环境变量读取
- [x] PostgreSQL 连接配置可用
- [x] 百炼 API Key / Base URL / Model 可通过环境变量配置
- [x] 测试框架可运行
- [x] 默认测试不需要真实模型密钥

### Verification

- 2026-09-11：`uv lock --check` 通过，锁定 Python `==3.12.*` 与 66 个包
- 2026-09-11：`uv run pytest -q` 通过，11 个测试全部通过
- 2026-09-11：`uv pip check` 通过，64 个已安装包依赖兼容
- 2026-09-11：`uv build` 通过，成功构建 sdist 与 wheel
- 2026-09-11：`python -m compileall -q src tests` 与 `git diff --check` 通过
- 2026-09-11：真实 Uvicorn 进程启动后请求 `/health` 返回 `{"status":"ok"}`
- 2026-09-11：独立代码评审无 Critical；DSN 兼容性问题修复后无剩余代码问题
- 2026-09-11：负责人确认验收通过，同意 S1-01 状态更新为 `VERIFIED`
- Result：S1-01 已通过独立审核与负责人验收，状态更新为 `VERIFIED`

---

## S1-02 Session 最小实体

**Status:** BLOCKED

**Dependencies:** S1-01

### Work

实现最小 Session：

```text
session_id
user_id
title
created_at
updated_at
```

Stage 1 使用配置中的固定 `user_id`。无 `session_id` 时，先创建 Session，
再为第一条 HumanMessage 分配稳定 UUID 并写入 Parent，最后执行 Router。

### Acceptance

- [ ] 无 session_id 时可创建 Session
- [ ] API 不接受客户端任意指定 user_id
- [ ] title 截取第一条 HumanMessage
- [ ] Session 可持久化 PostgreSQL
- [ ] 第一条 HumanMessage 在 Router 前进入 Parent 持久化状态
- [ ] Router 失败不会删除 Session
- [ ] Router 失败不会删除第一条 HumanMessage

---

## S1-03 Parent State 与 Parent Graph 骨架

**Status:** TODO

**Dependencies:** S1-01, S1-02

### Work

实现：

```text
START
→ route
→ invoke_capability
→ END
```

Parent State 至少：

```text
messages
resolved_capability_id
rejected_capability_ids
```

同时完成最小技术验证：Parent 分别调用 `create_agent` 和 `StateGraph` Child，
并验证不同 State Schema、独立 Child thread_id 和 Child token stream 可以被
Parent 观察。

### Acceptance

- [ ] Parent Graph 可 compile
- [ ] Parent State 不包含任何 Child 业务私有字段
- [ ] session_id 可作为 parent thread_id
- [ ] create_agent Child 和 StateGraph Child 均可被 Parent 调用
- [ ] Child token 可穿透 Parent stream
- [ ] rejected_capability_ids 每个新请求都会重置

---

## S1-04 Router

**Status:** TODO

**Dependencies:** S1-03

### Work

Router 只选择：

```text
general_chat
en_to_zh
```

结构化输出：

```text
capability_id
confidence
```

Router 只能从本轮未拒绝当前 HumanMessage 的候选中选择。

### Acceptance

- [ ] 普通聊天可路由 general_chat
- [ ] 明确英译汉可路由 en_to_zh
- [ ] Router 输出经过 schema 校验
- [ ] Router 输出经过候选集校验
- [ ] 已拒绝 Capability 不会被再次选择
- [ ] Router 非法输出属于 error，不属于 OUT_OF_SCOPE
- [ ] 未实现 interrupt / TopN
- [ ] 默认测试使用 Fake Model

---

## S1-05 general_chat

**Status:** TODO

**Dependencies:** S1-03

### Work

使用 LangChain `create_agent` 实现普通聊天 Agent。

在进入用户可见生成和业务消息持久化前执行 Capability 自有范围守卫。

能力边界：

```text
允许普通聊天
禁止所有翻译
默认简洁直接回答
```

### Acceptance

- [ ] 普通聊天正常
- [ ] 多轮聊天正常
- [ ] 翻译请求返回 OUT_OF_SCOPE
- [ ] OUT_OF_SCOPE 不产生用户可见 token
- [ ] OUT_OF_SCOPE 不推进 Child 消息历史
- [ ] 简短风格通过 System Prompt 注入，不进行二次模型压缩
- [ ] 默认测试使用 Fake Model

---

## S1-06 en_to_zh

**Status:** TODO

**Dependencies:** S1-03

### Work

使用 LangGraph `StateGraph` 实现英译汉。

在进入用户可见生成和业务消息持久化前执行 Capability 自有范围守卫。

### Acceptance

- [ ] 英文可翻译为中文
- [ ] 普通聊天返回 OUT_OF_SCOPE
- [ ] 中文翻英文返回 OUT_OF_SCOPE
- [ ] 非翻译任务返回 OUT_OF_SCOPE
- [ ] 忠实完整翻译当前 HumanMessage，不主动删减
- [ ] 历史只辅助语境、代词和术语一致性
- [ ] OUT_OF_SCOPE 不产生用户可见 token
- [ ] OUT_OF_SCOPE 不推进 Child 消息历史
- [ ] 使用独立 State
- [ ] 默认测试使用 Fake Model

---

## S1-07 ChildResult 与 OUT_OF_SCOPE 回流

**Status:** TODO

**Dependencies:** S1-04, S1-05, S1-06

### Work

实现最小：

```text
status
control_signal
```

实现：

```text
Child OUT_OF_SCOPE
→ 记录本轮 rejected_capability_ids
→ Parent Router 只看未拒绝候选
→ 对同一 HumanMessage 重新路由
```

每个 Capability 对同一条 HumanMessage 最多尝试一次。全部拒绝后 Parent
生成固定简短的 unsupported AIMessage，并正常结束本轮。

### Acceptance

- [ ] OUT_OF_SCOPE 与 failed/error 分离
- [ ] OUT_OF_SCOPE 不产生 Child 用户可见输出
- [ ] OUT_OF_SCOPE 不推进被拒绝 Child 的消息历史
- [ ] 同一 HumanMessage 不重复写入 messages
- [ ] general_chat → en_to_zh 切换成功
- [ ] en_to_zh → general_chat 切换成功
- [ ] Router 不会再次选择本轮已拒绝的 Capability
- [ ] 两个 Capability 全部拒绝后不会循环
- [ ] unsupported 回复写入 Parent messages
- [ ] unsupported 回复由 Parent 发送 message 事件
- [ ] unsupported 使用 done.status，不发送 error

---

## S1-08 PostgreSQL Checkpointer

**Status:** TODO

**Dependencies:** S1-02, S1-03, S1-05, S1-06, S1-07

### Work

- Parent Checkpointer
- general_chat Checkpointer
- en_to_zh Checkpointer
- Parent 完整公共 messages 权威源
- Context Builder 与 10/5 上下文规则
- Child 派生消息视图刷新
- 最终完整 AIMessage 写回 Parent

### Acceptance

- [ ] Parent / Child Checkpointer 逻辑隔离
- [ ] Child A / Child B 状态隔离
- [ ] general_chat child thread_id = `{session_id}:general_chat`
- [ ] en_to_zh child thread_id = `{session_id}:en_to_zh`
- [ ] Parent 保存完整公共对话
- [ ] 当前 HumanMessage 之前不足 10 个已完成轮次时传递全部已完成轮次
- [ ] 已有 10 个已完成轮次后只传最近 5 个完整轮次和当前 HumanMessage
- [ ] System Message 始终保留且不计入轮数
- [ ] 上下文裁剪不删除 Parent 历史
- [ ] Child 消息视图不会与旧输入重复累加
- [ ] unsupported / incomplete 不计入完整轮次
- [ ] 服务重启后 Session 状态可恢复
- [ ] 同一 Session 返回当前 Capability 后可继续执行

---

## S1-09 HTTP + SSE

**Status:** TODO

**Dependencies:** S1-02, S1-03, S1-07, S1-08

### Work

实现：

```http
POST /api/v1/chat/completions
```

请求包含可选 `session_id` 和必填 `message.content`。Stage 1 使用固定本地
`user_id`，并为 HumanMessage / AIMessage 分配稳定 UUID。

最小 SSE：

```text
message
error
done
```

```text
message → session_id + message_id + delta
error   → session_id + code + message + retryable
done    → session_id + status
```

### Acceptance

- [ ] LLM token 可流式返回
- [ ] 新 Session 从首个 SSE 事件起即可获得 session_id
- [ ] 同一回复所有 delta 使用相同 message_id
- [ ] FastAPI 对 LangGraph 内部 stream 做转换
- [ ] 前端协议不暴露原始 StateSnapshot
- [ ] 前端协议不暴露 node 名称
- [ ] done.status 只允许 completed / unsupported / failed
- [ ] 建立 SSE 后的运行错误发送 error → done(failed)
- [ ] 请求或 Session 校验失败在 SSE 前返回 HTTP 4xx JSON
- [ ] 部分输出后失败会保存 incomplete AIMessage

---

## S1-10 Stage 1 集成测试

**Status:** TODO

**Dependencies:** S1-01 ~ S1-09

### Acceptance Case A

```text
用户：介绍一下 LangGraph
→ general_chat
```

- [ ] PASS

### Acceptance Case B

```text
当前 general_chat
用户：Translate "How are you?" into Chinese
→ general_chat OUT_OF_SCOPE
→ Router
→ en_to_zh
```

- [ ] PASS

### Acceptance Case C

```text
当前 en_to_zh
用户：Good morning
→ 继续 en_to_zh
→ 不经过 Router
```

- [ ] PASS

### Acceptance Case D

```text
当前 en_to_zh
用户：帮我介绍一下 FastAPI
→ en_to_zh OUT_OF_SCOPE
→ Router
→ general_chat
```

- [ ] PASS

### Acceptance Case E

```text
用户：把“你好”翻译成英文
→ 每个 Capability 最多尝试一次
→ Parent 返回 unsupported
→ 不发送 error
```

- [ ] PASS

### Acceptance Case F

```text
完成 10 轮后仍传递全部完整对话
提交第 11 轮 HumanMessage
→ Child 只收到最近 5 个完整轮次 + 当前 HumanMessage
→ Parent 完整历史仍然存在
```

- [ ] PASS

### Acceptance Case G

```text
Child OUT_OF_SCOPE
→ 零 message 事件
→ 零 Child 消息历史推进
→ Parent HumanMessage 只存在一次
```

- [ ] PASS

### Model Strategy

- [ ] 默认完整验收使用 Fake Model
- [ ] 不配置百炼密钥时自动化测试仍可通过
- [ ] 真实百炼 smoke test 可选且与默认测试分离

### Persistence

- [ ] 服务重启后已有 Session 可继续
- [ ] Parent 恢复后包含完整公共历史和当前 Capability
- [ ] Parent / 两个 Child 的 thread_id 和 Checkpointer 状态隔离

### Scope Audit

确认 Stage 1 未实现：

- [ ] Manifest
- [ ] 权限
- [ ] mount/unmount
- [ ] interrupt/resume
- [ ] regenerate
- [ ] stop
- [ ] feedback
- [ ] 文件能力
- [ ] Redis
- [ ] Deep Agents

全部通过后：

```text
Stage 1 = VERIFIED
```

---

# Stage 2：完整聊天产品能力

> Stage 1 VERIFIED 前禁止开始。

## S2-01 Session 列表

**Status:** TODO

实现：

```http
GET /api/v1/chat/sessions
```

---

## S2-02 Session 改名

**Status:** TODO

```http
PATCH /api/v1/chat/sessions/{session_id}/rename
```

---

## S2-03 单 Session 单 Run

**Status:** TODO

使用进程内：

```text
active_runs[session_id]
```

重复请求返回：

```text
SESSION_BUSY
```

---

## S2-04 Stop

**Status:** TODO

```http
POST /api/v1/chat/sessions/{session_id}/stop
```

---

## S2-05 历史消息 Adapter

**Status:** TODO

```http
GET /api/v1/chat/sessions/{session_id}/messages
```

Checkpoint History → 前端消息结构。

---

## S2-06 Regenerate / Checkpoint Fork

**Status:** TODO

```http
POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate
```

---

## S2-07 Feedback

**Status:** TODO

```http
POST /api/v1/chat/messages/{message_id}/feedback
```

---

## S2-08 Session 删除

**Status:** TODO

```http
DELETE /api/v1/chat/sessions/{session_id}
```

执行：

```text
stop active run
→ delete parent checkpoints
→ delete child checkpoints
→ delete session
```

---

## S2-09 Stage 2 集成验收

**Status:** TODO

验收项后续在 Stage 1 完成后细化。

---

# Stage 3：Capability Runtime 平台化

> Stage 2 VERIFIED 前禁止开始。

## S3-01 Capability Manifest

**Status:** TODO

---

## S3-02 Capability Registry

**Status:** TODO

---

## S3-03 Local Capability mount

**Status:** TODO

---

## S3-04 Graceful unmount / DRAINING

**Status:** TODO

---

## S3-05 User Capability Permission

**Status:** TODO

---

## S3-06 Router Confidence

**Status:** TODO

---

## S3-07 interrupt / resume

**Status:** TODO

---

## S3-08 /resume SSE

**Status:** TODO

---

## S3-09 Stage 3 集成验收

**Status:** TODO

验收项后续在 Stage 2 完成后细化。

---

# Future Backlog（只记录，不实施）

- [ ] 文件上传
- [ ] 文件服务器 / 对象存储
- [ ] 文件解析
- [ ] RAG
- [ ] Capability Admin API
- [ ] 管理平台
- [ ] RBAC
- [ ] Remote Capability
- [ ] Redis 多实例协调
- [ ] Run History
- [ ] Observability
- [ ] Evaluation
- [ ] Complex Task Agent
- [ ] Deep Agents
- [ ] 多 Agent 串联 / 并联
- [ ] Context Engineering
- [ ] Router 自动优化
