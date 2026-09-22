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
Current Stage: Stage 2
Current Task: S2-10
Last Verified Task: S2-09
Last Verified Commit: 4c9922ce9de319b3f9966f76f7e2d1c95cc302f1
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

**Status:** DONE

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
- Result：S2-10 实现与工程验收完成，等待负责人确认后再将 Stage 2 更新为
  `VERIFIED`；禁止提前进入 S3-01

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
