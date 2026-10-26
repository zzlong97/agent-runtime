# UI-001 返工独立复核

2026-10-18，入口 state_version=9 / 待审核。结论：开发验收通过，
`UI-001-REV-001` 已关闭。负责人终验、提交和双远程门禁仍待满足。

本轮读取当前 task/state/context、0004-development、0015/0016；复用上轮
0003/0007 的源码和根因核验。16 个相关输入/产物与返工交付哈希一致，只有综合
E2E Cancel 段相对首轮改变；产品、单测和静态资源均未改变。

## 返工核验

新断言在点击取消前监听同一 slowRun 的真实 POST Cancel 响应，严格校验
202/cancel_requested 及 run_id/session_id/response_message_id；随后核验页面
cancelled 与同一 Run ID，真实权威历史中同一 AI 为非空 stopped。符合 D-047。
没有新增 sleep、路由延迟、产品延迟或任意终态兜底。首轮两次失败证据完整保留。

## 本轮实际验证

工作目录 `D:/workspace/agent_runtime/frontend`，运行前确认 18776 无监听：

```powershell
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
$env:PLAYWRIGHT_E2E_PORT='18776'
$env:PLAYWRIGHT_E2E_RUN_ID='ui001-review-final-20261018-a852f6d1'
$env:LOG_LEVEL='ERROR'
$env:PYTHONIOENCODING='utf-8'
$env:PLAYWRIGHT_JSON_OUTPUT_FILE='test-results/ui001-review-final-20261018-a852f6d1-report.json'
npm run test:e2e -- --timeout=60000 --reporter=list,json --output=test-results/ui001-review-final-20261018-a852f6d1
```

退出 0；生产构建成功，6 passed (1.1m)，0 skipped / unexpected / flaky，全部
retry=0。白名单测试结果保存在 0020，不保存 reporter 的进程环境配置。桌面/窄屏
截图使用 testInfo.outputPath 并另存为 0021/0022；原开发截图未覆盖。终态滚动
原始量测保存为 0023：内层 scrollHeight=2441、clientHeight=310，原列表
detached=false、listCount=1，最终回答仍通过视口断言。

全部 6 项包含：空态；真实 Run 正常/刷新恢复/Resume/失败/Cancel；延迟提交去重；
多屏终态列表保持；主动阅读锚点/重新生成；分页锚点/会话切换初载。
`RUNTIME_DEMO_FAILURE` 日志是综合用例主动失败场景的预期过程。

重建后 index.html、index-Bz0BVo3w.js、index-B3KNrXbB.css 的 3 个哈希均与交付
完全一致。`git diff --check -- frontend/e2e/chat.spec.js` 退出 0，仅已有行尾
提示。29 项单测、2 项静态 pytest 继续复用同版本有效证据，不冒充本轮重跑。

| 验收 | 最终结论与证据 |
| --- | --- |
| UIA-001 | 通过；原 0001 红灯根因与上轮独立源码核验继续有效 |
| UIA-002 | 通过；本轮多屏列表不卸载、最终权威回读和回答视口断言 |
| UIA-003 | 通过；本轮正常跟随、主动阅读位置、切换初载 |
| UIA-004 | 通过；本轮正常/恢复/Resume/失败/Cancel/重生成/分页及既有公开协议 |
| UIA-005 | 通过；同版单测/静态证据、当前构建与 6 项真实 msedge 统一通过，版本见0019 |

隔离用户为 `stage-two-playwright-ui001-review-final-20261018-a852f6d1`，确定性
Runtime Demo/Fake Agent，使用既有真实 PostgreSQL/Redis/Run/SSE/Parent 链路。
清理范围与上轮核对的固定用户所有权规则一致。afterEach 经正常 API 删除本轮测试
Session；结束后只读 SQL 计数为 0（分组结果 []），18776 无监听。仅清理本轮
临时数据，没有操作用户 Runtime、其他会话、Docker/卷或 DB-001。

覆盖限制沿用0007：仅 msedge，不声称所有浏览器或一般历史分页策略均已覆盖；
未验证用户现有服务是否加载此构建。现有大块 JS 警告未扩大处理。没有提交/推送。

保存有效开发验收检查点，状态交回总控为待负责人决策，resume_from=已验收。
