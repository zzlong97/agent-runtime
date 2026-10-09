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
Current Stage: Stage 3（IN_PROGRESS）
Current Task: S3-07（VERIFIED）
Last Verified Task: S3-07
Last Verified Commit: S3-07 验收提交（当前提交）
Blockers: None
```

---

# Blockers

None.

# Proposed Changes

None.

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

**Status:** VERIFIED

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

- [x] 无 session_id 时可创建 Session
- [x] API 不接受客户端任意指定 user_id
- [x] title 截取第一条 HumanMessage
- [x] Session 可持久化 PostgreSQL
- [x] 第一条 HumanMessage 在 Router 前进入 Parent 持久化状态
- [x] Router 失败不会删除 Session
- [x] Router 失败不会删除第一条 HumanMessage

### Verification

- 2026-09-12：默认 `uv run pytest -q` 通过，24 个测试通过，1 个 PostgreSQL
  集成测试按显式标记跳过
- 2026-09-12：启用 `RUN_POSTGRES_TESTS=1` 后，真实 PostgreSQL 集成测试通过；
  在 Router 内及 Router 抛错后均可恢复 Session 与首条 Parent HumanMessage
- 2026-09-12：本地 `agent-runtime-postgres` 容器健康，数据库
  `agent_runtime` 已初始化 Session 五列表及 LangGraph checkpoint 表
- 2026-09-12：`uv run python -m agent_runtime` 启动成功，真实请求
  `/health` 返回 200；Windows 使用 psycopg 兼容 SelectorEventLoop
- 2026-09-12：独立代码评审无 Critical、Important 或 Minor 问题，
  Assessment 为 Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-02 已验收并更新为 `VERIFIED`；允许开始 S1-03

---

## S1-03 Parent State 与 Parent Graph 骨架

**Status:** VERIFIED

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

- [x] Parent Graph 可 compile
- [x] Parent State 不包含任何 Child 业务私有字段
- [x] session_id 可作为 parent thread_id
- [x] create_agent Child 和 StateGraph Child 均可被 Parent 调用
- [x] Child token 可穿透 Parent stream
- [x] rejected_capability_ids 每个新请求都会重置

### Verification

- 2026-09-12：Parent State 仅包含 `messages`、
  `resolved_capability_id`、`rejected_capability_ids`，固定图拓扑编译并按
  `START → route → invoke_capability → END` 执行
- 2026-09-12：真实 `create_agent` Child 与自定义 `StateGraph` Child 均由
  Parent 调用；二者使用独立 State Schema、Checkpointer 和 capability 级
  thread_id
- 2026-09-12：两类 Child token 均可穿透 Parent custom stream，聚合后的最终
  `AIMessage` 使用调用方分配的稳定 UUID 写回 Parent `messages`，Child 私有字段
  未泄漏
- 2026-09-12：真实 PostgreSQL 验证关闭首个 saver / graph 后，以全新 saver /
  graph 恢复完整 Human + AI 公共历史
- 2026-09-12：默认 `uv run pytest -q` 通过，31 个测试通过，2 个 PostgreSQL
  集成测试按显式标记跳过；启用 `RUN_POSTGRES_TESTS=1` 后全量 33 个测试通过
- 2026-09-12：`uv lock --check`、`uv pip check`、源码编译和构建检查通过
- 2026-09-12：独立代码二次复核无 Critical、Important 或 Minor 问题，
  Assessment 为 Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-03 已验收并更新为 `VERIFIED`；允许开始 S1-04

---

## S1-04 Router

**Status:** VERIFIED

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

- [x] 普通聊天可路由 general_chat
- [x] 明确英译汉可路由 en_to_zh
- [x] Router 输出经过 schema 校验
- [x] Router 输出经过候选集校验
- [x] 已拒绝 Capability 不会被再次选择
- [x] Router 非法输出属于 error，不属于 OUT_OF_SCOPE
- [x] 未实现 interrupt / TopN
- [x] 默认测试使用 Fake Model

### Verification

- 2026-09-12：`StageOneRouter` 使用 LangChain `with_structured_output()` 绑定
  `RouterDecision`，未使用自由文本解析或启发式规则
- 2026-09-12：`capability_id` 和 `confidence` 均提供明确中文 Schema
  `description`；禁止额外字段并校验 confidence 范围为 0.0 至 1.0
- 2026-09-12：Fake Chat Model 验证普通聊天、明确英译汉、候选过滤、候选越权、
  Schema 非法、模型异常、无候选及 Parent Graph 接入
- 2026-09-12：低 confidence 仍直接返回 Top1；未实现 interrupt、人工确认或
  TopN
- 2026-09-12：默认 `uv run pytest -q` 通过，43 个测试通过，2 个 PostgreSQL
  集成测试按显式标记跳过；启用 `RUN_POSTGRES_TESTS=1` 后全量 45 个测试通过
- 2026-09-12：`uv lock --check`、`uv pip check`、源码编译、构建和
  `git diff --check` 通过
- 2026-09-12：独立代码评审无 Critical、Important 或 Minor 问题，
  Assessment 为 Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-04 已验收并更新为 `VERIFIED`；允许开始 S1-05

---

## S1-05 general_chat

**Status:** VERIFIED

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

- [x] 普通聊天正常
- [x] 多轮聊天正常
- [x] 翻译请求返回 OUT_OF_SCOPE
- [x] OUT_OF_SCOPE 不产生用户可见 token
- [x] OUT_OF_SCOPE 不推进 Child 消息历史
- [x] 简短风格通过 System Prompt 注入，不进行二次模型压缩
- [x] 默认测试使用 Fake Model

### Execution Evidence

- 2026-09-12：新增 `GeneralChatCapability`，使用 LangChain `create_agent`
  实现普通聊天，并通过中文 System Prompt 约束默认简洁、直接回答
- 2026-09-12：新增结构化 `GeneralChatScopeDecision`，在生成和 Child
  Checkpoint 更新前识别所有语言方向的翻译请求并返回 `OUT_OF_SCOPE`
- 2026-09-12：专项测试覆盖普通聊天、同一 Child thread 多轮历史、翻译拒绝
  零事件和零历史推进；`4 passed`
- 2026-09-12：默认全量回归 `47 passed, 2 skipped`；启用 PostgreSQL 后
  全量回归 `49 passed`
- 2026-09-12：独立代码审查无 Critical、Important 或 Minor 问题，结论为
  Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-05 已验收并更新为 `VERIFIED`；允许开始 S1-06

---

## S1-06 en_to_zh

**Status:** VERIFIED

**Dependencies:** S1-03

### Work

使用 LangGraph `StateGraph` 实现英译汉。

在进入用户可见生成和业务消息持久化前执行 Capability 自有范围守卫。

### Acceptance

- [x] 英文可翻译为中文
- [x] 普通聊天返回 OUT_OF_SCOPE
- [x] 中文翻英文返回 OUT_OF_SCOPE
- [x] 非翻译任务返回 OUT_OF_SCOPE
- [x] 忠实完整翻译当前 HumanMessage，不主动删减
- [x] 历史只辅助语境、代词和术语一致性
- [x] OUT_OF_SCOPE 不产生用户可见 token
- [x] OUT_OF_SCOPE 不推进 Child 消息历史
- [x] 使用独立 State
- [x] 默认测试使用 Fake Model

### Execution Evidence

- 2026-09-12：新增独立 `EnglishToChineseState` 和 LangGraph `StateGraph`，
  Child 私有状态仅包含派生消息快照与最近一次译文
- 2026-09-12：新增结构化英文到中文范围守卫；纯英文和明确英译汉请求可进入，
  普通聊天、中文翻英文、其他语言互译、代码生成及非翻译任务返回
  `OUT_OF_SCOPE`
- 2026-09-12：范围守卫在持久化翻译图外执行，拒绝路径不产生消息事件、不调用
  翻译模型且不创建或推进 Child Checkpoint
- 2026-09-12：中文翻译 Prompt 约束只翻译当前 HumanMessage，历史仅辅助代词、
  语境和术语，不省略、概括、解释或添加原文内容
- 2026-09-12：专项测试 `8 passed`；默认全量回归
  `55 passed, 2 skipped`；启用 PostgreSQL 后全量回归 `57 passed`
- 2026-09-12：独立代码审查无 Critical、Important 或 Minor 问题，结论为
  Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-06 已验收并更新为 `VERIFIED`；允许开始 S1-07

---

## S1-07 ChildResult 与 OUT_OF_SCOPE 回流

**Status:** VERIFIED

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

- [x] OUT_OF_SCOPE 与 failed/error 分离
- [x] OUT_OF_SCOPE 不产生 Child 用户可见输出
- [x] OUT_OF_SCOPE 不推进被拒绝 Child 的消息历史
- [x] 同一 HumanMessage 不重复写入 messages
- [x] general_chat → en_to_zh 切换成功
- [x] en_to_zh → general_chat 切换成功
- [x] Router 不会再次选择本轮已拒绝的 Capability
- [x] 两个 Capability 全部拒绝后不会循环
- [x] unsupported 回复写入 Parent messages
- [x] unsupported 回复由 Parent 发送 message 事件
- [x] unsupported 使用 done.status，不发送 error

### Execution Evidence

- 2026-09-12：新增极简 `ChildResult(status, control_signal)`，拒绝状态只能与
  `OUT_OF_SCOPE` 搭配，控制面不接受用户可见内容
- 2026-09-12：`general_chat` 和 `en_to_zh` 均返回标准 `ChildResult`；实际
  模型异常继续作为 `ApplicationError`，不解释为能力越界
- 2026-09-12：Parent Graph 支持当前能力直接续用、拒绝后记录本轮拒绝集并
  重新路由；双向切换均通过，同一能力不会对同一 HumanMessage 重复调用
- 2026-09-12：全部能力拒绝后生成固定 unsupported `AIMessage`，分配稳定 UUID、
  写入 Parent 公共历史、发送内部 message 事件并设置
  `completion_status="unsupported"`，不发送 error 且不继续循环
- 2026-09-12：S1-07 相关专项测试 `34 passed`；默认全量回归
  `63 passed, 2 skipped`；启用 PostgreSQL 后全量回归 `65 passed`
- 2026-09-12：独立代码审查无 Critical、Important 或 Minor 问题，结论为
  Ready
- 2026-09-12：负责人确认验收通过
- Result：S1-07 已验收并更新为 `VERIFIED`；允许开始 S1-08

---

## S1-08 PostgreSQL Checkpointer

**Status:** VERIFIED

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

- [x] Parent / Child Checkpointer 逻辑隔离
- [x] Child A / Child B 状态隔离
- [x] general_chat child thread_id = `{session_id}:general_chat`
- [x] en_to_zh child thread_id = `{session_id}:en_to_zh`
- [x] Parent 保存完整公共对话
- [x] 当前 HumanMessage 之前不足 10 个已完成轮次时传递全部已完成轮次
- [x] 已有 10 个已完成轮次后只传最近 5 个完整轮次和当前 HumanMessage
- [x] System Message 始终保留且不计入轮数
- [x] 上下文裁剪不删除 Parent 历史
- [x] Child 消息视图不会与旧输入重复累加
- [x] unsupported / incomplete 不计入完整轮次
- [x] 服务重启后 Session 状态可恢复
- [x] 同一 Session 返回当前 Capability 后可继续执行

### Execution Evidence

- 2026-09-12：新增 Context Builder，当前消息前不足 10 个已完成轮次时保留
  全部轮次，已有 10 个轮次后仅保留最近 5 轮；System Message 始终保留，
  `unsupported` 和 `incomplete` 不计数、不进入 Child 上下文
- 2026-09-12：两个固定 Capability Adapter 使用
  `RemoveMessage(REMOVE_ALL_MESSAGES)` 先清空旧 Child 消息，再写入 Parent 派生
  快照；多轮与能力切换测试确认不会重复累加旧输入
- 2026-09-12：Parent、`general_chat`、`en_to_zh` 使用三套同时存活的独立
  PostgreSQL saver，thread_id 分别为 `session_id`、
  `{session_id}:general_chat` 和 `{session_id}:en_to_zh`
- 2026-09-12：真实 PostgreSQL 验证关闭第一组 saver / graph 后，以全新 saver /
  graph 恢复 Parent 完整公共历史和当前 Capability，并继续完成能力回流与后续调用；
  两个 Child 状态和私有字段互不污染
- 2026-09-12：最终完整 `AIMessage` 由 Adapter 从 Child 事件聚合、分配服务端稳定
  UUID，并通过数据面写回 Parent 公共 `messages`
- 2026-09-12：Context Builder 与 Adapter / Checkpointer 专项测试
  `10 passed, 1 skipped`；默认全量回归 `73 passed, 3 skipped`；启用真实
  PostgreSQL 后全量回归 `76 passed`
- 2026-09-12：独立代码审查无 Critical、Important 或 Minor 问题，结论为
  Ready；确认未跨入 S1-09
- 2026-09-12：负责人确认验收通过
- Result：S1-08 已验收并更新为 `VERIFIED`；允许开始 S1-09

---

## S1-09 HTTP + SSE

**Status:** VERIFIED

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

- [x] LLM token 可流式返回
- [x] 新 Session 从首个 SSE 事件起即可获得 session_id
- [x] 同一回复所有 delta 使用相同 message_id
- [x] FastAPI 对 LangGraph 内部 stream 做转换
- [x] 前端协议不暴露原始 StateSnapshot
- [x] 前端协议不暴露 node 名称
- [x] done.status 只允许 completed / unsupported / failed
- [x] 建立 SSE 后的运行错误发送 error → done(failed)
- [x] 请求或 Session 校验失败在 SSE 前返回 HTTP 4xx JSON
- [x] 部分输出后失败会保存 incomplete AIMessage

### Execution Evidence

- 2026-09-12：新增 `POST /api/v1/chat/completions`，在建立 SSE 前完成请求、
  Session 所属关系和 Parent 状态校验；新 Session 先持久化 Session 与首条
  HumanMessage，再运行 Router
- 2026-09-12：新增带完整中文字段说明的请求与 SSE Schema；FastAPI Adapter
  只输出 `message`、`error`、`done` 及固定产品字段，不暴露 LangGraph 内部事件
- 2026-09-12：每轮在服务端分配稳定 HumanMessage / AIMessage UUID，并通过
  Parent 配置贯穿 Capability、unsupported 回复、SSE delta 和最终公共消息
- 2026-09-12：流建立后的运行异常转换为 `error → done(failed)`；模型已有部分
  输出时，以相同 AIMessage UUID 将聚合内容标记为 `incomplete` 并写回 Parent
- 2026-09-12：生产应用生命周期装配百炼 OpenAI 兼容模型、固定 Router、两个
  Capability Adapter 及三套独立 PostgreSQL Checkpointer；生命周期打开与关闭验证通过
- 2026-09-12：默认全量回归 `98 passed, 5 skipped`；启用真实 PostgreSQL 后
  全量回归 `103 passed`；依赖锁、环境兼容性、源码编译和分发包构建验证通过
- 2026-09-12：独立代码审查无 Critical、Important 或 Minor 问题，结论为
  Ready；确认未跨入 S1-10
- 2026-09-12：负责人确认验收通过
- Result：S1-09 已验收并更新为 `VERIFIED`；允许开始 S1-10

---

## S1-10 Stage 1 集成测试

**Status:** VERIFIED

**Dependencies:** S1-01 ~ S1-09

### Acceptance Case A

```text
用户：介绍一下 LangGraph
→ general_chat
```

- [x] PASS

### Acceptance Case B

```text
当前 general_chat
用户：Translate "How are you?" into Chinese
→ general_chat OUT_OF_SCOPE
→ Router
→ en_to_zh
```

- [x] PASS

### Acceptance Case C

```text
当前 en_to_zh
用户：Good morning
→ 继续 en_to_zh
→ 不经过 Router
```

- [x] PASS

### Acceptance Case D

```text
当前 en_to_zh
用户：帮我介绍一下 FastAPI
→ en_to_zh OUT_OF_SCOPE
→ Router
→ general_chat
```

- [x] PASS

### Acceptance Case E

```text
用户：把“你好”翻译成英文
→ 每个 Capability 最多尝试一次
→ Parent 返回 unsupported
→ 不发送 error
```

- [x] PASS

### Acceptance Case F

```text
完成 10 轮后仍传递全部完整对话
提交第 11 轮 HumanMessage
→ Child 只收到最近 5 个完整轮次 + 当前 HumanMessage
→ Parent 完整历史仍然存在
```

- [x] PASS

### Acceptance Case G

```text
Child OUT_OF_SCOPE
→ 零 message 事件
→ 零 Child 消息历史推进
→ Parent HumanMessage 只存在一次
```

- [x] PASS

### Model Strategy

- [x] 默认完整验收使用 Fake Model
- [x] 不配置百炼密钥时自动化测试仍可通过
- [x] 真实百炼 smoke test 可选且与默认测试分离

### Persistence

- [x] 服务重启后已有 Session 可继续
- [x] Parent 恢复后包含完整公共历史和当前 Capability
- [x] Parent / 两个 Child 的 thread_id 和 Checkpointer 状态隔离

### Scope Audit

确认 Stage 1 未实现：

- [x] Manifest
- [x] 权限
- [x] mount/unmount
- [x] interrupt/resume
- [x] regenerate
- [x] stop
- [x] feedback
- [x] 文件能力
- [x] Redis
- [x] Deep Agents

全部通过后：

```text
Stage 1 = VERIFIED
```

### Execution Evidence

- 2026-09-12：新增 Stage 1 端到端验收，以真实
  `FastAPI → ChatService → Parent Graph → Router / Child → SSE` 主链路覆盖
  Case A～G；只替换默认测试中的模型与持久化介质
- 2026-09-12：双向能力切换、当前能力直接续用、每个能力最多拒绝一次、拒绝
  Child 零 message 事件和零历史推进、Parent HumanMessage 去重均验证通过
- 2026-09-12：第 10 轮向 Child 传递前 9 个完整轮次和当前 HumanMessage；
  第 11 轮只传递最近 5 个完整轮次和当前 HumanMessage，Parent 仍保留 22 条
  完整公共消息
- 2026-09-12：默认验收显式清空百炼配置并使用确定性 Fake Model；真实百炼
  smoke test 由 `RUN_BAILIAN_SMOKE=1` 单独启用，不进入默认或 PostgreSQL 门禁
- 2026-09-12：真实 PostgreSQL 测试跨两次独立服务生命周期恢复 Session、当前
  Capability 和 Parent 完整公共历史；Parent、`general_chat`、`en_to_zh` 三个
  thread 的消息及 Child 私有状态保持隔离
- 2026-09-12：源码和依赖范围审计未发现 Manifest、权限、动态挂载、
  interrupt/resume、regenerate、stop、feedback、文件能力、Redis 或 Deep Agents
- 2026-09-12：S1-10 专项默认验收 `4 passed, 2 skipped`；默认全量回归
  `102 passed, 7 skipped`；启用真实 PostgreSQL 后全量回归
  `108 passed, 1 skipped`，唯一跳过项为显式隔离的真实百炼 smoke
- 2026-09-12：依赖锁、环境兼容性、源码与测试编译验证通过；独立代码审查无
  Critical、Important 或 Minor 问题，结论为 Ready
- 2026-09-13：负责人确认 Stage 1 已经完成，并开始 Stage 2 设计
- Result：S1-10 与 Stage 1 整体验收通过，允许进入 Stage 2

---

# Stage 2：完整聊天产品能力

> Stage 1 已 VERIFIED。Stage 2 严格按 S2-00～S2-10 顺序推进。

## S2-00 Stage 2 架构基线确认

**Status:** VERIFIED

**Dependencies:** S1-10

### Work

- 细化 Session、History、Active Run、Stop、Regenerate、Feedback 和 Delete 契约
- 明确活动 Parent 分支、消息状态、Capability 展示和 SSE Stage 2 增量
- 增加 React + Vite + Ant Design X 完整聊天演示页面
- 明确默认 Fake Model、前端组件测试和可选浏览器端到端验收

### Acceptance

- [x] 单用户、Parent 权威源和 Child 隔离原则保持不变
- [x] API、状态语义、分页、错误和删除顺序已明确
- [x] Regenerate 资格和活动分支可见性已明确
- [x] 演示页面范围、技术栈和静态资源拆分已明确
- [x] 负责人逐节确认整体设计
- [x] 负责人复核写入仓库后的 Stage 2 文档

### Verification

- 2026-09-13：负责人依次确认产品范围、API 契约、运行与持久化、前端结构和
  验收策略
- 2026-09-13：负责人书面确认“审核通过”
- Reviewed Commit：`191913e5051a06bb3b511a5c2513f491468fd1a4`
- Result：Stage 2 架构基线已验收；下一项 `S2-01` 可交由编码人员实施

## S2-01 Session 列表

**Status:** VERIFIED

**Dependencies:** S2-00

### Work

```http
GET /api/v1/chat/sessions?cursor={cursor}&limit={limit}
```

- 使用不透明游标
- 按 `updated_at DESC, session_id DESC` 稳定排序
- 默认 20 条，限制 1～100 条

### Acceptance

- [x] 只返回固定本地用户的 Session
- [x] 首次请求返回最新一页和可空 `next_cursor`
- [x] 相同排序键下不会重复或遗漏 Session
- [x] 非法游标返回明确客户端错误
- [x] Pydantic 字段均有完整中文 description

### Execution Evidence

- 2026-09-13：新增 `GET /api/v1/chat/sessions` 产品接口；查询只接受
  `cursor` 和 `limit`，默认 20 条且限制为 1～100 条，响应不暴露 `user_id`
- 2026-09-13：Session Service 固定注入配置中的 `local_user_id`；PostgreSQL
  使用 `updated_at DESC, session_id DESC` 复合键、对应索引和 `limit + 1`
  查询生成稳定下一页
- 2026-09-13：游标使用 Base64 URL-safe 编码的版本化 JSON，只携带版本与
  `updated_at + session_id` 排序边界；非法 Base64、JSON、版本、字段、时区或
  UUID 统一返回 HTTP 400 `SESSION_CURSOR_INVALID`
- 2026-09-13：真实 PostgreSQL 测试以 3 个相同 `updated_at` 的 Session 跨越
  `limit=2` 页边界，确认无重复、无遗漏，并确认其他用户数据不可见
- 2026-09-13：默认全量回归 `122 passed, 8 skipped`；启用真实 PostgreSQL 后
  全量回归 `129 passed, 1 skipped`，唯一跳过项为真实百炼 smoke
- 2026-09-13：依赖锁、源码与测试编译、源码包和 wheel 构建、OpenAPI 参数、
  暂存差异检查均通过；独立代码审查无 Critical、Important 或 Minor 问题，
  结论为 Ready
- 2026-09-13：负责人确认验收通过
- Result：S2-01 已验收并更新为 `VERIFIED`；允许开始 S2-02

---

## S2-02 Session 改名

**Status:** VERIFIED

**Dependencies:** S2-01

### Work

```http
PATCH /api/v1/chat/sessions/{session_id}/rename
```

- 标题 trim 后限制 1～100 字符
- 不调用模型生成标题

### Acceptance

- [x] 合法标题持久化并返回更新后的 Session
- [x] 空标题、超长标题和额外字段被拒绝
- [x] 不存在或不属于固定用户的 Session 返回 404
- [x] 服务重启后新标题仍可恢复

### Execution Evidence

- 2026-09-13：新增 `PATCH /api/v1/chat/sessions/{session_id}/rename`；请求体只
  接受 `title`，先去除首尾空白，再限制为 1～100 个字符，未调用模型生成标题
- 2026-09-13：请求、响应和路径参数均提供完整中文说明；响应只返回
  `session_id`、`title`、`created_at`、`updated_at`，不暴露 `user_id`
- 2026-09-13：PostgreSQL 使用单条参数化 `UPDATE`，同时按 `session_id` 和
  配置中的固定 `local_user_id` 过滤；不存在和越权目标统一返回 HTTP 404
  `SESSION_NOT_FOUND`
- 2026-09-13：改名只更新 `title` 和 `updated_at`，保持 `created_at`；真实
  PostgreSQL 测试确认重建 Repository / Service 后仍可恢复新标题，且其他用户
  记录保持不变
- 2026-09-13：默认全量回归 `133 passed, 9 skipped`；启用真实 PostgreSQL 后
  全量回归 `141 passed, 1 skipped`，唯一跳过项为真实百炼 smoke
- 2026-09-13：依赖锁、源码与测试编译、源码包和 wheel 构建、OpenAPI 契约、
  暂存差异检查均通过；独立代码审查无 Critical、Important 或 Minor 问题，
  结论为 Ready
- 2026-09-13：负责人确认验收通过
- Result：S2-02 已验收并更新为 `VERIFIED`；允许开始 S2-03

---

## S2-03 单 Session 单 Run

**Status:** VERIFIED

**Dependencies:** S2-02

### Work

```text
active_runs[session_id] = ActiveRun
```

- 在新增 HumanMessage 和 SSE 前完成 Session 占用
- Graph 在独立 producer task 中运行
- 使用 event queue 向 SSE 适配器传递产品事件

### Acceptance

- [x] 同一 Session 同时只允许一个 Run
- [x] 重复请求在 SSE 前返回 HTTP 409 `SESSION_BUSY`
- [x] 被拒绝的重复请求不写入 HumanMessage
- [x] 不同 Session 可以并发运行
- [x] 完成、失败、停止和断开路径都释放 Registry
- [x] 未引入 Redis 或多实例协调

### Execution Evidence

- 2026-09-13：新增进程内 `ActiveRunRegistry`，以 Session UUID 为键并在同一
  `asyncio.Lock` 下原子占用；重复请求在创建 HumanMessage 和建立 SSE 前返回
  HTTP 409 `SESSION_BUSY`，不同 Session 可分别占用
- 2026-09-13：Graph 改由独立 producer task 执行，使用每个 Run 独立的产品事件
  queue 向 SSE Adapter 传递 `message`、`error` 和 `done`，未向前端暴露
  LangGraph 内部事件
- 2026-09-13：完成、失败、取消、客户端断开、响应体未启动和服务关闭路径均会
  刷新 `Session.updated_at`、结束事件队列、完成 `terminal_future` 并释放 Registry；
  reservation token 与精确 Run 句柄阻止过期响应清理影响 replacement Run
- 2026-09-13：确定性锁竞争测试覆盖取消请求先于 producer 最终 release 获锁的
  路径；sentinel 与 release 在独立清理任务中执行，复审重复 100 次均通过
- 2026-09-13：默认全量回归 `158 passed, 9 skipped`；启用真实 PostgreSQL 后
  全量回归 `166 passed, 1 skipped`，唯一跳过项为真实百炼 smoke；真实数据库
  聊天测试确认终态 `updated_at > created_at`
- 2026-09-13：依赖锁、源码与测试编译、源码包和 wheel 构建、差异检查均通过；
  独立代码复审无 Critical、Important 或 Minor 问题，结论为 Ready
- 2026-09-13：负责人确认验收通过
- Result：S2-03 已验收并更新为 `VERIFIED`；允许开始 S2-04

---

## S2-04 Stop

**Status:** VERIFIED

**Dependencies:** S2-03

### Work

```http
POST /api/v1/chat/sessions/{session_id}/stop
```

- 取消当前 producer task
- 保存 `stopped` AIMessage
- 发送 `done(status="stopped")`
- 等待 Run 清理后返回

### Acceptance

- [x] 已输出文本完整保留并标记为 stopped
- [x] 零文本时仍保存稳定 UUID 的 stopped AIMessage
- [x] Stop 不发送 error，也不回滚历史
- [x] 无 active Run 时幂等成功
- [x] Stop 返回后同 Session 可以立即开始新 Run
- [x] 客户端断开会终止 Run 并按 incomplete 保存

### Execution Evidence

- 2026-09-14：新增 `POST /api/v1/chat/sessions/{session_id}/stop`；接口先校验
  固定本地用户的 Session，再停止当前 Run，无 active Run 时返回幂等 `idle`
- 2026-09-14：producer 聚合已输出文本；Stop 将部分或空内容以稳定响应 UUID、
  `runtime_status=stopped` 和可空 `capability_id` 写回 Parent，随后只发送
  `done(status=stopped)`，不删除 Session 或回滚历史
- 2026-09-14：客户端或 SSE 意外断开时复用同一终态链路，将部分或空输出保存为
  `runtime_status=incomplete`；`stopped` 与 `incomplete` 均不进入后续模型上下文
- 2026-09-14：Run 终态冻结、reservation token、迟到 producer 防护和入口前取消
  恢复共同覆盖 Stop/自然完成/旧 SSE/replacement Run 竞态；并发 Stop 共享同一
  成功或失败终态，返回前已完成持久化、SSE producer 结束和 Registry 清理
- 2026-09-14：Stage 2 SSE `message` 与 `done` 已补齐稳定 `message_id` 和可空
  `capability_id`；新生成的 completed、unsupported、incomplete、stopped 公共
  AIMessage 均保存规定的产品元数据
- 2026-09-14：正常 Stop 不发送 `error`；停止终态持久化或 Session 时间刷新失败时
  不伪报 stopped，而是使用稳定应用错误及 `error → done(failed)` 完成失败清理
- 2026-09-14：默认全量回归 `180 passed, 10 skipped`；启用真实 PostgreSQL 后
  全量回归 `189 passed, 1 skipped`，唯一跳过项为真实百炼 smoke；数据库集成
  测试确认 stopped checkpoint 可恢复且 Stop 后同 Session 可立即开始新 Run
- 2026-09-14：依赖锁、环境依赖一致性、源码与测试编译、源码包和 wheel 构建、
  差异检查均通过；两轮独立代码复审后的最终结论为 Ready，无 Critical 或
  Important 问题，未跟踪 `.idea/` 明确排除在提交外
- 2026-09-14：负责人确认验收通过
- Result：S2-04 已验收并更新为 `VERIFIED`；允许开始 S2-05

---

## S2-05 历史消息 Adapter

**Status:** VERIFIED

**Dependencies:** S2-04

### Work

```http
GET /api/v1/chat/sessions/{session_id}/messages?before={message_id}&limit={limit}
```

- 从最新活动 Parent checkpoint 读取权威 messages
- 转换为 Product Message DTO
- 默认 50 条，限制 1～100 条

### Acceptance

- [x] 页内消息按时间正序返回
- [x] before 只能引用当前 Session 活动历史中的 message_id
- [x] completed / unsupported / incomplete / stopped 均正确展示
- [x] 返回 capability_id 和当前反馈
- [x] 兼容缺少 Stage 2 元数据的 Stage 1 消息
- [x] 不暴露 StateSnapshot、节点、tasks 或 checkpoint metadata

### Execution Evidence

- 2026-09-14：新增 `GET /api/v1/chat/sessions/{session_id}/messages`；查询参数
  `before` 为可空消息 UUID，`limit` 默认 50 且限制为 1～100，额外参数和非法
  UUID 在进入服务前被拒绝
- 2026-09-14：新增 `MessageHistoryAdapter`，先验证固定本地用户，再从未指定
  checkpoint ID 的 Parent `aget_tuple()` 读取最新活动 checkpoint；过滤内部消息、
  转换 Product Message DTO 并按 `before` 在内存中独占切片，页内保持对话正序
- 2026-09-14：Product Message 仅返回 `message_id`、`role`、`content`、
  `runtime_status`、`capability_id` 和 `feedback`；四种 AI 终态均可展示，
  HumanMessage 状态为空，缺少 Stage 2 元数据的旧 AIMessage 按 completed 和空
  capability 兼容
- 2026-09-14：S2-07 前系统不存在可写反馈，因此历史接口准确返回当前空反馈；
  未提前创建 Feedback Store、反馈写入、Regenerate 或分支浏览能力
- 2026-09-14：无效或不在当前活动产品历史中的 `before` 返回 HTTP 400
  `MESSAGE_BEFORE_INVALID`；不存在或不属于固定用户的 Session 统一返回 404，
  Session 行存在但 Parent 状态缺失时返回稳定 409
- 2026-09-14：默认全量回归 `194 passed, 10 skipped`；启用真实 PostgreSQL 后
  全量回归 `203 passed, 1 skipped`，唯一跳过项为真实百炼 smoke；数据库测试
  确认接口可恢复最新 Parent checkpoint 中的完整 Human + completed AI 历史
- 2026-09-14：依赖锁、环境依赖一致性、源码与测试编译、源码包和 wheel 构建、
  OpenAPI 参数说明及差异检查均通过；独立代码复核无 Critical 或 Important
  问题，结论为 Ready；未跟踪 `.idea/` 继续排除在提交外
- 2026-09-14：负责人确认验收通过
- Result：S2-05 已验收并更新为 `VERIFIED`；允许开始 S2-06

---

## S2-06 Regenerate / Checkpoint Fork

**Status:** VERIFIED

**Dependencies:** S2-05

### Work

```http
POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate
```

- 只允许活动分支最新 completed AIMessage
- 从回答前 checkpoint fork，并使用相同 SSE 协议继续 Parent

### Acceptance

- [x] 历史中间回答、unsupported、incomplete、stopped 均不可重新生成
- [x] 原 HumanMessage 不重复且 message_id 保持不变
- [x] 新 AIMessage 获得新 UUID
- [x] 普通历史只返回新活动分支
- [x] 原 checkpoint 和旧反馈仍保留
- [x] 新分支失败或停止时不回滚旧回答
- [x] 前端不能指定 Capability 或绕过 Parent

### Execution Evidence

- 2026-09-14：新增
  `POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate`；
  只接受两个路径 UUID，不定义请求体，也不允许客户端传入 Capability
- 2026-09-14：新增 `CheckpointForker`，先从当前活动 Parent 历史确认目标是
  最后一条 `completed` AIMessage，再按原回答 `message_id` 过滤 checkpoint
  history，并定位 `next == ("invoke_capability",)` 的回答前状态
- 2026-09-14：使用 `aupdate_state` 创建保留原 HumanMessage 和路由状态的新
  checkpoint 分支，再以 `astream(None, fork_config)` 沿 Parent 继续；新回答使用
  新服务端 UUID，未复制原 HumanMessage，客户端不能直接调用 Child
- 2026-09-14：普通历史读取最新活动 checkpoint，因此成功、失败或停止后的新
  分支均保持活动；旧回答只保留在旧 checkpoint 中且未被删除，当前阶段也未删除
  或改写旧反馈，未提前实现 S2-07 Feedback Store 或产品级 branch_id
- 2026-09-14：准备阶段增加与 Active Run 绑定的 turn 发布屏障；Stop 会等待 fork
  配置完成交接后再持久化唯一 stopped 终态。fork 子任务全程受循环 shield 保护，
  重复请求取消只记录取消意图，交接后保存 incomplete 并清理 Run
- 2026-09-14：fork 内部应用错误、自身取消与并发 Stop 统一进行失败仲裁；失败先
  写入稳定 Run 终态再释放 reservation，Stop 不会在没有 stopped AIMessage 时
  伪报成功，也不会遗留过期 turn 或污染替代 Run
- 2026-09-14：资格、HTTP/SSE、真实 Parent Graph、成功/失败/停止分支和竞态等
  S2-06 定向测试 `62 passed`；默认全量回归 `220 passed, 11 skipped`
- 2026-09-14：启用真实 PostgreSQL 后全量回归 `230 passed, 1 skipped`，唯一跳过
  项为真实百炼 smoke；数据库端到端验证最新活动分支只包含原 HumanMessage 与
  新回答，同时旧回答 checkpoint 仍可恢复
- 2026-09-14：独立代码复核最终无 Critical、Important 或 Minor 问题，结论为
  Ready；未跟踪 `.idea/` 继续明确排除在提交外
- 2026-09-14：负责人确认验收通过
- Result：S2-06 已验收并更新为 `VERIFIED`；允许开始 S2-07

---

## S2-07 Feedback

**Status:** VERIFIED

**Dependencies:** S2-06

### Work

```http
POST /api/v1/chat/messages/{message_id}/feedback
```

- 支持 `like`、`dislike`、`cancel`
- 以 `user_id + message_id` 保存最终结果
- 保存 `session_id` 清理索引，支持幂等 Session 硬删除重试

### Acceptance

- [x] 只允许当前活动分支中的 completed AIMessage
- [x] like / dislike 覆盖旧值且不产生重复记录
- [x] cancel 删除当前反馈并保持幂等
- [x] unsupported / incomplete / stopped / HumanMessage 被拒绝
- [x] 历史查询返回当前最终反馈

### Execution Evidence

- 2026-09-14：新增 `POST /api/v1/chat/messages/{message_id}/feedback`；请求
  只接受 `like`、`dislike`、`cancel`，不接受客户端 `user_id`、`session_id`
  或其他扩展字段，路径、请求和响应 Schema 参数均提供明确中文说明
- 2026-09-14：新增独立 PostgreSQL `message_feedback` 表，以
  `(user_id, message_id)` 为主键并使用 `ON CONFLICT` 原子覆盖最终值；
  `cancel` 按唯一键幂等删除，`session_id` 仅保存为 S2-08 清理索引
- 2026-09-14：反馈写入前由 `MessageHistoryAdapter` 遍历固定本地用户各
  Session 的最新活动 Parent checkpoint；只有当前活动分支中的 completed
  AIMessage 通过校验，HumanMessage、unsupported、incomplete、stopped、
  旧分支、未知消息和其他用户消息使用统一 409 错误拒绝
- 2026-09-14：历史 Adapter 只为当前活动分支中的 completed AIMessage 批量
  读取固定用户最终反馈；旧分支反馈记录继续保留，但不会出现在普通历史中
- 2026-09-14：真实 PostgreSQL 端到端测试验证 like → dislike 只保留一行、
  服务重启后可恢复最终反馈、Regenerate 后旧反馈保留且不可写/不可见、新分支
  反馈独立，以及连续两次 cancel 均成功
- 2026-09-14：S2-07 定向测试 `82 passed`；默认全量回归
  `244 passed, 12 skipped`；启用真实 PostgreSQL 后全量回归
  `255 passed, 1 skipped`，唯一跳过项为真实百炼 smoke
- 2026-09-14：依赖锁、环境一致性、源码与测试编译、源码包和 wheel 构建、
  OpenAPI 契约及差异检查均通过；独立代码复审无 Critical、Important 或功能性
  Minor 问题，结论为 Ready；未跟踪 `.idea/` 继续明确排除在提交外
- 2026-09-14：负责人确认验收通过
- Result：S2-07 已验收并更新为 `VERIFIED`；允许开始 S2-08

---

## S2-08 Session 删除

**Status:** VERIFIED

**Dependencies:** S2-07

### Work

```http
DELETE /api/v1/chat/sessions/{session_id}
```

执行：

```text
stop and wait
→ delete child checkpoints
→ delete parent checkpoints
→ delete feedback
→ delete session last
```

### Acceptance

- [x] 删除 active Session 时先完成停止和 Run 清理
- [x] Parent、两个 Child、Feedback 与 Session 均被删除
- [x] Session 在所有关联数据之后删除
- [x] 中途失败后可以使用同一 session_id 重试
- [x] 不存在或不属于固定用户的目标幂等返回 204

### Execution Evidence

- 2026-09-14：新增 `DELETE /api/v1/chat/sessions/{session_id}`；成功、重复删除、
  目标不存在或不属于固定本地用户时均返回空响应 HTTP 204，非法 UUID 在进入
  服务前返回 422，路径参数及 OpenAPI 成功响应使用明确中文说明
- 2026-09-14：删除入口与单 Session 单 Run Registry 建立串行删除临界区；删除
  期间拒绝新 Run，活动 Run 先按 stopped 终态完成持久化、SSE 生产端结束和
  Registry 清理，真实删除成功后阻止已通过前置校验的迟到请求重新占用 Session
- 2026-09-14：Feedback 写入与删除共用同一 Session 操作协调器；删除先封闭迟到
  写入，再等待已通过目标校验且已进入临界区的写操作提交，随后清理反馈，确定性
  并发测试与真实 PostgreSQL 交错测试均确认删除完成后不会产生孤儿反馈
- 2026-09-14：持久化删除严格执行 `general_chat Child → en_to_zh Child → Parent
  → Feedback → Session`；Session 行作为最后删除的重试锚点，任一步失败统一返回
  可重试 `SESSION_DELETE_FAILED`，重复使用相同 `session_id` 会从头执行幂等清理
- 2026-09-14：最终持久化删除使用独立受保护任务；请求取消时仍等待删除得到明确
  结果，成功后先发布 tombstone 再传播取消，避免 Session 已提交删除但迟到 Run
  重新占用；active Run 停止失败也统一映射为可重试删除错误
- 2026-09-14：真实 PostgreSQL 集成测试预置 Parent、两个 Child checkpoint 和
  Feedback，并在 active Run 输出部分内容时调用删除接口；验证 stopped 清理先
  完成，三类 checkpoint 表、反馈表和 Session 表均无残留，连续第二次删除仍为 204
- 2026-09-14：S2-08 定向测试 `91 passed`；默认全量回归
  `269 passed, 13 skipped`；启用真实 PostgreSQL 后全量回归
  `279 passed, 1 skipped`，唯一跳过项为真实百炼 smoke
- 2026-09-14：依赖锁、环境一致性、源码与测试编译、源码包和 wheel 构建及差异
  检查均通过；两轮独立复审发现的 Feedback/DELETE 竞态、取消窗口和停止错误契约
  均已修复，最终结论为 Ready；未跟踪 `.idea/` 继续明确排除在提交外
- 2026-09-14：负责人确认验收通过
- Result：S2-08 已验收并更新为 `VERIFIED`；允许开始 S2-09

---

## S2-09 完整聊天演示页面

**Status:** VERIFIED

**Dependencies:** S2-08

### Work

- 创建 React + Vite + Ant Design / Ant Design X 前端
- 实现双栏聊天页面和可折叠运行状态面板
- 接入所有 Stage 2 API 与 POST SSE
- 构建独立 HTML、JS、CSS 静态资源并由 FastAPI 同源提供

### Acceptance

- [x] 页面支持会话新建、分页、选择、改名和二次确认删除
- [x] 页面支持流式聊天、Stop、Regenerate 和 Feedback
- [x] 运行面板显示 session_id、message_id、capability_id 与终态
- [x] Markdown 安全渲染，原始 HTML 不直接执行
- [x] 通用 UI 使用第三方开源组件，不自研组件库
- [x] HTML、JavaScript/JSX、API 模块和 CSS 源码分离
- [x] 构建产物包含独立且带内容哈希的 JS / CSS
- [x] `/chat` 和 `/assets/*` 可由 FastAPI 直接访问
- [x] 生产演示不要求安装 Node

### Execution Evidence

- 2026-09-15：新增 React 19 + Vite 8 + Ant Design / Ant Design X 双栏聊天页面，
  完成 Session 游标分页、选择、新建、改名、二次确认删除，以及流式聊天、Stop、
  Regenerate、like、dislike、cancel 和可折叠运行状态面板
- 2026-09-15：前端仅消费 Stage 2 产品 DTO 与 `message/error/done` POST SSE；
  正常终态及异常断流后均回读服务端活动分支，Session 和消息请求均具备代次保护，
  避免旧响应覆盖权威状态
- 2026-09-15：使用 `react-markdown + remark-gfm + rehype-highlight` 安全渲染
  Markdown 与代码高亮；原始 HTML 不执行，危险协议被过滤，外链增加隔离属性
- 2026-09-15：Vite 生产构建输出独立 HTML 与带内容哈希的 JS / CSS，并提交到
  `src/agent_runtime/static/`；FastAPI 同源提供 `/chat` 与 `/assets/*`，构建后的
  Python 生产演示不依赖 Node.js
- 2026-09-15：Vitest + React Testing Library 共 20 项测试通过；默认 pytest 共
  271 项通过、13 项按配置跳过；Vite 构建、依赖锁检查、环境一致性检查、源码编译、
  sdist/wheel 构建与 wheel 静态资源检查均通过
- 2026-09-15：两轮独立审查发现的代码高亮缺失、异常断流权威状态回读、消息与
  Session 请求竞态及运行期删除竞态均已修复；最终复核无 Critical、Important 或
  Minor 问题，结论为 Ready
- 2026-09-15：负责人确认验收通过
- Result：S2-09 已验收并更新为 `VERIFIED`；允许开始 S2-10

---

## S2-10 Stage 2 集成验收

**Status:** VERIFIED

**Dependencies:** S2-01 ~ S2-09

### Acceptance

- [x] 默认 `uv run pytest -q` 使用 Fake Model 并全部通过
- [x] 前端 Vitest + React Testing Library 全部通过
- [x] `npm run build` 成功且静态资源边界检查通过
- [x] 可选 PostgreSQL 后端集成测试通过
- [x] 可选 Playwright 真实浏览器端到端验收通过
- [x] 普通聊天、英译汉和有限 OUT_OF_SCOPE 回流未回归
- [x] 10/5 上下文规则继续排除 unsupported / incomplete / stopped
- [x] 未实现 Stage 3 或 Future 能力
- [x] README 和全部项目文档与实现一致
- [x] 关键业务入口、出口、拒绝、取消和失败写入按日中文日志，敏感正文与凭据不落盘

### Execution Evidence

- 2026-09-15：新增显式运行的 Playwright 1.63 浏览器验收；真实启动 FastAPI
  生产构建、PostgreSQL 和确定性 Fake Model，每次运行使用独立端口和唯一测试
  用户隔离数据，并在启动前、用例结束及正常关闭时执行幂等清理
- 2026-09-15：真实 Edge 浏览器完成普通聊天、like、Regenerate、英译汉有限回流、
  Session 改名、新建后重新选择、双 Capability 均拒绝后的 unsupported、二次确认
  删除，以及部分输出期间 Stop 并收到 stopped 终态；`1 passed`
- 2026-09-15：浏览器验收发现 S2-09 自定义 vendor 分组破坏 React / Ant Design
  初始化顺序并导致 `/chat` 白屏；移除该分组后生产页面真实挂载，构建继续输出
  独立且带内容哈希的 HTML、JavaScript 和 CSS，FastAPI 静态资源边界测试
  `2 passed`
- 2026-09-15：Vitest 收集范围限定到 `src/**/*.test.{js,jsx}`，与 Playwright
  用例互不混入；从 lockfile 执行 `npm ci` 后，Vitest + React Testing Library
  `20 passed`，`npm run build` 成功
- 2026-09-15：默认 `uv run pytest -q` 使用 Fake Model，结果为
  `271 passed, 13 skipped`；其中 12 项为显式 PostgreSQL 测试，1 项为真实百炼
  smoke，不读取真实模型密钥或访问模型网络
- 2026-09-15：初始化并连接 `agent_runtime` PostgreSQL 测试数据库后，显式启用
  `RUN_POSTGRES_TESTS=1` 的完整后端验收为 `283 passed, 1 skipped`，唯一跳过项为
  真实百炼 smoke
- 2026-09-15：Stage 1 主链路、上下文及静态资源定向回归为
  `14 passed, 1 skipped`；普通聊天、英译汉、有限 OUT_OF_SCOPE 回流与 10/5
  上下文排除 unsupported / incomplete / stopped 均未回归
- 2026-09-15：`uv lock --check`、`uv pip check`、源码与测试编译、sdist/wheel
  构建、wheel 中哈希 JS/CSS 资源检查、`git diff --check` 与 Stage 3 / Future
  范围审计均通过
- 2026-09-15：独立审查发现固定 Playwright 端口和测试用户无法隔离多进程；改为
  运行级端口、user_id 与 afterEach API 清理，并固定主进程与 worker 的配置继承；
  两份真实 Edge 验收使用自动独立端口并发执行，结果均为 `1 passed`，结束后无
  服务进程且专用测试 Session 残留为 0
- 2026-09-15：独立终审确认无剩余 Critical、Important 或 Minor 问题，结论为
  Ready
- 2026-09-16：本地 PostgreSQL 从便携版迁移到 Windows Docker Desktop；项目根
  目录新增最小 `compose.yaml`，仅部署 `postgres:17`，端口只绑定
  `127.0.0.1:5432`，数据使用固定命名卷 `agent-runtime-postgres` 持久化
- 2026-09-16：负责人明确允许丢弃迁移前历史数据；旧便携版 PostgreSQL 目录和旧
  Docker 测试卷已删除，新容器初始化 `agent_runtime` 数据库及 `runtime` 用户，
  健康检查通过
- 2026-09-16：命名卷写入探针数据后重启容器仍可读取，随后已清理探针表；后端
  自动创建 Session、Feedback 和 Checkpoint 共 6 张表，`/health` 与 `/chat`
  均返回 200
- 2026-09-16：显式启用 `RUN_POSTGRES_TESTS=1` 的完整后端验收为
  `283 passed, 1 skipped`，唯一跳过项为真实百炼 smoke；Compose 配置解析、
  数据库连通性和 PostgreSQL 17 运行版本均已验证
- 2026-09-21：补齐贯穿项目的业务日志规范与实现；应用生命周期、HTTP、聊天运行、
  Session、Active Run、Parent、Router、普通聊天和英译汉 Capability 均记录中文
  结构化入口、出口及异常事件，并通过统一入口脱敏正文、Prompt、凭据和数据库连接串
- 2026-09-21：日志模块覆盖 UTF-8 按日日切、重复配置去重、并发写入、生命周期失败、
  Capability 异常关联和常见敏感字段别名；默认 Fake Model 全量验收为
  `280 passed, 13 skipped`，日志相关与关键业务路径定向回归为 `104 passed`
- 2026-09-21：实际生成 `logs/agent-runtime-2026-09-21.log`，事件包含关联 ID、状态、
  错误码和耗时；密钥、数据库凭据与对话正文样例扫描均为 0 命中
- 2026-09-21：`uv lock --check`、`uv pip check`、源码与测试编译以及
  `git diff --check` 均通过；独立复审确认无剩余 Critical 或 Important 问题，结论为
  Ready
- 2026-09-21：本次尝试复跑可选 PostgreSQL 集成测试时 Docker Desktop Engine 未
  完成就绪，端口 5432 未监听，故未将该次运行计为通过；2026-09-16 的数据库完整
  验收证据保持有效，本次日志改动未修改持久化结构或数据库访问路径
- 2026-09-22：最新复核发现 Session 删除 PostgreSQL 集成测试仅通过一次
  `await asyncio.sleep(0)` 推断 DELETE 已进入 Registry 删除临界区，存在调度竞态；
  复现结果为 `1 failed, 291 passed, 1 skipped`，该次结果不计为验收通过
- 2026-09-22：将测试编排改为由包裹真实删除服务的 `asyncio.Event` 发出确定性信号；
  信号只会在 DELETE 已进入 Registry 临界区、且持久化清理尚未放行时触发。测试通过
  真实 `ChatService.submit_feedback()` 验证新反馈被拒绝，随后继续执行 Feedback、
  Checkpoint 与 Session 的真实 PostgreSQL 清理断言，不增加固定 sleep 或生产测试钩子
- 2026-09-22：按复核标准执行 `RUN_POSTGRES_TESTS=1 uv run --locked pytest -q`，
  完整后端套件结果为 `292 passed, 1 skipped`，退出码为 0；唯一跳过项仍为需显式启用
  的真实百炼 smoke，S2-10 PostgreSQL 验收门禁恢复通过
- 2026-09-23：负责人确认 Stage 2 验收通过
- Result：Stage 2 已更新为 `VERIFIED`；允许进入已重新设计的 Stage 2.5，原
  S3-01 不再是下一任务

---

# Stage 2.5：持久化 Run 与可恢复 Runtime

> 本节保留 Stage 2.5 当时的执行记录。Stage 2.5 已 VERIFIED，后续范围已经重新
> 裁决为本文 Stage 3；历史状态描述不代表当前任务。

## S2.5-00 Stage 2.5 架构基线确认

**Status:** VERIFIED

**Dependencies:** S2-10

### Work

- 审核负责人提供的 S2.5 草案与 Stage 2 当前实现
- 识别持久输入、事件顺序、Redis/PG 一致性、Checkpoint 对账、Resume 幂等、
  API 迁移、事件隐私、Session 删除和慢客户端等缺口
- 逐项完成负责人裁决
- 维护既有 requirements / architecture / decisions / tasks / README / AGENTS
- 移除原 Stage 3 的正式阶段承诺，将仍有价值的内容降为 Future 候选

### Acceptance

- [x] Stage 2 标记为 VERIFIED
- [x] S2.5 的需求、架构、决策和任务边界一致
- [x] Parent 公共消息权威源和 Parent / Child 状态隔离保持不变
- [x] 持久 Run、RuntimeEvent、Redis、SSE、Cancel、Interrupt 和恢复边界明确
- [x] 页面迁移和真实链路演示模式纳入阶段范围
- [x] 原 Stage 3 不再作为已承诺的下一阶段
- [x] 未编写或修改业务代码

### Verification

- 2026-09-23：负责人逐项确认 S2.5 边界与修正，包括持久恢复输入、
  Sequencer、公开事件协议、Checkpoint-first 对账、断线不取消、单 pending
  Interrupt、非空终态消息和开发演示模式
- 2026-09-23：文档维护完成后等待负责人进行 S2.5 架构文档验收；验收前不得开始
  S2.5-01
- 2026-09-24：负责人明确指示开始 Stage 2.5 编码阶段，S2.5 架构文档验收通过
- Result：S2.5-00 已更新为 `VERIFIED`；下一允许任务为 `S2.5-01`

---

## S2.5-01 Run 持久化与幂等

**Status:** VERIFIED

**Dependencies:** S2.5-00 VERIFIED

### Work

- 增加 Run 数据模型、迁移、Repository 和状态转换
- 实现全局 `request_id`、请求指纹和稳定消息 ID
- 实现活动 Run 的 Session 部分唯一约束
- 实现最小恢复输入的活动期保留和终态清除
- 只支持 `normal`、`regenerate`

### Acceptance

- [x] 相同 request_id + 相同请求返回原 Run
- [x] 相同 request_id + 不同请求返回 409
- [x] 数据库拒绝同一 Session 的第二个活动 Run
- [x] 终态不可修改，输入正文已清除
- [x] Stage 2 历史不被强制补建 Run

### Verification

- 2026-09-24：按 TDD 完成 Run 领域模型、规范化请求指纹、PostgreSQL Schema、
  幂等创建和状态转换；应用启动会在 Session 表之后初始化 Run 表及活动 Run 部分
  唯一索引
- 2026-09-24：真实 PostgreSQL 并发测试验证相同 `request_id` 只创建一个 Run，
  重复请求返回数据库中原始稳定消息 ID，不同指纹返回 409，同一 Session 的第二个
  活动 Run 由数据库约束拒绝
- 2026-09-24：真实 PostgreSQL 状态转换测试验证终态不可覆盖、终态清除
  `input_payload`；Schema 初始化测试验证 Stage 2 历史 Session 不补建 Run
- 2026-09-24：补齐 Run 创建、读取、拒绝、转换和失败链路的中文业务日志；日志包含
  可用关联 ID、状态、稳定错误码、错误类型和耗时，不记录正文、恢复输入、请求指纹
  或原始异常详情
- 2026-09-24：`RUN_POSTGRES_TESTS=1 uv run --locked pytest -q` 结果为
  `330 passed, 1 skipped`；唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-24：默认 `uv run --locked pytest -q` 结果为
  `316 passed, 15 skipped`；PostgreSQL 集成项按约定默认跳过
- 2026-09-24：`git diff --check`、`uv lock --check`、Python `compileall`、
  `uv pip check` 和 `uv build` 通过；独立代码审查无 Critical / Important 问题
- 2026-09-24：负责人确认 S2.5-01 验收通过
- Result：S2.5-01 已更新为 `VERIFIED`；允许开始 S2.5-02

---

## S2.5-02 RuntimeEvent 与 Run Sequencer

**Status:** VERIFIED

**Review Status:** ACCEPTED

**Dependencies:** S2.5-01

### Work

- 增加 RuntimeEvent 表、类型化 Schema 和 public/internal 可见性
- 实现 UUIDv4 event ID、序号块预留和 Run Sequencer
- 实现 Run 状态与 durable event 同事务
- 实现公开事件白名单与敏感字段拒绝
- 实现 `message.started(attempt)` 和唯一 Run 终态事件

### Acceptance

- [x] 并发发射不产生重复或倒退 seq
- [x] 序号块缺号不会被误判为错误
- [x] internal 事件不能通过公开 SSE Schema
- [x] 任一 Run 最多一个终态事件
- [x] Prompt、Checkpoint、正文请求和工具原始数据不会进入公开 payload

### Verification

- 2026-09-24：增加固定公开 RuntimeEvent Schema、public/internal 可见性、
  UUIDv4 event ID、PostgreSQL durable event 表及公开投影白名单；公开投影不包含
  `source`、`visibility`、`durability`，未引入独立 `trace_id`
- 2026-09-24：Run Sequencer 使用 PostgreSQL 高水位分段预留 `seq`；同一进程同一
  Run 通过弱引用注册表只保留一个存活 Sequencer，实例释放后的恢复会租用新序号块，
  允许缺号但不会重用旧序号
- 2026-09-24：`message.started` 在 Run 行锁事务内校验固定
  `response_message_id` 和从 1 开始严格递增的 `attempt`；`message.delta` 只分配
  序号且不写 PostgreSQL
- 2026-09-24：Run 状态变化与对应 durable event 在同一 PostgreSQL 事务提交；
  事件写入失败会回滚状态，数据库部分唯一索引和状态机共同保证每个 Run 最多一个
  终态事件
- 2026-09-24：公开 Schema 全部禁止额外字段并提供中文参数说明；Prompt、
  Checkpoint、State、正文请求、节点标识及工具原始数据无法进入固定公开 payload
- 2026-09-24：Schema、Sequencer 与 PostgreSQL 事件专项测试通过；默认
  `uv run --locked pytest -q` 结果为 `334 passed, 17 skipped`
- 2026-09-24：`RUN_POSTGRES_TESTS=1 uv run --locked pytest -q` 结果为
  `350 passed, 1 skipped`；唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-24：`git diff --check`、`uv lock --check`、Python `compileall`、
  `uv pip check` 和 `uv build` 通过
- 2026-09-24：独立二次代码审查确认无 Critical / Important 问题；未实现 Redis、
  SSE Gateway、异步 Run API 或 Coordinator，未跨入 S2.5-03
- 2026-09-24：修复 `REV-S25-02-001`；状态事件现在必须在持有 Run 行锁时通过
  `current_status + event_type + target_status` 三元组校验，非法组合在更新 Run 和
  插入 durable Event 之前被拒绝
- 2026-09-24：当前阶段只允许 `queued + run.started → running`、
  `running + run.completed → completed` 和 `running + run.failed → failed`；
  `interrupt.resumed`、Cancel 和 Recovery 状态事件转换继续禁止，未提前接入后续阶段
- 2026-09-24：新增 PostgreSQL 负向测试，验证 queued Run 不能通过
  `interrupt.resumed` 进入 running、非法组合不写事件且 Run 状态不变、重复
  `running + run.started → running` 被拒绝、合法启动转换仍正常
- 2026-09-24：修复后默认 `uv run --locked pytest -q` 结果为
  `334 passed, 18 skipped`；`RUN_POSTGRES_TESTS=1 uv run --locked pytest -q`
  结果为 `351 passed, 1 skipped`
- Result：S2.5-02 保持 `DONE`，`REV-S25-02-001` 已修复并进入待复审状态；
  未标记 `VERIFIED`，复审前不进入 S2.5-03
- 2026-09-26：负责人确认 S2.5-02 验收通过，任务状态更新为 `VERIFIED`，允许开始
  S2.5-03；历史待复审记录保留作为审核轨迹

---

## S2.5-03 Redis Stream 实时传输

**Status:** VERIFIED

**Dependencies:** S2.5-02

### Work

- 在根 `compose.yaml` 增加本地 Redis 服务
- 使用 `runtime:events:{run_id}` 和 `{seq}-0`
- 每次写入刷新 30 分钟 TTL
- 实现 Redis 可用性降级与运行期断线处理
- Redis 不使用持久化命名卷

### Acceptance

- [x] Redis 正常时公开实时事件按 seq 到达
- [x] Redis 启动失败不阻止 Run 执行
- [x] Redis 运行中断不丢失 PostgreSQL durable event
- [x] Redis 恢复后不伪造缺失 delta
- [x] TTL 行为和本地绑定通过集成验证

### Verification

- 2026-09-26：增加 Redis 8 Compose 服务，只绑定 `127.0.0.1`，显式关闭 RDB/AOF，
  未配置 Redis 持久卷；`docker compose config` 静态解析通过
- 2026-09-26：实现 `RedisStreamPublisher`，使用 `runtime:events:{run_id}`、
  `{seq}-0` 和 Redis 事务内 `XADD + EXPIRE 1800`；只序列化公开白名单投影，
  Redis 异常返回降级结果且不记录正文、delta、连接地址或凭据
- 2026-09-26：Run Sequencer 已接入尽力发布；durable 事件只在 PostgreSQL 提交
  成功后发布，transient delta 不写 PostgreSQL；不缓存、不重试且不补建故障期间
  丢失的 delta
- 2026-09-26：新增单元、Compose 静态及 Redis/PostgreSQL 组合集成测试；默认
  `uv run --locked pytest -q` 结果为 `345 passed, 20 skipped`
- 2026-09-26：`uv lock --check`、Python `compileall`、`uv pip check`、`uv build`
  和 `git diff --check` 通过
- 2026-09-26：真实 Redis/PostgreSQL 组合验收尚未执行；Docker Desktop 后端因其
  自身运行时套接字错误崩溃，S2.5-03 在恢复真实容器环境前不标记 `DONE`
- 2026-09-26：按 Docker Desktop Windows 已知 AF_UNIX socket 故障的非破坏方式
  移开临时运行目录，未执行 Factory Reset，`agent-runtime-postgres` 命名卷与历史
  容器数据保持不变；PostgreSQL 与 Redis Compose 服务均达到 `healthy`
- 2026-09-26：`RUN_POSTGRES_TESTS=1 RUN_REDIS_TESTS=1` 下执行 Redis/PG+Redis
  专项测试，结果为 `2 passed`；真实验证固定 Stream ID、30 分钟 TTL、同 Publisher
  中断恢复、不补建 delta，以及 Redis 故障不回滚 PostgreSQL durable event
- 2026-09-26：`RUN_POSTGRES_TESTS=1 RUN_REDIS_TESTS=1 uv run --locked pytest -q`
  完整套件结果为 `364 passed, 1 skipped`；唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-26：独立复核确认代码与测试设计层面无剩余 Critical / Important 问题；
  `REV-S25-03-001` 的真实运行证据已经补齐
- Result：S2.5-03 实现和真实外部依赖门禁完成，更新为 `DONE` 并等待负责人复审；
  复审通过前不进入 S2.5-04，不自行标记 `VERIFIED`
- 2026-09-26：负责人确认 S2.5-03 验收通过，任务状态更新为 `VERIFIED`；
  累计实现基线已提交为 `1f8a132`，允许开始 S2.5-04

---

## S2.5-04 异步 Run API 与单进程 Coordinator

**Status:** VERIFIED

**Dependencies:** S2.5-01 ~ S2.5-03

### Work

- 普通消息与 Regenerate 改为提交 Run 并返回 HTTP 202
- 增加 Session 当前活动 Run 查询
- 实现提交后唤醒、queued 补偿扫描和启动恢复扫描
- 移除公开 Session Stop 的当前产品职责
- 保持固定 user_id 和 Session 所有权校验

### Acceptance

- [x] 202 响应包含 run_id、session_id、response_message_id 和 status
- [x] API 返回前 Run 与幂等数据已经持久化
- [x] 丢失进程内唤醒后 queued Run 会被补偿执行
- [x] 页面刷新能够查询活动 Run
- [x] 未引入多实例 Lease 或分布式队列

### Verification

- 2026-09-27：普通消息与 Regenerate 已迁移为持久 Run 提交接口；成功时返回
  HTTP 202 和公开 Run 摘要，`request_id` 必填且全局幂等，冲突内容复用同一
  ID 返回 409；公开 Session Stop 路由已移除
- 2026-09-27：新 Session、稳定消息 ID、请求指纹、最小恢复输入与 queued Run
  在同一 PostgreSQL 事务提交；重复新 Session 请求复用原 Run 且不留下孤儿
  Session；Regenerate 在 Run 落库后才创建 Parent fork
- 2026-09-27：增加固定用户 Session 活动 Run 查询和单进程 `RunCoordinator`；
  已验证提交后唤醒、同进程去重、启动扫描及 queued 周期补偿，running 等恢复
  对账明确留给 S2.5-08，未引入 Lease、分布式队列或多实例语义
- 2026-09-27：真实 PostgreSQL / Redis Compose 服务均为 `healthy`；
  `RUN_POSTGRES_TESTS=1 RUN_REDIS_TESTS=1 uv run --locked pytest -q`
  完整套件结果为 `357 passed, 1 skipped`，唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-27：`docker compose config --quiet`、`uv lock --check`、Python
  `compileall`、`uv pip check`、`uv build` 和 `git diff --check` 全部通过
- 2026-09-27：独立代码审查未发现 Critical / Important 问题；专项复核结果为
  `28 passed`，真实 PostgreSQL 异步 Run 端到端用例连续运行三次均通过
- Result：S2.5-04 实现与真实外部依赖门禁完成，状态更新为 `DONE` 并等待负责人
  复审；复审通过前不进入 S2.5-05，不自行标记 `VERIFIED`
- 2026-09-27：Reviewer 提出阻塞项 `REV-S25-04-002`：Regenerate Run 未记录
  可定位的来源 Run，`parent_run_id` 被固定保存为 `NULL`
- 2026-09-27：增加按当前 Session 与来源 `response_message_id` 查询 Run 的可空
  Repository 接口；S2.5 来源回复写入真实 `parent_run_id`，Stage 2 历史回复找不到
  来源 Run 时继续保存 `NULL`，未补建历史 Run 或新增 Run 类型
- 2026-09-27：真实 PostgreSQL 回归测试覆盖来源关联、Stage 2 历史兼容和相同
  `request_id` 幂等重试；专项结果为 `2 passed`
- 2026-09-27：默认 `uv run --locked pytest -q` 结果为
  `340 passed, 19 skipped`；`RUN_POSTGRES_TESTS=1 uv run --locked pytest -q`
  结果为 `356 passed, 3 skipped`
- 2026-09-27：同时启用真实 PostgreSQL / Redis 的完整门禁结果为
  `358 passed, 1 skipped`；`docker compose config --quiet`、`uv lock --check`、
  Python `compileall`、`uv pip check`、`uv build` 和 `git diff --check` 通过
- 2026-09-27：`REV-S25-04-002` 来源关联逻辑专项复核结果为 `2 passed`；该次
  复核未确定性覆盖幂等重试时 `queued → running` 的状态推进，后续结论由
  `REV-S25-04-004` 的竞态复核记录取代
- Result：`REV-S25-04-002` 已修复，S2.5-04 保持 `DONE` 并等待 Reviewer
  复核；不自行标记 `VERIFIED`，不开始 S2.5-05
- 2026-09-27：Reviewer 提出阻塞项 `REV-S25-04-004`：普通 Run 的幂等集成测试
  错误要求首次 `queued` 响应与重试时的可变权威状态全量相等，导致 Coordinator
  已推进到 `running` 时产生时序竞态
- 2026-09-27：测试改为确定性等待 Run 进入 `running` 后重试，只比较 `run_id`、
  `session_id` 和 `response_message_id` 三个稳定字段，并验证 PostgreSQL 中只有一个
  Run、一个 Session 且模型只执行一次；保留合法的当前 `status` 推进
- 2026-09-27：真实 PostgreSQL 异步 Run 专项连续五轮均为 `2 passed`；原
  Regenerate 回归为 `3 passed`；默认完整套件为 `340 passed, 19 skipped`，真实
  PostgreSQL / Redis 完整套件为 `358 passed, 1 skipped`
- Result：`REV-S25-04-004` 已修复，S2.5-04 保持 `DONE` 并等待 Reviewer
  复核；不自行标记 `VERIFIED`，不开始 S2.5-05
- 2026-09-27：负责人确认 S2.5-04 验收通过；累计实现已提交为 `8515051`，
  并同步到 GitHub 与 Gitee，允许开始 S2.5-05

---

## S2.5-05 SSE Gateway、合并与背压

**Status:** VERIFIED

**Dependencies:** S2.5-03, S2.5-04

### Work

- 增加 Run events GET SSE
- 合并 PostgreSQL durable event 与 Redis Stream
- 支持 Last-Event-ID 和 after_seq
- 实现排序、去重、有限批次、写入超时和 20 秒心跳
- 断线只关闭 Gateway，不取消 Executor

### Acceptance

- [x] 重连不会重复或倒序发送事件
- [x] Last-Event-ID 优先于 after_seq
- [x] 合法缺号不会阻塞事件流
- [x] Redis delta 过期后仍能得到持久终态
- [x] 慢客户端不会建立无界内存队列
- [x] SSE 断开后 Run 继续完成

### Verification Notes

- 2026-09-27：新增 `GET /api/v1/chat/runs/{run_id}/events`；服务层先校验
  Run 所属 Session 的固定本地用户归属，再建立独立 SSE Gateway
- 2026-09-27：Gateway 以有限批次同时读取 PostgreSQL durable public event 与
  Redis Stream，按 `seq` 排序去重；`Last-Event-ID` 优先于 `after_seq`，SSE
  `id` 固定等于事件 `seq`，合法缺号不会阻塞后续事件
- 2026-09-27：增加终态提交交错测试；Gateway 观察到终态后会在关闭前重查
  PostgreSQL，避免首次事件查询早于终态事务提交时漏发终态事件
- 2026-09-27：20 秒心跳使用无 `id` 的注释帧；事件按需迭代且不建立额外队列，
  响应头、事件帧和结束帧写入均受超时保护；断开或慢连接只关闭 SSE，不传播取消
  到独立 Executor
- 2026-09-27：真实 PostgreSQL + Redis 组合测试验证初次合并为 seq
  `1,2,3,4,5`、从 seq 2 重连为 `3,4,5`；删除 Redis Stream 模拟 delta 过期后，
  仍从 PostgreSQL 得到 seq `1,2,4,5` 和 `run.completed`
- 2026-09-27：默认 `uv run --locked pytest -q` 结果为
  `364 passed, 20 skipped`；同时启用真实 PostgreSQL / Redis 的完整套件结果为
  `383 passed, 1 skipped`，唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-27：`uv lock --check`、Python `compileall`、`uv pip check`、`uv build`
  和 `git diff --check` 全部通过
- 2026-09-27：独立审查发现 `REV-S25-05-001`：函数式 HTTP middleware 会把
  自定义 SSE Response 包装到内存通道后再写网络，使原超时未覆盖真实 ASGI send；
  已改为无缓冲的纯 ASGI 日志 middleware，并增加完整 `create_app` 慢网络写入测试，
  确认超时关闭真实连接且不取消 Executor
- 2026-09-27：`REV-S25-05-001` 专项复审通过；该次复审范围只覆盖真实 ASGI
  写入超时，后续跨存储合并结论由 `REV-S25-05-002` 记录取代
- 2026-09-27：Reviewer 提出阻塞项 `REV-S25-05-002`：先查询 PostgreSQL、再读取
  较高序号 Redis event 会使用高序号推进游标，从而永久越过两次读取之间刚提交的
  较低序号 durable event
- 2026-09-27：增加确定性跨存储交错测试，修复前稳定输出 seq `3,4` 并漏掉
  durable seq `2`；Gateway 改为先读取 Redis，再以 PostgreSQL 查询收口本轮快照，
  修复后严格输出 seq `2,3,4`；该单向读取顺序修复随后被
  `REV-S25-05-004` 的反向竞态结论取代，不作为最终方案
- 2026-09-27：修复 `REV-S25-05-003`，README 两处过期的 S2.5-04 进度和
  “独立 GET SSE 尚未实现”描述均已更新为 S2.5-05 实际状态
- 2026-09-27：修复后 S2.5-05 专项结果为 `82 passed, 1 skipped`，真实
  PostgreSQL + Redis Gateway/Stream 专项为 `10 passed`
- 2026-09-27：Reviewer 提出阻塞项 `REV-S25-05-004`：固定 Redis → PostgreSQL
  顺序仍可能先读空 Redis，随后从 PostgreSQL 看到较高 durable 前沿，进而反向
  越过两次读取之间已发布的较低 transient event
- 2026-09-27：最终合并采用有界 Redis → PostgreSQL → 条件性 Redis 收口；仅当
  durable 前沿高于首次 Redis 前沿时再读取一次 Redis，且只接纳不高于 durable
  前沿的事件，更高事件留待下一有限批次，不引入无限稳定化或无界预取
- 2026-09-28：两个确定性竞态测试均保留并单独复跑通过：较高 Redis seq 不会越过
  较低 durable event，较高 PostgreSQL 前沿也不会反向越过尚未读取的 Redis
  transient event；结果为 `2 passed`
- 2026-09-28：S2.5-05 专项结果为 `83 passed, 1 skipped`；默认完整套件结果为
  `365 passed, 20 skipped`；启用真实 PostgreSQL / Redis 的完整套件结果为
  `384 passed, 1 skipped`，唯一跳过项为需显式启用的真实百炼 smoke
- 2026-09-28：真实 PostgreSQL + Redis Gateway/Stream 专项结果为 `11 passed`；
  `uv lock --check`、`uv pip check`、Python `compileall`、`uv build` 和
  `git diff --check` 全部通过
- Result：S2.5-05 实现与真实外部依赖门禁完成，状态更新为 `DONE` 并等待负责人
  复审；不自行标记 `VERIFIED`，不开始 S2.5-06
- 2026-09-28：负责人确认 S2.5-05 验收通过；实现已提交为 `a4d1b98`，并确认
  GitHub 与 Gitee 的 `master` 均指向同一提交，允许开始 S2.5-06

---

## S2.5-06 Cancel、非空终态与 Session 删除

**Status:** VERIFIED

**Dependencies:** S2.5-04, S2.5-05

### Work

- 增加 Run Cancel API 和协作式/超时强制取消
- 实现完成与取消的首终态竞争
- 统一 completed / unsupported / incomplete / stopped 公共消息落盘
- 保证所有终态消息非空并使用预分配 response_message_id
- 扩展 Session 删除到 Run、Event、Interrupt 和 Redis

### Acceptance

- [x] Cancel 持久化后返回 202 且重复调用幂等
- [x] Cancel 不回滚已有外部副作用
- [x] 失败或取消在零 delta 时仍形成非空可解释消息
- [x] 只有 completed 完整轮次进入模型上下文
- [x] Session 最后删除且中途失败可重试
- [x] Redis 删除失败不阻塞 PostgreSQL 硬删除

### Verification

- 2026-09-28：新增 `POST /api/v1/chat/runs/{run_id}/cancel`；运行中 Run 在行锁事务
  内幂等写入 `cancel_requested` 后返回 202，queued / interrupted Run 直接形成
  `stopped` 公共消息并提交 `run.cancelled`
- 2026-09-28：Coordinator 增加进程内协作取消信号和可配置宽限期；执行器响应信号
  后完成公共消息与事件投影，未响应时仅强制取消同一进程内任务；连接断开语义不变
- 2026-09-28：完成与取消竞争继续在 PostgreSQL Run 行锁下决胜；取消先写入时
  `run.completed` 被拒绝，完成或其他终态先提交时 Cancel 只返回不可变权威终态
- 2026-09-28：失败与取消统一使用预分配 `response_message_id` 保存非空
  `incomplete` / `stopped` 公共消息，`message.finalized` 先于 `run.failed` /
  `run.cancelled`；既有上下文测试继续证明仅 completed 完整轮次进入模型输入
- 2026-09-28：Session 删除屏障已覆盖持久 Run；等待活动 Run 终止后收集 run_id，
  尽力删除 Redis Stream，再删除 RuntimeEvent / Run、Checkpoint、Feedback，最后
  删除 Session；Redis 失败只记录降级且不阻塞 PostgreSQL
- 2026-09-28：针对独立复审发现的取消宽限期与终态竞争问题，Coordinator 在进入
  取消终态持久化后停止强制取消计时器，并等待终态收尾完成；完成、失败与取消使用
  同一 Run 进程内决胜锁，防止较晚 Cancel 插入 `message.finalized` 与 Run 终态之间
- 2026-09-28：失败与取消兜底消息在持久化前统一去除空白并保证非空；无法从
  `input_payload` 重建稳定 Parent 写入位置时不提交无公共消息的数据库终态，留待后续
  恢复；Run 决胜锁使用弱引用释放空闲条目
- 2026-09-28：新增确定性回归测试，覆盖慢取消收尾超过宽限期仍被等待、完成事件与
  Cancel 交错只产生一个 `message.finalized`、空白取消/失败消息兜底，以及无法重建
  Parent 写入位置时不错误提交终态
- 2026-09-28：独立复审发现等待者取消可反向中断共享 Executor、queued / interrupted
  直接取消会被调用方取消中断；Coordinator 等待改为 shield 共享任务，直接取消延迟
  传播调用方取消，并新增等待者取消与关闭期间终态收尾的确定性回归测试
- 2026-09-28：`running / recovering → cancel_requested` 改由
  `internal.run.cancel_requested` durable Event 与 Run 状态在同一 PostgreSQL 事务
  提交；内部事件不进入公开 SSE，重复 Cancel 继续返回当前权威状态
- 2026-09-28：补齐 Cancel 终态幂等出口及 Session 持久化关联数据删除开始、完成、
  失败中文业务日志，日志不记录请求正文、输出正文或连接凭据
- 2026-09-28：S2.5-06 默认完整套件为 `376 passed, 26 skipped`；启用真实
  PostgreSQL / Redis 的完整套件为 `401 passed, 1 skipped`，唯一跳过项为需显式
  启用的真实百炼 smoke
- 2026-09-29：负责人确认 S2.5-06 验收通过；允许按工程流程提交并同步 GitHub、
  Gitee，两个远程确认一致后进入 S2.5-07
- 2026-09-29：S2.5-06 实现、测试与验收记录已提交为 `5d70a95`
- Result：S2.5-06 已通过实现、真实依赖门禁、独立复审和负责人验收，状态更新为
  `VERIFIED`

---

## S2.5-07 Interrupt / Resume

**Status:** VERIFIED

**Dependencies:** S2.5-02, S2.5-04, S2.5-05

### Work

- 增加最小 `run_interrupts` 表和 pending 唯一约束
- 实现 interrupt.required / interrupt.resumed
- 实现同 Run Resume API、行锁与恢复请求幂等
- Interrupt 后关闭 SSE，但保持 Run 和 Session 占用
- Cancel 时关闭待处理中断

### Acceptance

- [x] 每个 Run 同时最多一个 pending Interrupt
- [x] 同一 Interrupt 只成功 Resume 一次
- [x] 不同恢复内容复用请求 ID 返回 409
- [x] Resume 不新增 HumanMessage，也不创建新 Run
- [x] 页面可在刷新后恢复中断提示

### Verification Notes

- 2026-09-29：完成 `run_interrupts` 持久化、`interrupt.required` /
  `interrupt.resumed` 原子状态事件、同 Run Resume、恢复请求幂等、活动中断查询、
  SSE 中断关闭和 Cancel 关闭 pending Interrupt。
- 2026-09-29：修复 `REV-S25-07-001`。首次执行只在需要时使用 Run 的
  `start_checkpoint_id`，中断投影和 `Command(resume=...)` 均使用当前线程的最新
  Checkpoint；真实 LangGraph 测试覆盖已有 Session 的非空起点和 Regenerate。
- 2026-09-29：修复 `REV-S25-07-002`。相同 Resume 请求幂等重试也会安全重试
  本地 Coordinator 唤醒，避免数据库已转为 `running` 后因首次唤醒遗漏而停滞。
- 2026-09-29：Interrupt/Resume 专项为 `91 passed, 3 skipped`；真实 PostgreSQL
  专项为 `14 passed`；默认完整套件为 `383 passed, 28 skipped`；启用真实
  PostgreSQL 与 Redis 的完整套件为 `410 passed, 1 skipped`，唯一跳过项为真实
  百炼模型 smoke test。
- 2026-09-29：`uv lock --check`、`uv pip check`、`compileall`、`uv build` 和
  `git diff --check` 全部通过。
- 2026-09-29：Reviewer 复审通过，负责人确认验收通过，允许按工程流程提交并同步
  GitHub、Gitee，两个远程确认一致后进入 S2.5-08。
- 2026-09-29：S2.5-07 实现、测试与验收记录已提交为 `5b5e5af`。
- Result：S2.5-07 已通过实现、真实依赖门禁、独立复审和负责人验收，状态更新为
  `VERIFIED`。

---

## S2.5-08 Checkpoint 对账与崩溃恢复

**Status:** VERIFIED

**Dependencies:** S2.5-01, S2.5-02, S2.5-04, S2.5-07

### Work

- 为 Run Checkpoint 写入 run_id 和稳定结果标识 metadata
- 实现 Checkpoint-first 的完成与中断投影
- 从精确最新 Run Checkpoint 或 start_checkpoint + input_payload 恢复
- 实现最多三次恢复和失败终态
- 对副作用执行强制幂等能力检查

### Acceptance

- [x] Run 入库后、首 Checkpoint 前崩溃可恢复
- [x] Checkpoint 后、Run 投影前崩溃只补投影，不重复模型调用
- [x] 流式中崩溃后 message.started 新 attempt 会清除旧草稿
- [x] interrupted 启动扫描只对账 stopped Checkpoint，不自动执行 Graph
- [x] 第三次恢复失败后形成唯一 failed 终态
- [x] 无幂等保障的副作用执行不会进入自动恢复

### Verification Notes

- 2026-09-29：Parent Run 执行及 Regenerate fork 的每个新 Checkpoint 均写入
  `run_id` 与稳定 `response_message_id` metadata；恢复器通过 metadata 过滤只读取
  目标 Run 的最新 Checkpoint，不使用 Session 最新状态进行模糊恢复。
- 2026-09-29：Coordinator 启动与周期扫描会分派 `running` / `recovering`，继续跳过
  `interrupted`；现场 Resume 使用独立分派原因，不计入崩溃恢复次数。
- 2026-09-29：恢复接管在 PostgreSQL 单事务中递增 `recovery_attempts`、切换
  `recovering` 并写内部 durable event；恢复激活次数必须匹配权威计数，最多三次。
- 2026-09-29：真实 LangGraph + PostgreSQL 测试证明：最终消息与 Interrupt
  Checkpoint 只补数据库投影且节点调用次数不增加；中间 Checkpoint 从精确位置继续，
  首 Checkpoint 前从 `start_checkpoint_id + input_payload` 重放，均产生递增的
  `message.started` attempt 且不重复 HumanMessage。
- 2026-09-29：真实 PostgreSQL Checkpointer 在关闭并重新打开 Saver 后，仍能在同一
  Session 的多个 Run Checkpoint 中按 `run_id` metadata 精确找回目标 Run 终态。
- 2026-09-29：恢复次数已达三次且仍无终态 Checkpoint 时，不再执行 Graph，保存非空
  `incomplete` 公共消息并形成唯一 `run.failed`；未知副作用能力在自动执行前被拒绝。
- 2026-09-29：S2.5-08 与 Interrupt/Coordinator 真实 PostgreSQL 专项结果为
  `23 passed`；默认完整套件结果为 `388 passed, 35 skipped`；启用真实 PostgreSQL
  与 Redis 的完整套件结果为 `422 passed, 1 skipped`，唯一跳过项为真实百炼
  smoke test。
- 2026-09-30：修复 `REV-S25-08-001`。queued、running、interrupted 与
  cancel_requested Run 在发现自身已落盘的非空 `stopped` Checkpoint 时，均在任何
  Graph 执行前只补 `run.cancelled` 投影；running / recovering 先经过既定
  cancel_requested 状态，不把 stopped 错投影为 failed。interrupted 只在启动扫描
  进入 Checkpoint 对账，没有 stopped 消息时立即返回并继续等待 Resume / Cancel。
- 2026-09-30：取消对账在写事件前检查已有 `message.finalized`，因此进程在
  `message.finalized` 已提交、`run.cancelled` 未提交的窗口崩溃后不会产生重复公开
  终态事件；interrupted 的 pending Interrupt 随 `run.cancelled` 原子关闭。
- 2026-09-30：新增真实 PostgreSQL Checkpointer 重启确定性测试，覆盖 queued、
  running、interrupted、cancel_requested 四种 stopped 崩溃窗口，均验证 Graph
  调用次数为 0、稳定 stopped AIMessage 唯一、`message.finalized` 唯一且
  `run.cancelled` 唯一。S2.5-08 与 Interrupt/Coordinator 专项结果为 `27 passed`；
  默认完整套件结果为 `388 passed, 39 skipped`；启用真实 PostgreSQL 与 Redis 的
  完整套件结果为 `426 passed, 1 skipped`，唯一跳过项为真实百炼 smoke test。
- 2026-09-30：`uv lock --check`、`uv pip check`、`compileall`、`uv build`、
  `docker compose config --quiet` 与 `git diff --check` 全部通过。
- 2026-09-30：Reviewer 确认 `REV-S25-08-001` 修复通过，负责人确认 S2.5-08
  验收通过，允许按工程流程提交并同步 GitHub、Gitee，两个远程确认一致后进入
  S2.5-09。
- Result：S2.5-08 已通过实现、真实依赖门禁、独立复审和负责人验收，状态更新为
  `VERIFIED`。
- 2026-09-30：S2.5-08 已提交为 `e6fe9c2`，GitHub 与 Gitee 的 `master` 均已
  核对指向该提交，允许开始 S2.5-09。

---

## S2.5-09 最小 Agent 执行契约

**Status:** VERIFIED

**Dependencies:** S2.5-02, S2.5-04

### Work

- 定义 RunContext、AgentContext、TaskInput 与类型化事件出口
- 将现有 Capability 调用适配到统一边界
- 保持 ChildResult 极薄控制面和 Parent 公共消息数据面
- 使用 Fake Agent 验证慢速、失败和中断场景

### Acceptance

- [x] Agent 无法直接操作 SSE、Redis、Session 删除或 Run 终态
- [x] Agent 私有状态不进入 Parent 或公开事件
- [x] 现有 general_chat / en_to_zh / OUT_OF_SCOPE 行为不回归
- [x] 未引入 Manifest、动态 Registry、权限或多 Agent 调度

### Verification Notes

- 2026-09-30：新增最小统一 Agent Contract：`RunContext` 只提供 Run 稳定标识、
  只读取消探针和类型化事件出口；`AgentContext` 只提供固定 capability 标识与隔离的
  Child 配置；`TaskInput` 只保存由 Parent 公共消息派生且与原对象深拷贝隔离的执行
  视图。
- 2026-09-30：`general_chat` 与 `en_to_zh` 已通过既有 Adapter 接入统一三上下文
  调用入口。Agent 只发送 `AgentTextEvent`，Adapter 重建允许公开的消息字段后再写回
  Parent 数据面；`ChildResult` 继续只承载状态与 `OUT_OF_SCOPE` 控制信号。
- 2026-09-30：持久 Run 的 `request_id`、`input_message_id` 与
  `response_message_id` 会随 Parent Checkpoint metadata 贯穿普通执行、Regenerate
  fork 和崩溃恢复；Child 仍只使用 `{session_id}:{capability_id}` 隔离配置。
- 2026-09-30：Fake Agent 测试覆盖慢速执行观察协作式取消、失败控制结果以及真实
  LangGraph Checkpoint 的 Interrupt/Resume；契约测试同时证明上下文不含 SSE、
  Redis、Session 删除和 Run Repository，未定义事件与私有字段无法通过事件出口。
- 2026-09-30：S2.5-09 相关专项为 `43 passed, 4 skipped`；默认完整套件为
  `394 passed, 39 skipped`；启用真实 PostgreSQL 与 Redis 的完整套件为
  `432 passed, 1 skipped`，唯一跳过项为按规范显式启用的真实百炼 smoke test。
- 2026-09-30：`uv lock --check`、`uv pip check`、`compileall`、`uv build`、
  `docker compose config --quiet` 与 `git diff --check` 全部通过。
- 2026-09-30：修复 `REV-S25-09-001`。`TaskInput` 在统一契约边界对每条
  `BaseMessage` 及其嵌套字段执行深拷贝，不再与 Parent `messages` 共享可变引用；
  两个 Adapter 的确定性 Fake Agent 测试均验证修改消息正文、普通元数据和嵌套
  元数据后，Parent Checkpoint 中稳定消息 ID、原始正文及元数据保持不变。
- 2026-09-30：隔离回归为 `2 passed`，S2.5-09 相关专项为
  `45 passed, 4 skipped`；修复后默认完整套件为 `396 passed, 39 skipped`；启用
  真实 PostgreSQL 与 Redis 的完整套件为 `434 passed, 1 skipped`，唯一跳过项
  仍为真实百炼 smoke test。
- 2026-09-30：Reviewer 确认 `REV-S25-09-001` 修复通过，负责人确认 S2.5-09
  验收通过，允许按工程流程提交并同步 GitHub、Gitee，两个远程确认一致后进入
  S2.5-10。
- Result：S2.5-09 已通过实现、真实依赖门禁、独立复审和负责人验收，状态更新为
  `VERIFIED`。

---

## S2.5-10 `/chat` 页面迁移与 Runtime 演示模式

**Status:** VERIFIED

**Dependencies:** S2.5-04 ~ S2.5-09

### Work

- 页面迁移到 202 Run API 和独立 SSE Gateway
- 展示 Run 状态、刷新重连、Cancel 和 Interrupt/Resume
- 终态后重新读取 Parent 历史
- 增加默认关闭的开发环境 Runtime 演示模式
- 保持第三方开源组件与 HTML / JSX / API Client / CSS 分离

### Acceptance

- [x] 页面可观察 queued 到终态的完整状态变化
- [x] 页面刷新后可恢复活动 Run 和中断操作
- [x] attempt 增长时不会混合旧、新流式文本
- [x] 开发演示场景经过真实后端链路，前端不伪造
- [x] 演示模式不进入正式 Capability Router
- [x] 构建继续产出独立且带哈希的 JS / CSS

### Verification Notes

- 2026-09-30：`/chat` 已迁移到 HTTP 202 Run API 与独立 GET SSE；公开 Run
  状态面板支持 queued、running、recovering、interrupted、cancel_requested 和
  终态展示，页面刷新可恢复活动 Run、游标和待处理中断，同 Run Resume 与异步
  Cancel 均通过确定性前端测试。
- 2026-09-30：客户端只投影公开事件白名单，按已成功处理的 `seq` 断点续传；
  `message.started.attempt` 增大时清空旧草稿，终态始终重新读取 Parent 公共历史，
  不依赖临时增量作为最终权威内容。
- 2026-09-30：新增默认关闭的 `RUNTIME_DEMO_MODE`。显式启用后，normal、
  interrupt、fail、slow 四种确定性场景经过真实 Run、RuntimeEvent、Redis、GET
  SSE、Checkpoint 与 Interrupt/Resume 链路；演示 Graph 不构建或调用正式
  Capability Router。
- 2026-09-30：S2.5-10 后端专项为 `28 passed`，前端 Vitest 为 `23 passed`；
  默认完整套件为 `399 passed, 39 skipped`；启用真实 PostgreSQL 与 Redis 的完整
  套件为 `437 passed, 1 skipped`，唯一跳过项为按规范独立启用的真实百炼 smoke
  test。
- 2026-09-30：使用系统 Chrome 的 Playwright 真实页面验收为 `1 passed`，覆盖
  正常完成、中断后刷新恢复与 Resume、确定性失败以及慢速运行 Cancel；Vite 构建
  继续生成独立且带内容哈希的 JS 与 CSS。
- 2026-09-30：收尾复审补齐真实网络异常后的游标续传、queued/interrupted 直接
  Cancel 终态回读以及刷新跨终态提交窗口的确定性测试；客户端在 `fetch` 或
  `reader.read()` 网络失败后从最后成功处理的 `seq` 重连，直接取消终态立即回读
  Parent 历史，页面恢复先确认活动 Run 再读取权威历史。页面元数据同步更新为
  Stage 2.5。
- 2026-09-30：独立代码二次复核确认上述三个 Important 与一个 Minor 均已解决，
  结论为 `Ready to merge: Yes`；任务仍按流程保持 `DONE`，等待负责人验收。
- 2026-10-01：Reviewer 发现 `REV-S25-10-001`。页面在 Run 创建请求返回 202 前
  仍处于 idle，快速重复提交会使用不同 `request_id` 创建多个 Run；新会话场景会
  进一步创建多个 Session，因此退回修复且继续保持 `DONE / 待复审`。
- 2026-10-01：新增同步 `submittingRef` 与页面 `submitting` 状态，在任何网络等待
  前原子占用提交入口；提交期间禁用 Sender、Regenerate、新建和会话切换。Run
  摘要返回并投影 queued 后才释放提交锁，POST 确定失败时删除对应乐观用户消息并
  恢复交互，避免保留非权威页面内容。
- 2026-10-01：延迟 202 的 Vitest 回归先稳定复现两次 API 调用，修复后确认快速
  重复提交只调用一次创建接口；失败清理与再次提交测试同时通过。前端完整测试为
  `25 passed`。
- 2026-10-01：Edge Playwright 新增 800ms HTTP 202 延迟场景，真实 PostgreSQL、
  Redis 与 Runtime 链路确认只有一次 POST、一个 Session 和一条用户消息；连同
  原有完整流程共 `2 passed`。修复后默认套件为 `399 passed, 39 skipped`，真实
  PostgreSQL + Redis 完整套件为 `437 passed, 1 skipped`。
- 2026-10-01：独立代码复核确认提交锁在首个 `await` 前建立，成功路径先投影
  queued 再释放，失败路径无条件释放且只清理本次乐观消息；未发现解锁空窗、死锁
  或范围回归，`REV-S25-10-001` 复核结论为 `Ready to merge: Yes`。
- 2026-10-01：负责人确认 S2.5-10 验收通过，允许按工程流程提交并同步 GitHub、
  Gitee；两个远程确认一致后进入 S2.5-11。
- 2026-09-30：`uv lock --check`、`uv pip check`、`compileall`、`uv build`、
  `docker compose config --quiet` 与 `git diff --check` 全部通过。
- Result：S2.5-10 已通过实现、真实依赖门禁、独立复审和负责人验收，状态更新为
  `VERIFIED`。

---

## S2.5-11 Stage 2.5 集成验收

**Status:** VERIFIED

**Dependencies:** S2.5-01 ~ S2.5-10

### Acceptance

- [x] 默认 pytest 使用 Fake Model 并全部通过
- [x] PostgreSQL Run / Event / Interrupt 集成测试通过
- [x] Redis 正常、启动失败和运行中断验收通过
- [x] 进程重启与 Checkpoint 对账恢复验收通过
- [x] SSE 续传、合并、去重与慢客户端验收通过
- [x] Cancel、Interrupt/Resume 和 Session 删除验收通过
- [x] Vitest、前端构建和 Playwright 真实页面验收通过
- [x] Stage 1 / Stage 2 产品能力无回归
- [x] 未实现 Future 候选能力
- [x] README、AGENTS 和全部 docs 与实现一致

真实百炼仍只作为独立 smoke test。需要执行时，由负责人在项目指定配置文件中
完成配置后再显式运行，不作为默认或 CI 门禁。

### Verification Notes

- 2026-10-01：默认 `uv run --locked pytest -q` 使用 Fake Model，结果为
  `399 passed, 39 skipped`；跳过项仅为显式启用的 PostgreSQL、Redis 与真实百炼
  集成测试。
- 2026-10-01：项目现有 PostgreSQL 与 Redis Docker Compose 容器均为 `healthy`；
  启用 `RUN_POSTGRES_TESTS=1` 和 `RUN_REDIS_TESTS=1` 的完整套件结果为
  `437 passed, 1 skipped`，唯一跳过项为不属于本门禁的真实百炼 smoke test。
- 2026-10-01：真实依赖完整套件覆盖 Run / RuntimeEvent / Interrupt 事务与幂等、
  Redis Stream 顺序和故障降级、Checkpoint 对账与最多三次恢复、SSE 合并续传与
  慢客户端、Cancel 竞争、同 Run Resume 及 Session 完整硬删除。
- 2026-10-01：前端 Vitest 结果为 `25 passed`；Vite 生产构建成功，系统 Edge 的
  Playwright 真实页面验收结果为 `2 passed`，覆盖正常完成、中断刷新与 Resume、
  失败、慢速 Cancel，以及 HTTP 202 延迟期间的重复提交互斥。
- 2026-10-01：Stage 1 / Stage 2 产品定向回归结果为 `67 passed, 1 skipped`；
  真实 PostgreSQL 持久化场景已包含在上述 `437 passed` 完整套件中。
- 2026-10-01：`uv lock --check`、`uv pip check`、Python `compileall`、`uv build`、
  `docker compose config --quiet`、wheel 哈希 JS/CSS 资源检查和范围审计均通过；
  未发现 Capability Manifest、动态 Registry、权限、多 Agent、多实例 Worker、
  Transactional Outbox、文件或 RAG 等 Future 能力实现。
- 2026-10-01：README 的阶段进度和运行说明已同步到 S2.5-11；AGENTS、需求、架构、
  决策与当前实现边界一致。
- 2026-10-01：独立只读复审逐项核对验收标准、测试映射、文档状态和 Future 范围，
  未发现 Critical、Important 或 Minor 问题，结论为 `Ready to merge: Yes`。
- 2026-10-01：负责人确认 S2.5-11 验收通过；按工程流程提交并同步 GitHub、Gitee，
  暂不开始后续阶段开发。
- Result：S2.5-11 与 Stage 2.5 整体验收通过，状态更新为 `VERIFIED`；后续阶段
  尚未重新裁决，不进入 Future 候选能力开发。

---

# Stage 3：Capability Runtime

## S3-00 Stage 3 架构基线确认

**Status:** VERIFIED

**Dependencies:** S2.5-11

### Goal

在已验收 S2.5 Runtime 上确认静态 Capability Runtime 的职责边界、数据模型方向、
恢复策略和明确非目标。

### Scope

- Manifest / Source / Registry
- 统一 Capability、Invocation、Task、State Scope
- Health、并发、超时、恢复、Operation Ledger 和最小用户权限
- 现有测试 Capability 迁移及 Session 删除扩展

### Forbidden Scope

- 正式功能编码
- 热加载、Workflow、多实例、RBAC、RAG 或管理平台

### Database Changes

无；本任务只做架构裁决。

### Main Interfaces

无实现接口；输出 S3-00 已确认架构基线。

### Acceptance

- [x] 负责人逐项确认 S3 核心边界
- [x] 明确 S3 非目标
- [x] S2.5 的 Run、Event、SSE、Checkpoint 与 Parent 权威源保持有效

### Test Requirements

不运行实现测试；由 S3-01 对真实基线进行文档一致性审查。

### Regression Scope

不修改实现。

### Definition of Done

负责人已完成 S3-00 裁决，可进入 S3-01 文档化。

---

## S3-01 整理 S3 架构文档与任务验收树

**Status:** VERIFIED

**Dependencies:** S3-00

### Goal

把已确认的 S3-00 决策落实到现有权威文档，解决实现级缺口，形成可独立编码、Review
和验收的 Stage 3 任务树。

### Scope

- 更新 requirements / architecture / decisions / tasks / README / AGENTS
- 明确 Manifest 最终 Schema、Capability 接入协议和 Runtime 边界
- 明确四张新增表的字段、约束、索引、外键和删除语义
- 记录架构调整清单
- 定义 S3 分阶段任务和最终 Gate

### Forbidden Scope

- 修改业务实现、数据库或测试代码
- 创建与现有权威文档重复的新规划文档
- 提前实现 S3-02 及后续任务

### Database Changes

无；仅记录后续数据模型。

### Main Interfaces

无代码接口；文档确定 `CapabilitySource`、Manifest、Bootstrap Factory、Capability、
AgentResult、Task、Operation 和 Permission 契约。

### Acceptance

- [x] S3-00 已确认内容全部进入现有权威文档
- [x] 公开 capability_id、测试权限、无候选错误、Operation Context、entrypoint、
  manual recovery、OUT_OF_SCOPE Task 回滚和 Regenerate 安全门禁已完成负责人裁决
- [x] 数据表定义包含字段、类型、可空、约束、索引、外键与删除语义
- [x] 每个开发任务包含目标、依赖、范围、禁止范围、数据库、接口、验收、测试、
  回归和完成条件
- [x] 没有编写业务代码

### Test Requirements

- Markdown 标题与链接检查
- 文档冲突关键字和阶段范围审计
- `git diff --check`

### Regression Scope

只读核对 S2.5 实现和全部权威文档，不修改已验收代码。

### Definition of Done

文档检查通过、负责人验收后提交并同步两个远程；在此之前不得开始 S3-02。

### Verification Notes

- 2026-10-02：对照 S2.5 实际代码核查固定 Router/Parent 分发、公开 capability_id
  Literal、Agent Contract、Checkpoint Recovery、RuntimeEvent 内部前缀、数据库建表
  方式和 Session 删除顺序，没有把草案建立在不存在的抽象上。
- 2026-10-02：负责人完成公开 capability_id、测试权限、无候选错误、Operation
  Context、entrypoint 工厂、manual recovery、OUT_OF_SCOPE Task 回滚和 Regenerate
  安全门禁的逐项裁决。
- 2026-10-02：原位更新 AGENTS、README、requirements、architecture、decisions 和
  tasks；未新增相似规划文档，未修改任何业务或测试代码。
- 2026-10-02：S3-00~S3-14 共 15 个任务连续且每项均具备九类必需章节；架构决策
  D-001~D-063 连续无重复；六份 Markdown 围栏成对，README 相对链接全部存在，
  活跃阶段陈旧表述扫描为零，`git diff --check` 通过。
- 2026-10-03：负责人确认 S3-00、S3-01 已处理并要求开始 S3-02，S3-01 验收通过。
- Result：S3-01 已 VERIFIED；完成提交并同步 GitHub、Gitee 后开始 S3-02。

---

## S3-02 Manifest Schema 与 Local CapabilitySource

**Status:** VERIFIED

**Dependencies:** S3-01 VERIFIED

### Goal

实现严格、确定且与实例化解耦的 Manifest 解析和本地 Source。

### Scope

- Manifest 及嵌套 concurrency/execution Pydantic Schema
- ID、版本、entrypoint 与跨字段组合校验
- `CapabilitySource` Protocol 和只读来源记录
- 固定目录 Local YAML Source、确定排序和完整错误收集
- 两个测试 Capability 的标准 Manifest 文件，但暂不接入执行链路

### Forbidden Scope

- entrypoint 导入和 Capability 实例化
- Registry、Router、权限、Task 或运行时执行
- 文件监听、热加载、mount/unmount
- 任意业务扩展字段

### Database Changes

无。

### Main Interfaces

```text
CapabilityManifest
ConcurrencyPolicy
ExecutionPolicy
CapabilityManifestDocument
CapabilitySource.load() -> list[CapabilityManifestDocument]
LocalYamlCapabilitySource
```

### Acceptance

- [x] 合法 Manifest 可稳定解析为关闭 Schema
- [x] 缺失 state_scope、未知字段、非法 ID/entrypoint/版本均拒绝
- [x] automatic + unsafe、并发字段非法组合均拒绝
- [x] Source 返回全部文档及来源，不因单文件错误提前停止
- [x] 扫描结果顺序跨运行稳定
- [x] 两个测试 Manifest 声明 invocation + none，但尚不接入执行链路

### Test Requirements

- Schema 边界与中文 description 单元测试
- YAML 语法、未知字段、重复列表、路径排序测试
- property/参数化测试覆盖策略组合

### Regression Scope

- 默认测试不读取外部目录
- 两个既有 Capability 行为不变

### Definition of Done

实现、专项测试、默认回归、Review 和任务文档证据完成，经负责人验收后提交并同步
GitHub/Gitee 同一提交。

### Verification Notes

- 2026-10-03：新增严格关闭且冻结的 `CapabilityManifest`、`ConcurrencyPolicy`、
  `ExecutionPolicy`；覆盖 ID、SemVer、entrypoint、State Schema、恢复/副作用和并发
  组合校验，所有 Pydantic 字段均提供明确中文 `description`。
- 2026-10-03：新增只负责原始文档收集的 `CapabilitySource` Protocol 与
  `LocalYamlCapabilitySource`；按规范化相对路径稳定排序，单文件 YAML/读取错误转为
  带来源的只读错误记录，不导入 entrypoint 或实例化 Capability。
- 2026-10-03：`general_chat`、`en_to_zh` 已增加 `invocation + none` 标准 Manifest；
  wheel 构建检查确认两个 YAML 和 Manifest/Source 模块均进入分发包，尚未接入执行链路。
- 2026-10-03：独立代码复审发现并已修复合法 SemVer 预发布标识误拒、YAML 循环/
  集合结构破坏完整错误收集，以及时间策略接受无穷值三项问题；均先补失败测试，
  再完成最小修复。
- 2026-10-03：修复 `REV-S3-02-001`；解析阶段的极深 YAML `RecursionError` 现在
  转换为当前来源的 `YAML_STRUCTURE_ERROR`，不会阻止后续合法 Manifest 收集，并以
  “极深错误文件 + 合法兄弟文件”确定性回归测试锁定。
- 2026-10-03：专项测试 `70 passed`；默认完整套件 `469 passed, 39 skipped`，跳过项
  均为需显式环境开关的 PostgreSQL、Redis 或百炼测试。
- 2026-10-03：`uv lock --check`、`uv pip check`、`compileall`、`uv build` 和
  `git diff --check` 均通过；本任务无数据库变更，未启动 Docker 外部依赖。
- 2026-10-03：负责人确认 S3-02 审核通过并要求开始 S3-03，S3-02 验收通过。
- Result：S3-02 已 VERIFIED；完成提交并同步 GitHub、Gitee 后开始 S3-03。

---

## S3-03 Capability 协议、Bootstrap 与静态 Registry

**Status:** VERIFIED

**Dependencies:** S3-02

### Goal

从已校验 Manifest 构建不可变 Registry Snapshot，并建立统一实例化与资源生命周期。

### Scope

- `CapabilityBootstrapContext` 和统一 entrypoint 工厂校验
- Capability `initialize / health_check / invoke` Protocol 骨架
- AgentResult、metadata、HealthResult 和 CapabilityError Schema
- 动态 import、实例初始化、逆序 cleanup
- 重复 ID 整组隔离和单 Capability 故障隔离
- Registry Snapshot 查询与最小 Router Projection

### Forbidden Scope

- Parent/Router 正式改线
- Task、权限、并发、超时或恢复执行
- 热加载、卸载、DRAINING 或多版本实例

### Database Changes

无。

### Main Interfaces

```text
CapabilityBootstrapContext
CapabilityFactory
Capability
AgentResult / AgentResultMetadata
HealthResult
CapabilityError
CapabilityRegistry / CapabilityRegistryEntry / RouterProjection
```

### Acceptance

- [x] entrypoint 只能加载合法工厂，错误对象被隔离
- [x] 重复 ID 的全部来源不进入 Snapshot
- [x] 单个 import/initialize/health 失败不影响其他能力
- [x] Capability 看不到 SSE、Redis、Session 或 Run Repository
- [x] cleanup 对部分初始化失败和正常关机均幂等逆序执行
- [x] Router Projection 只有四个允许字段

### Test Requirements

- Fake Source / Fake Factory / Fake Capability 单元测试
- import、协议不匹配、初始化失败、cleanup 失败隔离测试
- 重复 ID 全隔离与顺序确定性测试

### Regression Scope

- 应用主链路尚不切换 Registry
- S2.5 测试继续通过

### Verification Notes

- 2026-10-03：完成关闭的 `AgentResult` / `AgentResultMetadata` / `HealthResult`、
  不可变关闭 `CapabilityError`、五字段只读 `CapabilityBootstrapContext` 和异步
  `Capability` 协议；Registry 会严格复验 Manifest、在完整校验前收集全部合法声明
  ID、整组隔离重复来源，并校验同步工厂及三个 bound async 方法的精确签名。
- 2026-10-03：完成 import、协议、初始化和启动健康检查的局部故障隔离；静态
  Snapshot 不可热变更，可服务 Router Projection 严格只含
  `capability_id / name / description / enabled`。
- 2026-10-03：完成部分初始化和正常关闭的逆序幂等 cleanup；单个 cleanup 失败不
  阻止其余资源释放，构建或关闭取消会继续清理并允许未完成回调安全重试。
- 2026-10-03：首轮代码复核发现并关闭 `REV-S3-03-001/002/003`；正式审核随后发现
  `REV-S3-03-004/005`。`REV-S3-03-005` 已通过 CapabilityError 根类型、构造后
  篡改、额外属性、`__dict__` 和标量子类对抗测试。
- 2026-10-03：`REV-S3-03-004` 首次修复后，Reviewer 进一步发现无效来源声明的
  Capability ID 未完全复用正式 Manifest 的空白归一化语义。最终将 ID 约束提取为
  共享 Pydantic `CapabilityId` 类型，并由 Registry 预提取与正式 Schema 共用同一个
  `TypeAdapter`，不再手写字符串归一化。
- 2026-10-03：新增两组互补回归：带普通首尾空白的同 ID 无效来源必须与合法来源
  整组标记为 `duplicate_id`；正式 Schema 不会裁剪的控制分隔符不得造成虚假冲突。
  最终独立复核未发现新的 Critical、Important 或 Minor 问题。
- 2026-10-03：默认完整套件 `491 passed, 39 skipped`；启用真实 PostgreSQL +
  Redis 的完整套件 `529 passed, 1 skipped`，唯一跳过为显式百炼 smoke test。
- 2026-10-03：`uv lock --check`、`compileall`、`uv pip check`、`uv build`、
  `docker compose config --quiet` 和 `git diff --check` 通过。
- 2026-10-08：Reviewer 确认 S3-03 审核通过；任务更新为 `VERIFIED`，按工程流程
  提交并同步 GitHub、Gitee 后进入 S3-04。
- Result：S3-03 已 `VERIFIED`；静态 Registry 保持独立组件边界，未提前接入应用
  主链路。完成验收提交与双远程同步后进入 S3-04。

### Definition of Done

静态 Registry 可独立构建和关闭，未接入业务链路；测试、Review、负责人验收、双远程
同步完成。

---

## S3-04 Capability Task、Context、Operation 与 Permission 持久化

**Status:** VERIFIED

**Dependencies:** S3-01 VERIFIED

### Goal

建立 S3 四张表及只负责持久化原子性的 Repository，不提前实现业务编排。

### Scope

- `capability_tasks`
- `capability_task_contexts`
- `capability_operations`
- `user_capability_permissions`
- 领域模型、建表、约束、索引、CRUD 和事务基础方法
- 延续现有幂等 `setup()` 方式

### Forbidden Scope

- Task action 业务决策
- Router、Registry、Capability 调用
- Operation Context Manager
- RBAC、审计历史表、workflow_id 或迁移框架替换

### Database Changes

按 architecture 15.7 创建四张表、复合 FK、CHECK 与索引；外键采用 RESTRICT，
不对本地 Registry 或固定 user 建数据库 FK。

### Main Interfaces

```text
CapabilityTaskRepository
CapabilityTaskContextRepository
CapabilityOperationRepository
UserCapabilityPermissionRepository
```

### Acceptance

- [x] 建表可重复执行
- [x] 非法状态、ended_at 组合和跨 Session/Capability current Task 被数据库拒绝
- [x] 同一 current context 唯一
- [x] Operation 业务键及 idempotency_key 唯一
- [x] 权限无记录与 false/true 可准确区分
- [x] Repository 不记录用户正文、外部响应或凭据

### Test Requirements

- 纯模型/Repository 单元测试
- 真实 PostgreSQL 约束、并发写和事务回滚测试
- setup 重入与旧数据库升级测试

### Regression Scope

- S2.5 原表、Run/Interrupt/Event 约束不变
- 现有 Session 删除暂不接入新表

### Verification Notes

- 2026-10-08：新增 `capability_tasks`、`capability_task_contexts`、
  `capability_operations`、`user_capability_permissions` 四张表及严格 CHECK、复合外键、
  RESTRICT 外键和查询/唯一索引，延续幂等 `setup()`，未引入 Alembic。
- 2026-10-08：新增四个 Repository 与不可变领域对象；持久化原子方法覆盖新建 Task
  与切换 current、Task 终态与清理 current、Operation 双唯一键幂等登记、权限
  无记录/显式拒绝/显式允许三态，不包含 Task Action、Router 或调用编排。
- 2026-10-08：单元专项 `10 passed`；真实 PostgreSQL 专项 `3 passed`，覆盖旧数据库
  增量建表、setup 重入、数据库约束、并发写和事务回滚。
- 2026-10-08：默认完整套件 `501 passed, 42 skipped`；启用真实 PostgreSQL + Redis
  的完整套件 `542 passed, 1 skipped`，唯一跳过为显式百炼 smoke test。
- 2026-10-08：Reviewer 确认 S3-04 审核通过；任务更新为 `VERIFIED`，按工程流程
  提交并同步 GitHub、Gitee 后进入 S3-05。
- Result：S3-04 已 `VERIFIED`；持久化层仍未接入现有 Session 删除或 Invocation/
  Task Service 主链路，完成验收提交与双远程同步后进入 S3-05。

### Definition of Done

四张表和 Repository 经真实 PostgreSQL 门禁、Review、负责人验收并同步两个远程。

---

## S3-05 Invocation、Task Service 与 State Scope

**Status:** VERIFIED

**Dependencies:** S3-03, S3-04

### Goal

实现单 Run 顺序 Invocation、Task 解析、确定性 Child thread 和 State Schema 兼容边界。

### Scope

- Invocation ID、open lifecycle invariant 和 internal durable event Schema
- `continue/new` Task Service 事务
- continue 缺失降级 new 的 diagnostic
- provisional Task 与 OUT_OF_SCOPE 回滚
- Task terminal intent 的 Runtime 应用
- invocation/run/session thread_id factory
- State Schema compatibility 校验

### Forbidden Scope

- Router 权限候选、Health、并发、超时和 Operation Ledger
- 多个并行 Invocation、Invocation 表、State 自动迁移
- 非 current Task 选择 API

### Database Changes

不新增表；写 S3 Task 表和既有 RuntimeEvent。必要索引调整必须限定在已确认查询路径。

### Main Interfaces

```text
CapabilityInvocationService
CapabilityTaskService
ChildThreadIdFactory
StateCompatibilityPolicy
InvocationStarted/Completed/Failed/CancelledPayload
```

### Acceptance

- [x] 同一 Run 同时最多一个 open Invocation
- [x] new 与 current 切换原子，continue 正确锁定 current
- [x] continue 无 current 时原子降级 new 并写 diagnostic
- [x] rejected new 恢复原 current，清理 provisional Task/thread
- [x] 拒绝前已有输出、Operation 或业务 State 时不执行回滚并失败
- [x] 三种 scope 的 thread_id 确定、互不串线
- [x] 不兼容 Task 拒绝 continue 且 Task/Context 不变
- [x] Run 失败/取消不自动改变 Task 状态

### Test Requirements

- Task Service 状态机和 thread_id 测试向量
- 真实 PostgreSQL 行锁、事务、并发和回滚测试
- 真实 Checkpointer 三 scope 隔离/共享测试
- OUT_OF_SCOPE 零业务副作用测试

### Regression Scope

- Parent 公共 messages 与 10/5 Context Builder 不变
- 既有有限回流语义不变

### Verification Notes

- 2026-10-08：新增关闭且带中文字段说明的 Invocation/Task internal durable
  RuntimeEvent Schema；所有 Invocation 事件固定携带 `invocation_id`、`capability_id`、
  `task_id`、请求/有效 Task Action、`state_scope` 与实际 `state_schema_version`，终态再携带
  `outcome` 或 `error_code`。`started` 与终态事件在 PostgreSQL Run 行锁内配对，恢复复用
  唯一 open `invocation_id`，普通事件写入入口不能绕过该约束。
- 2026-10-08：新增 `CapabilityTaskService`，在 Run/Session/Task 行锁和单事务内完成
  `new`、`continue`、缺失 current 降级、最近执行投影、Task 终态与内部事件写入；同一
  Invocation 的重复解析幂等复用同一个 Task。
- 2026-10-08：实现 provisional Task 的 OUT_OF_SCOPE 回滚；零公开输出、零 Operation、
  零 Child Checkpoint 时恢复原 current、删除 provisional Task、原子关闭 Invocation 并
  清理临时 thread，任一业务事实已存在时保持 Task 并以
  `CAPABILITY_EXECUTION_FAILED` 关闭 Invocation。
- 2026-10-08：新增固定 `capability:v1` Child thread_id 工厂和 State Schema 兼容策略；
  invocation/run/session 分别按 invocation_id、run_id、task_id 隔离或共享。兼容旧 Task
  时使用 Task 已固化的 State Schema 版本生成 namespace，不会切换到当前 Manifest 新版本；
  不兼容 continue 返回 `CAPABILITY_STATE_VERSION_INCOMPATIBLE` 且 Task/Context 不变。
- 2026-10-08：初次编码验证中，S3-05 单元专项 `42 passed`、真实 PostgreSQL/
  Checkpointer 专项 `3 passed`；默认完整套件 `516 passed, 45 skipped`，启用真实
  PostgreSQL + Redis 的完整套件 `560 passed, 1 skipped`。
- 2026-10-08：按 `REV-S3-05-001` 补齐 Invocation 事件最小关联字段、nullable 组合约束和
  `continue → new` durable event 精确断言；按 `REV-S3-05-002` 改为使用已解析 Task
  固化版本生成 Child thread_id，并增加跨 Run 读取旧版本真实 Checkpoint 的测试。
- 2026-10-08：修复后定向测试 `17 passed, 3 skipped`，Capability/Runtime 回归
  `229 passed, 20 skipped`，默认完整套件 `518 passed, 45 skipped`；`uv lock --check`、
  `uv pip check`、`compileall`、`uv build`、Compose 配置和 `git diff --check` 均通过。
- 2026-10-08：Docker Desktop 由负责人恢复后，修复后的真实 PostgreSQL/Checkpointer
  专项 `3 passed`；启用真实 PostgreSQL + Redis 的完整套件 `562 passed, 1 skipped`，
  唯一跳过为显式百炼 smoke test。
- 2026-10-08：Reviewer 确认 `REV-S3-05-001/002` 均已关闭，S3-05 审核通过；任务更新
  为 `VERIFIED`，按工程流程提交并同步 GitHub、Gitee 后进入 S3-06。
- Result：S3-05 已 `VERIFIED`；未接入 Parent/Router 主链路，未实现 S3-06 权限、
  动态候选或其他后续能力，完成验收提交与双远程同步后进入 S3-06。

### Definition of Done

Task/Invocation/State 专项、真实数据库与 Checkpointer 验证、Review、负责人验收和双远程
同步完成。

---

## S3-06 权限、动态 Router 候选与公开 capability_id

**Status:** VERIFIED

**Dependencies:** S3-03, S3-04, S3-05

### Goal

移除候选和产品 Schema 中的固定能力枚举，以 Registry + 权限构造动态路由边界。

### Scope

- Router 输入使用 Registry 最小投影
- Router 结构化输出增加 `task_action`
- Router 前权限过滤和 invoke 前实时复查
- `CAPABILITY_PERMISSION_DENIED` / `CAPABILITY_UNAVAILABLE` / unsupported 分流
- API、RuntimeEvent、历史和 Checkpoint 的 capability_id 放宽为 Manifest ID 字符串
- 前端按普通标签展示未知合法 ID

### Forbidden Scope

- 管理 API、权限缓存、RBAC/ABAC、用户切换
- Router confidence interrupt 或自动优化
- Health 实现细节；本任务使用可服务状态测试替身

### Database Changes

不新增表；只读/写 `user_capability_permissions`。

### Main Interfaces

```text
CapabilityPermissionService
RouterCandidateProvider
RouterDecision(capability_id, task_action, confidence)
ManifestCapabilityId public schemas
```

### Acceptance

- [x] 无记录和 false 均不进入 Router，true 才进入
- [x] Router 后撤销权限会在 invoke 前被拒绝
- [x] 无权限、不可服务和全部 OUT_OF_SCOPE 的 Run 终态严格区分
- [x] 第三个合法测试 ID 可通过消息、事件、历史和前端展示
- [x] 公开字段和事件 schema_version 不变
- [x] 两个测试 Capability 不被自动授权

### Test Requirements

- Router 候选、双检竞态和错误映射单元测试
- API/Event Pydantic Schema 回归
- 真实 PostgreSQL 权限动态变更测试
- 前端未知 capability_id 渲染测试

### Regression Scope

- S2.5 SSE 白名单、seq、重连和终态事件不变
- 现有两个 ID 的 JSON 输出完全兼容

### Verification Notes

- 2026-10-09：新增无缓存 `CapabilityPermissionService`，无权限行和
  `allowed=false` 均拒绝，只有 `allowed=true` 放行；Router 候选构造与 Invocation
  前复查分别实时读取权限，权限撤销后复查稳定返回不可重试
  `CAPABILITY_PERMISSION_DENIED`。
- 2026-10-09：新增 `RouterCandidateProvider`，只向 Router 暴露 Registry 的
  `capability_id/name/description/enabled` 四字段投影；已授权但不可服务返回可重试
  `CAPABILITY_UNAVAILABLE`，所有已实际拒绝候选被排除后返回空候选，保留 Parent
  `unsupported` 分流语义。S3-07 的 Health TTL/刷新治理和 S3-12 的正式主链路迁移均未
  提前实现。
- 2026-10-09：`RouterDecision` 增加必填 `task_action=continue|new`，Parent 控制状态
  可保存该动作；动态路径支持第三个合法测试 ID。既有 Stage 1 固定分发兼容层按任务树
  保留到 S3-12 移除，不参与动态候选授权判定。
- 2026-10-09：公开 SSE Schema、历史 DTO、`message.finalized` RuntimeEvent、历史转换、
  终态 Checkpoint 投影统一改用 `ManifestCapabilityId`；字段名、null 语义、公开事件类型
  和 `schema_version=1` 不变。前端继续把未知合法 ID 作为普通标签原样展示。
- 2026-10-09：S3-06 后端专项 `65 passed, 2 skipped`，权限/Router/历史增量专项
  `21 passed`，真实 PostgreSQL 权限动态变更专项连同单元测试 `6 passed`；前端完整
  套件 `26 passed`。
- 2026-10-09：内部复审发现并关闭 `REV-S3-06-001`：动态候选全部被本轮
  `OUT_OF_SCOPE` 拒绝后，Router 改为返回非错误控制结果，Parent 通过条件边进入
  `unsupported`，不再错误地产生 `ROUTER_NO_CANDIDATE`；新增 Router 与 Parent 贯通的
  两个确定性回归测试，修复后相关回归 `34 passed, 1 skipped`。
- 2026-10-09：修复后默认完整后端套件 `530 passed, 46 skipped`，前端完整套件
  `26 passed`。
- 2026-10-09：Docker Desktop 由负责人恢复后，PostgreSQL 与 Redis 均为 healthy；
  修复后启用真实 PostgreSQL + Redis 的完整套件 `575 passed, 1 skipped`，唯一跳过为显式
  百炼 smoke test。
- 2026-10-09：`uv lock --check`、`uv pip check`、`compileall`、`uv build`、前端生产
  构建、Compose 配置和 `git diff --check` 均通过；Wheel 包含权限路由模块及哈希前端
  资源。
- 2026-10-09：`REV-S3-06-001` 定向复审确认已关闭，复审专项 `26 passed`、精确分支
  复验 `5 passed`，未发现新的 Critical 或 Important 问题。
- 2026-10-09：Reviewer 与负责人确认 S3-06 验收通过，任务更新为 `VERIFIED`；按工程
  流程提交并同步 GitHub、Gitee 后进入 S3-07。
- Result：S3-06 已 `VERIFIED`；未提前实现 S3-07 Health TTL、刷新或其他后续能力，
  完成验收提交与双远程同步后进入 S3-07。

### Definition of Done

动态候选和权限行为通过后端/前端专项、Review、负责人验收并同步两个远程。

---

## S3-07 Capability Health 与服务准入

**Status:** VERIFIED

**Dependencies:** S3-03, S3-06

### Goal

实现启动强检、TTL Snapshot、按需单飞刷新和 degraded 服务规则。

### Scope

- Runtime 全局 Health TTL 配置
- 启动期 health_check
- Snapshot、过期刷新和单飞锁
- healthy/degraded/unhealthy 准入
- allow_degraded 与内部中文告警
- 单能力检查失败隔离

### Forbidden Scope

- Capability 业务依赖检查实现
- 健康管理 API、后台持续探测、分布式状态同步
- 把健康详情传给 Router 或公开 SSE

### Database Changes

无；HealthSnapshot 仅进程内。

### Main Interfaces

```text
CapabilityHealthService
HealthSnapshot
ServiceabilityDecision
```

### Acceptance

- [x] 启动时每个已实例化能力恰好强检一次
- [x] TTL 内复用，过期并发请求只刷新一次
- [x] degraded 的 allow true/false 行为正确
- [x] unhealthy 与检查异常不进入候选
- [x] 其他健康 Capability 不受影响

### Test Requirements

- 假时钟、并发单飞、异常和 TTL 边界单元测试
- Registry/Router 准入集成测试
- 日志脱敏断言

### Regression Scope

- `/health` 现有应用健康语义不擅自扩张为 Capability 明细 API
- Redis 不可用仍不影响健康准入模块启动

### Verification Notes

- 2026-10-09：新增关闭只读的 `HealthSnapshot` 与 `ServiceabilityDecision`，以及进程内
  `CapabilityHealthService`；启动阶段对每个 active 实例顺序强检一次，检查异常收敛为
  `unhealthy / HEALTH_CHECK_FAILED`，只记录 `capability_id`、错误类型和稳定摘要码，
  不记录异常正文、凭据或业务数据。
- 2026-10-09：新增默认 30 秒的 Runtime 全局 `CAPABILITY_HEALTH_TTL_SECONDS`；TTL 内
  复用快照，到达 `expires_at` 边界即在每 Capability 的异步锁内刷新，锁内二次检查保证
  并发请求只触发一次检查。配置层与服务层均拒绝零、负数、NaN 和无穷值。
- 2026-10-09：Registry 静态 Entry 只保留启动诊断，动态刷新不修改不可变 Snapshot；
  Router 候选改用异步 `serviceable_router_projections()`，只得到既有四字段投影。healthy
  放行，degraded 按 `allow_degraded` 判定并写中文 WARNING，unhealthy 和检查异常只排除
  对应能力，不影响其他健康能力。
- 2026-10-09：健康与配置专项 `17 passed`；Capability/Registry/Router/配置聚焦回归
  `170 passed, 7 skipped`；默认完整后端套件 `544 passed, 46 skipped`；启用真实
  PostgreSQL + Redis 的完整套件 `589 passed, 1 skipped`，唯一跳过为显式百炼 smoke
  test；前端完整套件 `26 passed`。
- 2026-10-09：`uv lock --check`、`uv pip check`、`compileall`、`uv build`、前端生产
  构建、Compose 配置和 `git diff --check` 均通过；独立代码复审未发现 Critical 或
  Important 问题，提出的无穷 TTL 配置校验和 degraded 日志断言两个 Minor 已补齐。
- 2026-10-09：Reviewer 与负责人确认 S3-07 验收通过，任务更新为 `VERIFIED`；按工程
  流程提交并同步 GitHub、Gitee 后进入 S3-08。
- Result：S3-07 已 `VERIFIED`；未提前实现 S3-08 并发、超时或错误映射，完成验收提交
  与双远程同步后进入 S3-08。

### Definition of Done

健康专项、并发测试、Review、负责人验收和双远程同步完成。

---

## S3-08 Capability 并发、超时与错误映射

**Status:** TODO

**Dependencies:** S3-05, S3-07

### Goal

在 Invocation Gateway 中落实单进程并发准入、两阶段超时取消和稳定 CapabilityError
到 Run 错误映射。

### Scope

- unlimited/bounded 控制器
- acquire timeout 与 CAPABILITY_BUSY
- execution timeout、cooperative cancel、grace、强制 Task cancel
- 显式 Cancel 与 timeout 区分
- Semaphore 唯一释放路径
- CapabilityError 白名单映射和未知异常收敛

### Forbidden Scope

- 分布式 Semaphore、Worker Lease、跨实例限流
- 重试调度、熔断器或自适应限速
- 修改公开事件结构

### Database Changes

无新增表；写 Invocation internal event 和既有 Run 终态。

### Main Interfaces

```text
CapabilityConcurrencyController
CapabilityExecutionController
CapabilityErrorMapper
```

### Acceptance

- [ ] bounded 上限不被并发竞争突破
- [ ] 等待超时不创建新 Task，并形成 started/failed Invocation
- [ ] 执行超时先协作取消，宽限后才强制取消
- [ ] 显式 Cancel 记录 cancelled 而非 timeout
- [ ] 所有异常和取消路径释放槽位
- [ ] 失败公共消息非空且 Task 默认保持 active

### Test Requirements

- 可控 Barrier/Clock 的并发测试
- 忽略协作取消的 Fake Capability 强制取消测试
- Cancel/完成/timeout 竞态与唯一 Run 终态测试
- 错误 details 不进入公开事件或日志测试

### Regression Scope

- S2.5 Cancel 202、首终态获胜和 Session 单活动 Run 不变
- 慢 SSE 客户端仍不取消 Run

### Definition of Done

并发/超时/取消竞态门禁、Review、负责人验收和双远程同步完成。

---

## S3-09 Operation Ledger 与 ctx.operation

**Status:** TODO

**Dependencies:** S3-04, S3-05, S3-08

### Goal

提供逐业务副作用的稳定幂等键、Ledger 状态机和 Runtime 管理的异步 Operation Context。

### Scope

- 幂等键 canonical 编码与固定测试向量
- `ctx.idempotency_key()`
- `ctx.operation()` / OperationHandle
- pending 创建/复用、succeeded 去重、failed 默认不重试
- 正常退出成功、未知异常保留 pending、确定失败显式标记
- Task/Capability/Invocation 归属校验

### Forbidden Scope

- Runtime 自动识别业务副作用
- 保存业务响应 payload
- 自动重试 failed、Saga、补偿或人工操作界面
- 外部 Provider 专用 Adapter

### Database Changes

使用 `capability_operations`；若 S3-04 的已确认索引无需变化，不新增表。

### Main Interfaces

```text
RunContext.idempotency_key(operation_key)
RunContext.operation(operation_key)
CapabilityOperationContext
CapabilityOperationHandle.mark_failed()
```

### Acceptance

- [ ] 相同 run/invocation/operation 得到相同 key，不同任一维度得到不同 key
- [ ] 崩溃或普通异常后保持 pending
- [ ] 正常执行原子进入 succeeded
- [ ] Capability 显式确定失败才进入 failed
- [ ] succeeded 不重复调用副作用，failed 默认拒绝自动重试
- [ ] 一次 Invocation 的多个 Operation 独立记录

### Test Requirements

- canonical hash 测试向量
- Context Manager 各退出路径单元测试
- 真实 PostgreSQL 并发 get-or-create、崩溃窗口和唯一约束测试
- Fake 外部系统幂等调用次数测试

### Regression Scope

- 无副作用 Capability 不要求创建 Operation
- Operation 内容不进入 Parent、SSE 或业务日志

### Definition of Done

Ledger 真实数据库与崩溃窗口验证、Review、负责人验收和双远程同步完成。

---

## S3-10 Capability 恢复策略与 Checkpoint 对账

**Status:** TODO

**Dependencies:** S3-05, S3-09

### Goal

把 Manifest recovery/side-effect policy 接入 S2.5 恢复器，并保证 Invocation 与
Operation 标识跨进程重启稳定。

### Scope

- automatic + none 自动恢复
- automatic + idempotent 的 open invocation 与 pending Operation 恢复
- automatic + unsafe 加载期拒绝回归
- manual 崩溃恢复安全失败
- started 无终结事件的 invocation_id 复用
- Checkpoint-first 最终消息/Interrupt 投影保持

### Forbidden Scope

- manual recovery API 或 UI
- 新 retry Run、恰好一次承诺
- failed Operation 自动重试、状态迁移、Saga 或补偿

### Database Changes

无新增表；读取 RuntimeEvent、Task 和 Operation，写既有 Run/Event/Message 投影。

### Main Interfaces

```text
CapabilityRecoveryPolicyEvaluator
OpenInvocationResolver
RunCheckpointInspector integration
```

### Acceptance

- [ ] automatic + none 沿用精确 Checkpoint 恢复
- [ ] automatic + idempotent 复用 invocation_id、task_id 和 idempotency_key
- [ ] succeeded Operation 不重复，pending 只用原 key 重试
- [ ] manual 不调用 Capability，形成非空 incomplete + failed
- [ ] manual 专用错误不影响 automatic 路径
- [ ] 恢复次数上限、Interrupt 对账和唯一终态保持 S2.5 语义

### Test Requirements

- 真实 PostgreSQL Checkpointer 的进程重启测试
- 副作用前、外部成功后 ledger 前、ledger 后 checkpoint 前等崩溃窗口
- automatic/manual 参数化测试
- 模型与外部副作用调用次数断言

### Regression Scope

- S2.5 三次恢复上限和 start_checkpoint/input_payload 重放不变
- Run 不新增 retry 类型

### Definition of Done

恢复矩阵与真实崩溃窗口通过、Review、负责人验收并同步两个远程。

---

## S3-11 Regenerate Capability 安全门禁

**Status:** TODO

**Dependencies:** S3-05, S3-06, S3-09, S3-10

### Goal

在创建 regenerate Run 前验证 Capability State 与副作用策略，避免把 Parent Fork
错误当成 Child State Fork。

### Scope

- 从活动分支最新 completed AIMessage 确定原 capability_id
- 只允许 `state_scope=invocation + side_effect_policy=none`
- Registry、Health、权限和 State Schema 提交前复查
- 复用 current Task、生成新 invocation_id 和独立 Child thread
- 不支持组合返回 HTTP 409 `CAPABILITY_REGENERATE_UNSUPPORTED`
- 保持 Parent Checkpoint Fork、request_id 幂等和原 Run API 事务边界

### Forbidden Scope

- Manifest regenerate 扩展字段
- run/session Child Checkpoint Fork
- 带副作用 Capability 的重生成、补偿或重复操作确认
- Regenerate 时重新调用 Router

### Database Changes

无新增表；不支持请求必须在创建 Run、Task、Invocation 和 Operation 前返回。

### Main Interfaces

```text
CapabilityRegeneratePolicy
RegenerateSubmissionValidator integration
CAPABILITY_REGENERATE_UNSUPPORTED
```

### Acceptance

- [ ] invocation + none 可创建 regenerate Run 并复用 current Task
- [ ] 每次合法 Regenerate 使用新 invocation_id 和新 Child thread
- [ ] run/session scope 或 idempotent/unsafe 在 Run 创建前返回 409
- [ ] 拒绝路径不创建 Run、Task、Invocation、Operation 或 Checkpoint
- [ ] 不重新 Router，且 invoke 前权限/健康复查仍执行
- [ ] 旧 Session 无 current Task 时按既定 continue→new 降级

### Test Requirements

- State Scope × Side Effect Policy 参数化门禁测试
- API 409 无持久化副作用测试
- 真实 Parent Checkpoint Fork + invocation thread 隔离集成测试
- request_id 幂等与权限/健康竞态测试

### Regression Scope

- Stage 2 最新 completed 消息限制、Checkpoint Fork 和历史分支不变
- 两个测试 Capability 的既有 Regenerate 可继续工作

### Definition of Done

Regenerate 安全矩阵、真实 Checkpointer 集成、Review、负责人验收和双远程同步完成。

---

## S3-12 迁移 general_chat / en_to_zh 并移除硬编码分支

**Status:** TODO

**Dependencies:** S3-06 ~ S3-11

### Goal

把两个测试能力完整迁移到标准 Capability Runtime，删除正式链路中的固定能力分发。

### Scope

- 两个 entrypoint 工厂、标准 Capability 和 AgentResult 适配
- Registry 驱动 composition root、Router 和 Invocation Gateway
- 删除 Parent/chat/recovery/history 中固定 ID 集合与 if/elif 分发
- general_chat 简短风格、en_to_zh 忠实完整翻译和范围判断保持
- Fake Model、真实百炼 smoke 接线保持
- 测试显式准备 allow/deny 权限

### Forbidden Scope

- 把测试能力宣称为生产能力
- 新增第三个业务 Capability
- 修改业务 Prompt 目标或上下文 10/5 规则
- 热加载、管理 API 或前端能力管理

### Database Changes

无新增表；验收 fixture 显式写权限和 Task 数据。

### Main Interfaces

标准 Manifest/Factory/Capability/AgentResult；Parent 仅调用通用 Invocation Gateway。

### Acceptance

- [ ] 正式代码无两个 ID 的能力分发硬编码
- [ ] 两个能力均从 Manifest 到 Registry 再到 Runtime 执行
- [ ] OUT_OF_SCOPE 有限回流和拒绝零业务副作用不回归
- [ ] 最终完整 AIMessage 仍只由 Parent 保存
- [ ] 三种 state scope 可通过 Manifest 测试变体验证，不复制业务实现
- [ ] 两个测试 Manifest 固定为 invocation + none，既有 Regenerate 通过安全门禁
- [ ] 真实百炼仍仅为显式 smoke

### Test Requirements

- 迁移前全部能力专项等价回归
- Fake Model 端到端普通聊天、翻译、切换、连续和全部拒绝
- 动态 Registry 真实 Checkpointer/Run/Event 集成
- 代码搜索断言固定分发已移除

### Regression Scope

- Stage 1 Cases A~G
- Stage 2 历史、Regenerate、Feedback
- Stage 2.5 Run、SSE、Cancel、Interrupt/Resume、恢复

### Definition of Done

两个测试能力完全通过通用链路，旧分发移除，专项与全量回归、Review、负责人验收、
双远程同步完成。

---

## S3-13 Session 删除与动态 Child Checkpoint 清理

**Status:** TODO

**Dependencies:** S3-05, S3-09, S3-12

### Goal

把 S3 Task、Operation 和动态 Child thread 纳入 S2.5 可重试 Session 硬删除屏障。

### Scope

- 删除前收集 run/task/thread 标识
- contexts → operations → tasks → runtime → checkpoints → session 顺序
- 动态三 scope thread 与遗留 thread 清理
- Redis 继续 best effort
- 中途失败保持 Session 重试锚点

### Forbidden Scope

- 软删除、归档、审计保留或全局数据保留策略
- 删除 Capability Manifest 或 Registry 实例
- 扩张为跨租户数据清理

### Database Changes

无新增表；补充按 Session 查询/删除方法和必要的已确认索引。

### Main Interfaces

```text
SessionCapabilityDataDeleter
DynamicChildThreadCollector
SessionDeletionService integration
```

### Acceptance

- [ ] 活动 Run 先取消并终止
- [ ] 四张 S3 表中该 Session 相关数据全部删除
- [ ] invocation/run/session scope checkpoint 全部删除
- [ ] 遗留 `{session_id}:{capability_id}` checkpoint 兼容清理
- [ ] 任一步失败不提前删除 Session，重试可完成
- [ ] 其他 Session 和 Registry 不受影响

### Test Requirements

- 真实 PostgreSQL + Checkpointer + Redis 集成测试
- 每个删除步骤故障注入和幂等重试测试
- 多 Task、多 Run、多 Capability、多 scope 删除矩阵

### Regression Scope

- S2.5 Interrupt/Event/Run/Feedback/Parent checkpoint 删除
- Redis 不可用不阻塞 PostgreSQL

### Definition of Done

完整删除矩阵、故障重试、Review、负责人验收和双远程同步完成。

---

## S3-14 Stage 3 集成验收 Gate

**Status:** TODO

**Dependencies:** S3-02 ~ S3-13

### Goal

执行完整 S3 Gate，证明 Capability Runtime 可演示、可恢复且没有实现阶段外平台能力。

### Scope

- Manifest / Registry Gate
- Task / State / Checkpoint Gate
- Health / Concurrency / Timeout Gate
- Recovery / Operation Ledger Gate
- Permission / Router / public capability_id Gate
- Regenerate Capability 安全门禁 Gate
- 现有测试 Capability 与 `/chat` 页面 Gate
- Session 删除和 S1/S2/S2.5 全回归
- 文档、范围、构建和双远程一致性验收

### Forbidden Scope

- 在 Gate 中顺手增加新功能或重构
- 真实模型作为默认测试门禁
- 以 mock 替代必须真实验证的 PostgreSQL、Redis 和 Checkpointer 事务

### Database Changes

无；只验证最终 Schema 和升级路径。

### Main Interfaces

不新增接口；验收所有 S3 已实现契约。

### Acceptance

- [ ] Manifest 严格校验、重复隔离、局部故障隔离通过
- [ ] Registry Snapshot、动态 Router 与权限双检通过
- [ ] Task 生命周期、OUT_OF_SCOPE 回滚和三种 State Scope 通过
- [ ] State Schema 兼容与不兼容拒绝通过
- [ ] Regenerate 只允许 invocation + none，其他组合在 Run 创建前 409
- [ ] Health TTL、degraded、并发、timeout 与 Cancel 竞态通过
- [ ] automatic 恢复、manual 安全失败和 Operation Ledger 崩溃窗口通过
- [ ] 两个标准测试 Capability 和第三个协议测试 ID 通过完整产品链路
- [ ] Session 删除覆盖四表和全部 Child checkpoint
- [ ] `/chat` 可观察通用 capability_id，终态仍回读 Parent 历史
- [ ] 默认 Fake Model、真实 PostgreSQL/Redis、Vitest、构建和 Playwright 通过
- [ ] 真实百炼仅作为负责人配置后的可选 smoke
- [ ] Stage 1 / 2 / 2.5 无回归
- [ ] 未实现 S3 非目标
- [ ] README、AGENTS 和全部 docs 与实现一致

### Test Requirements

- 默认 `uv run --locked pytest -q`
- 显式启用真实 PostgreSQL / Redis 的完整后端套件
- `uv lock --check`、`uv pip check`、compileall、build、compose config
- 前端 Vitest、生产 build、系统浏览器 Playwright
- `git diff --check`、Schema/日志/范围静态审计

### Regression Scope

完整 Stage 1、Stage 2、Stage 2.5 产品与技术回归。

### Definition of Done

所有 Gate 有可复核证据，独立 Reviewer 无未解决 Critical/Important，负责人验收后
提交最终文档并将 GitHub/Gitee 推送到同一提交；此前不得进入下一阶段。

---

# Future Backlog（只记录，不实施）

- [ ] 文件上传
- [ ] 文件服务器 / 对象存储
- [ ] 文件解析
- [ ] RAG
- [ ] Capability Admin API
- [ ] 管理平台
- [ ] RBAC
- [ ] Local Capability 动态 mount / unmount
- [ ] DRAINING 优雅卸载
- [ ] Capability 权限管理 API / RBAC
- [ ] Router confidence / 低置信度确认策略
- [ ] Remote Capability
- [ ] 多实例 Runtime / Worker Lease / 分布式任务队列
- [ ] Transactional Outbox
- [ ] Run History 产品界面与独立保留策略
- [ ] 多审批人 / 审批收件箱 / 超时 / 并行中断
- [ ] 高级 SSE 流控与自适应限速
- [ ] 分布式 Trace
- [ ] Observability
- [ ] Evaluation
- [ ] Complex Task Agent
- [ ] Deep Agents
- [ ] 多 Agent 串联 / 并联
- [ ] Context Engineering
- [ ] Router 自动优化
