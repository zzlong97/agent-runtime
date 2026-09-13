# Stage 2 完整聊天产品与演示页面设计

**状态：** 已由负责人于 2026-09-13 逐节确认
**范围：** Stage 2
**前置条件：** Stage 1 已完成并通过验收

## 1. 目标

Stage 2 将现有 Agent Runtime 从可验证的后端主链路升级为可实际操作和演示的
聊天产品：

- 补齐 Session 列表、改名、删除和消息历史
- 保证单 Session 单 Run，并提供可等待的 Stop
- 使用 checkpoint fork 实现最新回答 Regenerate
- 为活动分支中的完成回答提供 Feedback
- 交付完整 React 聊天页面，让上述能力可以直接观察和操作

Stage 2 不改变 Parent 公共消息权威源、Child 私有状态隔离、有限
`OUT_OF_SCOPE` 回流和 10/5 上下文规则。

## 2. 已确认产品边界

- 继续使用单用户可信环境，服务端固定 `user_id`
- Regenerate 只允许当前活动分支中最新的 `completed` AIMessage
- Stop 是独立的 `stopped` 终态，不属于失败
- `unsupported`、`incomplete`、`stopped` 在历史中可见，但不进入模型上下文
- 普通历史只展示 Regenerate 后的新活动分支
- Feedback 只允许活动分支中的 `completed` AIMessage
- Session 删除使用幂等硬删除
- `general_chat` 继续尽可能简短回答；`en_to_zh` 继续忠实完整翻译

## 3. 产品页面

页面使用双栏结构：

```text
┌──────────────────┬─────────────────────────────────────┐
│ Session 列表     │ 当前聊天                            │
│ 新建 / 改名      │ 历史消息 / Markdown / Feedback      │
│ 加载更多 / 删除  │ Sender / Stop / Regenerate          │
│                  │ 可折叠 Runtime 状态面板             │
└──────────────────┴─────────────────────────────────────┘
```

状态面板只展示稳定产品字段：

- `session_id`
- `message_id`
- `capability_id`
- 当前是否正在接收流
- 最终状态

提示词、节点名称、StateSnapshot、tasks、checkpoint metadata 和 Child 私有字段
均不得进入前端协议。

## 4. 技术方案选择

采用 React + Vite + Ant Design / Ant Design X：

- Ant Design X 官方提供面向 React 的 AI 对话组件，包括 Bubble、Sender 和
  Conversations，适合直接组合聊天产品界面
- `@ant-design/x-markdown` 提供流式 Markdown 渲染能力
- Vite 使用 `index.html` 作为入口，并将生产代码构建为优化后的独立静态资源
- npm 负责前端依赖，`package-lock.json` 固定实际安装版本

参考：

