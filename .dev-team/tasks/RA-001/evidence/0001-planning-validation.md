# RA-001 规划验证证据

## 验证范围

- AgentRuntime 当前 Manifest、Registry、Capability 契约、Run/Interrupt/RuntimeEvent、
  Operation Ledger、恢复与 `/chat` 页面边界。
- 远程目标 `D:\workspace\semantic-pattern-mining\semantic-pattern-mining\code` 的现有
  HTTP 接口、thread_id、needs_input、健康检查和业务运行持久化。
- A2A 官方 1.0 规范中的 Agent Card、HTTP+JSON、SendMessage、GetTask、CancelTask、
  input-required、Task 和 Artifact 语义。

## 只读盘点结论

- AgentRuntime 当前 Capability 协议是进程内 Python 工厂与对象调用，不能直接充当
  独立部署 Agent 的远程协议。
- 目标 Agent 当前只有同步 `POST /api/agent/invoke`、`GET /health`、thread_id 多轮和
  completed/needs_input 返回；没有 A2A Agent Card、持久 A2A Task 查询、Cancel 或
  messageId 幂等契约。
- 目标 Agent 代码当前位于未跟踪工作区，读取时 HEAD 为
  `198d8c6087e10c12858f63261235e3b1e8fb9f9f`；本任务没有修改该仓库。未来计算接入
  成本前必须由对方团队建立可核对的实际基线，不能只使用当前 HEAD 推断代码差异。
- AgentRuntime Resume API 已接受通用对象，但页面只提供 confirmation 的确认/拒绝；
  目标 Agent 缺少日期时需要 text Interrupt，因此页面和关闭 Schema 都必须扩展。
- 远程受理与本地绑定之间存在跨服务原子间隙，必须使用稳定 messageId 幂等重送；
  结果未知时 Operation 保持 pending，Run 也必须保持非终态并继续阻塞 Session。
- S2.5 本地恢复 3 次上限不能强制终结未确认远程 Task；候选架构已明确远程 Run 的
  recovery_attempts 可超过 3，并继续有界退避查询。

## 规划验证

规划正文版本：

```text
docs/requirements.md sha256:0ebd7900c8185332a5cd94a719ff5f8c3c496339f8ca8ebd4c48700be64fbe5c
docs/architecture.md sha256:8491d719d62cad86b00199e442ba4f39a9870054e8c406815eec35024336cd79
docs/decisions.md sha256:04cab938fad35d7a7f66b54ff5dcf66d8d74073da55855c5cde81ff537427f8c
.dev-team/tasks/RA-001/task.md sha256:02d24d018a0c46bec3777d27bffe7aa4931734f8382a72e8cabf0f81887694e2
```

执行的文档验证：

- 四个 Markdown 文件代码围栏均为偶数，标题可定位。
- `git diff --check -- docs/requirements.md docs/architecture.md docs/decisions.md` 通过；
  只有 Git 的 LF/CRLF 提示，无空白错误。
- 需求 RA-AC-01～10、架构 17.1～17.12、决策 D-064～D-069 与任务拆分互相引用一致。
- 没有修改 README、AGENTS、产品代码、测试代码、数据库、远程 Agent、提交或远程分支。

本任务是规划交付，没有伪造业务测试或端到端结果。正式实施仍需新的开始授权、开发、
独立审核、负责人终验、提交和双远程同步。

## 外部依据

- A2A 官方规范：<https://github.com/a2aproject/A2A/blob/main/docs/specification.md>

