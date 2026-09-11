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

# 4. Stage 2：完整聊天产品能力

Stage 2 目标：把 Runtime Demo 升级成可实际使用的聊天后端。

必须实现：

## 4.1 Session 列表

```http
GET /api/v1/chat/sessions
```

## 4.2 Session 改名

```http
PATCH /api/v1/chat/sessions/{session_id}/rename
```

## 4.3 Session 删除

```http
DELETE /api/v1/chat/sessions/{session_id}
```

Stage 2 使用硬删除。

删除时：

```text
存在 active Run
→ 自动 stop
→ 删除 Parent checkpoints
→ 删除相关 Child checkpoints
→ 删除 Session
```

## 4.4 历史消息查询

```http
GET /api/v1/chat/sessions/{session_id}/messages
```

后端负责将 LangGraph checkpoint history 转换为前端可直接消费的消息结构。

不得直接暴露原始 `StateSnapshot`。

## 4.5 单 Session 单 Run

规则：

```text
一个 Session 同一时间最多一个 active Run
```

重复请求：

```text
SESSION_BUSY
```

## 4.6 Stop

```http
POST /api/v1/chat/sessions/{session_id}/stop
```

Stop 只停止当前 Run。

不删除：

- Session
- 已有历史消息
- 已成功持久化状态

已经流式输出的内容保留。

## 4.7 Regenerate

```http
POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate
```

要求：

- 使用 checkpoint history
- 从历史 checkpoint fork 新执行分支
- 不覆盖原回答
- 不删除原 checkpoint
- 第一版不额外维护 `branch_id` / `is_active`

## 4.8 Feedback

```http
POST /api/v1/chat/messages/{message_id}/feedback
```

支持：

```text
like
dislike
cancel
```

同一：

```text
user_id + message_id
```

只保留最终反馈。

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

# 6. Stage 3：Capability Runtime 平台化

Stage 3 目标：从固定两个 Agent 升级为可扩展 Capability Runtime。

必须实现：

## 6.1 Capability Manifest

所有 Capability 必须提供统一能力描述。

至少包含：

```text
capability_id
name
short_description
description
version
examples
non_examples
input_schema
output_schema
```

## 6.2 Capability Registry

提供：

```text
mount()
unmount()
get()
list()
```

Stage 3 Registry 仍使用进程内存。

## 6.3 Local Capability 动态挂载

支持：

```text
Manifest 校验
→ 本地实现加载
→ Registry 注册
```

不支持 Remote Capability。

## 6.4 优雅卸载

```text
ACTIVE
→ DRAINING
→ 拒绝新任务
→ active_runs = 0
→ UNMOUNTED
```

## 6.5 用户 Capability 权限

增加用户与 Capability 的授权关系。

Router 只能看到当前用户被授权的 Capability。

权限每次执行前实时查询数据库。

Stage 3 不做权限缓存。

## 6.6 Router confidence

增加全局：

```text
confidence_threshold
```

仍只输出 Top1，但协议预留 `candidates[]`。

## 6.7 Interrupt / Resume

低置信度时：

```text
interrupt()
```

用户确认后：

```text
Command(resume=...)
```

新增：

```http
POST /api/v1/chat/sessions/{session_id}/resume
```

确认操作不得创建新的 HumanMessage。

## 6.8 动态管理边界

Stage 3 只实现内部：

```text
mount()
unmount()
```

不实现 Admin API 和管理平台。

---

# 7. Future：只规划，不实现

以下能力当前所有阶段均不实现：

## 7.1 文件能力

未来规划：

```text
POST /api/v1/chat/files/upload
```

原则：

```text
文件生命周期 → session_id
文件引用 → HumanMessage attachments
文件解析 → Capability 自己决定
```

当前不实现任何文件存储、解析、摘要、Embedding、RAG。

## 7.2 平台能力

未来：

- Capability Admin API
- 管理平台
- RBAC
- 多租户
- Capability 配置数据库化
- Remote Capability
- Redis
- 多实例 Runtime
- Run History
- Observability
- Evaluation
- Complex Agent
- Deep Agents
- 多 Agent 串联 / 并联
- Context Engineering
