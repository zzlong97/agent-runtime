# UI-001-REV-001 返工验证

2026-10-18，入口为 state_version=6 / 待开发。完整读取审核 0003/0007，按
receiving-code-review 核对 D-047、Cancel API 路由、摘要 Schema 及 App 处理。
反馈成立：Cancel 持久化中间态后异步返回 202，允许很快进入 cancelled；不能要求
浏览器轮询必然捕获短暂中间态。审核两次红灯保留在原 0007 与 0010～0013。

本轮仅修改 `frontend/e2e/chat.spec.js` 综合用例的 Cancel 段：

- 从本次慢速 Run 的真实 202 创建响应取得 run_id/session_id/response_message_id。
- 在点击前监听该 Run 的真实 POST Cancel 响应，严格断言 HTTP 202、
  status=cancel_requested 及三个归属 ID 相同。
- 稳定页面状态严格要求 cancelled，并核对 Run ID；监听该 Session 的真实历史
  GET，严格核对同一 response_message_id 的 AI 为 stopped、正文非空。
- 保留页面“已停止”验证。没有新增 sleep、路由延迟、产品延迟或任意终态兜底，
  没有减少取消覆盖，也没有改产品/API/取消执行逻辑。

命令与环境（工作目录 `D:/workspace/agent_runtime/frontend`）：

```powershell
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
$env:PLAYWRIGHT_E2E_PORT='18775'
$env:PLAYWRIGHT_E2E_RUN_ID='ui001-rework-cancel-20261018-c47e831f'
$env:LOG_LEVEL='ERROR'
$env:PYTHONIOENCODING='utf-8'
npx playwright test -g '真实浏览器完成 S2.5' --timeout=60000 --trace=on --output=test-results/ui001-rework-cancel-20261018-c47e831f
```

退出 0，`1 passed (19.0s)`，用例耗时 11.3s。一次运行通过，没有反复重试到绿。
正常完成、刷新恢复、Resume、失败以及修订 Cancel 段全部实际执行。演示失败场景
输出 RUNTIME_DEMO_FAILURE 是该既有用例的预期过程，不是测试异常。

成功原始 trace 保存为 `0017-rework-cancel-trace.zip`，从未改写的 trace network
及响应正文核对：

| 项目 | 实际结果 |
| --- | --- |
| Run | b79a8e02-7638-4613-8135-e6275a7ee30b |
| Session | 5265fb25-12bc-400e-9166-258ce6f49d4b |
| 公共 AI | 61c90818-ad43-4d66-b3be-25a982df068a |
| POST Cancel | 202 / cancel_requested，三个 ID 全部与创建摘要一致 |
| 页面终态 | cancelled，Run ID 一致 |
| Parent 历史 | 同一 AI，runtime_status=stopped，正文非空 |

16 处产物/相关输入指纹核对仅本次获准测试文件变化，其余全部相同；完整精确版本
见 `0016-rework-versions.json`。ChatWorkspace、CSS、App、MessageList、3 个静态
构建产物均保持原字节；未执行不必要的重建。复用同版 29 项单测、2 项静态测试，
及 0007 已独立通过的其他 5 项浏览器证据。本次只运行受影响综合用例，后续独立
审核统一复跑新测试版本的 6 项；不把本次单例通过写为新版本全套独立审核通过。

执行前确认专用端口 18775 无监听，沿用已核对的唯一测试用户隔离和 Fake Agent
真实 Run/SSE/Parent 链路。afterEach 经正常 Session API 清理本轮测试数据，测试
服务退出后 18775 无监听。没有碰用户当前服务、其他会话、Docker/卷或 DB-001。
`git diff --check -- frontend/e2e/chat.spec.js` 退出 0，仅原有行尾提示。

本轮结论为已处理 UI-001-REV-001 并交回独立复核，不自行验收或扩大修复范围。
