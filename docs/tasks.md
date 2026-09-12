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
Current Task: S1-10
Last Verified Task: S1-09
Last Verified Commit: ec48d1503ba4521acd5d858aa66e58a63179e174
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

**Status:** DONE

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
- Result：S1-10 实现和验证完成，等待负责人验收；Stage 1 尚未最终标记
  `VERIFIED`，未进入 Stage 2

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
