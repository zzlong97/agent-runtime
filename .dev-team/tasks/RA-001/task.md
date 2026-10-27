# RA-001 远程 Agent 接入与接入成本评估

## 目标

为 AgentRuntime 增加远程 Agent 调用能力；随后把负责人已有但尚未适配的 Agent
接入真实 Runtime 链路，并基于实际改造与验证记录统计接入成本。

## 已批准规划边界

- 本任务是 Stage 3 之后的候选阶段，尚未获得实施优先级和开发授权；当前只完成规划。
- 远程 Agent 通过 A2A 1.0 HTTP+JSON 和 AgentRuntime 强制 Profile，作为普通
  Capability 进入现有 Router、Run、SSE、公共消息与 `/chat`，不建立旁路入口。
- 首个接入对象位于 `D:\workspace\semantic-pattern-mining\semantic-pattern-mining`；
  当前 AgentRuntime 团队不得修改该仓库，对方团队负责 A2A Server 适配。
- 当前团队未来只负责本项目的 Manifest transport、A2A Client/Adapter、持久绑定、
  恢复/取消、文本 Interrupt、页面、Fake A2A 和集成验收。
- 首期只支持 invocation scope、当前 HumanMessage、GetTask 轮询、文本 Artifact；
  不实现远程流式、跨 Run 上下文、文件、动态注册、Workflow 或管理平台。
- Parent/Child 隔离、Parent 公共消息权威源和本地 Run 终态语义保持不变。
- 接入成本必须来自实际接入记录，并区分 Runtime 一次性成本和单 Agent 适配成本。

正式需求见 `docs/requirements.md` 第 9 节，架构见 `docs/architecture.md` 第 17 节，
关键决策见 `docs/decisions.md` D-064～D-069。

## 实施任务拆分（仅规划，不得启动）

1. `RA-DEV-01`：Manifest transport、Agent Card 校验、A2A Client 接口和 Fake Server。
2. `RA-DEV-02`：远程绑定事件、Operation Ledger、Send/Get/Cancel、恢复与错误映射。
3. `RA-DEV-03`：confirmation/text Interrupt、Resume 与 `/chat` 页面适配。
4. `RA-EXT-01`：对方团队完成 semantic-pattern-mining A2A 适配，当前团队只提供契约。
5. `RA-INT-01`：真实联调、取消/崩溃演练、全量回归和接入成本报告。

`RA-DEV-01～03` 可以在未来获批后先基于 Fake A2A 独立推进；真实联调依赖
`RA-EXT-01`。实现前先完成文本 Interrupt 穿透执行控制器和 open Invocation 恢复的
架构探针，未通过不得接入真实 Agent。

## 已关闭的核心问题

- 产品入口：进入现有 Router/聊天链路，不新增独立入口。
- 协议：A2A 1.0 HTTP+JSON；现有私有 `/api/agent/invoke` 不作为正式契约。
- 恢复：稳定 messageId、持久 taskId/contextId、GetTask 与 Operation Ledger。
- 补参：A2A input-required 复用同 Run Interrupt，并新增文本回答类型。
- 取消：必须远程确认终态；结果未知不得宣称 cancelled，Session 删除保留证据。
- 验收：参数齐全、缺参补充、执行中取消、Runtime 重启恢复四条真实场景。

## 当前暂停点

负责人将继续规划另一个能力，再比较实施优先级。在收到“选择 RA-001 并开始实施”
或等价明确授权前，不创建开发子任务、不编码、不联调、不要求对方团队开始改造，也不
提交或推送本规划。

## 项目门禁

规划基线已获负责人同意，但开始门禁仍未满足。未来开发完成后仍需独立审核、负责人
终验、任务提交及 GitHub/Gitee 双远程同步。