- [Ant Design X 介绍](https://x.ant.design/docs/react/introduce/)
- [Ant Design X Markdown](https://x.ant.design/x-markdowns/components/)
- [Vite 官方入门](https://vite.dev/guide/)
- [Ant Design X MIT License](https://github.com/ant-design/x/blob/main/packages/x-sdk/LICENSE)

未采用的方案：

- 单 HTML 页面：不利于测试、维护和资源缓存，且不满足已确认的资源拆分要求
- 独立部署前端服务：增加演示部署步骤，Stage 2 没有必要
- 自研聊天组件库：扩大范围并重复实现成熟开源组件已经提供的能力

## 5. API 契约

| 功能 | 接口 | 响应 |
|---|---|---|
| Session 列表 | `GET /api/v1/chat/sessions` | JSON 游标页 |
| Session 改名 | `PATCH /api/v1/chat/sessions/{session_id}/rename` | JSON Session |
| Session 删除 | `DELETE /api/v1/chat/sessions/{session_id}` | HTTP 204 |
| 消息历史 | `GET /api/v1/chat/sessions/{session_id}/messages` | JSON 消息页 |
| 普通聊天 | `POST /api/v1/chat/completions` | SSE |
| Stop | `POST /api/v1/chat/sessions/{session_id}/stop` | JSON |
| Regenerate | `POST /api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate` | SSE |
| Feedback | `POST /api/v1/chat/messages/{message_id}/feedback` | JSON |

Session 列表使用 `updated_at DESC, session_id DESC` 的不透明游标。消息历史
使用 `before=message_id`，默认返回最新页，响应页内保持对话正序。

所有非 DELETE 资源接口对不存在或不属于固定用户的对象返回 404。重复 Run
在建立 SSE 前返回 HTTP 409 `SESSION_BUSY`。Stop 和 Delete 保持幂等。

## 6. 消息与 SSE

Product Message 字段：

```text
message_id
role
content
runtime_status
capability_id
feedback
```

AIMessage 的 `runtime_status`：

| 状态 | 用户可见 | 进入模型上下文 | 可以 Regenerate | 可以 Feedback |
|---|---:|---:|---:|---:|
| completed | 是 | 是 | 仅最新一条 | 是 |
| unsupported | 是 | 否 | 否 | 否 |
| incomplete | 是 | 否 | 否 | 否 |
| stopped | 是 | 否 | 否 | 否 |

Stage 2 SSE 继续只使用 `message`、`error`、`done`。其中：

```text
message → session_id + message_id + capability_id + delta
error   → session_id + code + message + retryable
done    → session_id + message_id + capability_id + status
```

`done.status` 允许 `completed`、`unsupported`、`stopped`、`failed`。

## 7. Active Run 与 Stop

每个 Run 使用进程内 Registry 占用 Session。Graph 在独立 producer task 中执行，
产品事件进入队列，SSE 响应消费队列。

```text
请求校验
→ reserve Session
→ 准备 HumanMessage
→ 启动 producer
→ stream event queue
→ terminal persistence
→ release Session
```

Stop 通过取消 producer 触发统一终态处理，必须等待 `stopped` AIMessage 写入、
SSE 生产端结束以及 Registry 清理。无活动 Run 时立即幂等返回。

浏览器刷新或网络断开不允许遗留无消费者的后台 Run；该 Run 被终止并以
`incomplete` 状态记录，即使尚未产生文本也保留可解释的消息状态。

## 8. Regenerate

Regenerate 先验证目标是当前活动分支最后一个 `completed` AIMessage，再遍历
Parent checkpoint history，定位回答生成前且已经包含 HumanMessage 和路由状态的
checkpoint。

按照 LangGraph 官方 time travel 方式，通过 `update_state` 创建新 checkpoint
分支，再以返回的配置和空输入继续执行。原 checkpoint 不变。

新分支：

- 复用原 HumanMessage 及其 message_id
- 生成新的 AIMessage UUID
- 继续经过 Parent 控制面
- 启动后成为普通历史所见的活动分支
- 失败或停止时不自动回滚

参考：[LangGraph Time Travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)。

## 9. Feedback 与删除

Feedback 存储使用 `user_id + message_id` 唯一键，并保存 `session_id` 作为
删除清理索引。like / dislike 使用 upsert，cancel 删除记录。写入前通过当前
活动历史验证消息资格。

Session 删除顺序：

```text
stop and wait
→ delete child checkpoints
→ delete parent checkpoints
→ delete feedback
→ delete session last
```

Session 行最后删除，使中间步骤失败后仍可使用原 ID 重试。

## 10. 前端资源边界

```text
frontend/
├── index.html
├── package.json
├── package-lock.json
├── vite.config.js
└── src/
    ├── main.jsx
    ├── App.jsx
    ├── components/
    ├── services/
    └── styles/
```

Vite 构建到：

```text
src/agent_runtime/static/
├── index.html
└── assets/
    ├── *.js
    └── *.css
```

构建产物提交到仓库，FastAPI 提供 `/chat` 和 `/assets/*`。因此运行已构建
页面只需要 Python / uv；Node / npm 仅用于开发和重新构建。

## 11. 验收

默认门禁：

- `uv run pytest -q`
- `npm test -- --run`
- `npm run build`
- 静态资源路由测试

可选真实集成：

- PostgreSQL + Fake Model 后端持久化测试
- Playwright 完整浏览器流程
- 百炼 OpenAI 兼容接口 smoke test

默认测试不读取百炼密钥。需要真实百炼 smoke 时，配置文件固定为项目根目录
`.env`，字段为 `DASHSCOPE_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。

## 12. 实施顺序

```text
S2-01 Session 列表
→ S2-02 Session 改名
→ S2-03 单 Session 单 Run
→ S2-04 Stop
→ S2-05 历史 Adapter
→ S2-06 Regenerate
→ S2-07 Feedback
→ S2-08 Session 删除
→ S2-09 完整聊天演示页面
→ S2-10 集成验收
```

每个任务都必须先写失败测试，再实现最小行为，完成当前任务验收后才能进入下一
任务。
