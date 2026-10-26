# UI-001 独立开发验收

审核日期：2026-10-18（Asia/Shanghai）。入口：state_version=4、待审核、
current_role=dev-team-reviewer。模式：开发验收。结论：不通过；直接缺陷修复已通过
对应浏览器检查，但当前 UIA-005 的完整浏览器门禁仍失败，不能写为全部通过。

## 审核依据与版本

完整读取 AGENTS、index、project、本任务 task/state/context、开发交接 0001/0002，
定向核对 requirements 4.10/4.11、6.10/6.11，architecture 13.8、14.5 和
decisions D-036/D-040/D-041/D-045/D-051。批准需求与架构 SHA-256 未漂移。
以 `0005-tested-versions.json` 为输入重新计算 7 个产物、9 个相关输入及 2 张开发
截图的 SHA-256，18 处全部匹配。HEAD 仍为
`7b493182d034ea5247f8fc2cb63dd67e1154ea20`，本任务实际版本以文件指纹为准。

独立检查了 ChatWorkspace、MessageList、App 的终态回读/切换/分页调用路径、
CSS 高度边界和已安装 Ant Design X 的 useCompatibleScroll。实际产品修改仅为
ChatWorkspace 在空消息时显示初载 Spin，以及 CSS 的定位/高度/滚动边界；App.jsx
和 MessageList.jsx 保持交付原字节。权威历史替换、Sender 禁用、请求序列隔离及
原有第三方组件均保留。没有发现超出任务的 API/SSE/数据库或 Stage 变化。

## 实际命令与结果

工作目录 `D:/workspace/agent_runtime/frontend`：

```powershell
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
$env:PLAYWRIGHT_E2E_PORT='18773'
$env:PLAYWRIGHT_E2E_RUN_ID='ui001-review-20261018-5b9e4c27'
npm run test:e2e -- --timeout=60000 --output=test-results/ui001-review-20261018-5b9e4c27
```

退出 1。Vite 生产构建成功（6552 modules），生成 index.html、index-Bz0BVo3w.js、
index-B3KNrXbB.css；构建后这 3 个文件的哈希与开发交付完全一致。6 个 Edge 用例中
5 passed、1 failed，耗时 1.3m。通过的 5 项为：

- 桌面 1280×720 与窄屏 390×844 空态；欢迎图标和标题在视口，截图已人工查看，
  保存为 0008/0009。窄屏页面可正常向下滚动到输入框。
- HTTP 202 延迟下重复提交仍只有一个 Session/Run。
- 12 轮多屏会话正常跟随：真实慢速 Run 终态历史 GET 挂起时，原列表 DOM 保持
  连接且数量为 1；权威回读完成后最终回答仍在视口。
- 主动阅读历史：首条消息位置变动小于 2px、列表未卸载；重新生成后活动分支
  消息数正确，最新回答可见。
- 按真实历史 API 限制每页 12 条触发分页，加载后最新消息锚点误差小于 2px；
  切换时显示初载、禁用输入、无旧消息，加载结束后显示目标会话的 2 条消息。

失败项是既有综合 Run/刷新/Resume/失败/Cancel 用例的第 92 行瞬态断言，详细见
UI-001-REV-001。保留第一轮失败后，仅对此用例进行一次有界独立复跑：

```powershell
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
$env:PLAYWRIGHT_E2E_PORT='18774'
$env:PLAYWRIGHT_E2E_RUN_ID='ui001-review-cancel-20261018-17da6f3b'
npx playwright test -g '真实浏览器完成 S2.5' --timeout=60000 --trace=on --output=test-results/ui001-review-cancel-20261018-17da6f3b
```

退出 1，1 failed，用例耗时 15.4s；同一断言再次失败。未修改源码/测试，未重试到
绿。两次 trace 和 error-context 分别保存为 0010～0013，输出目录互相独立。

`git diff --check -- frontend/src/components/ChatWorkspace.jsx frontend/src/styles/app.css
frontend/src/App.test.jsx frontend/e2e/chat.spec.js src/agent_runtime/static/index.html`
退出 0，只有已有 LF/CRLF 提示。29 项前端单测与 2 项静态页面 pytest 使用开发
0002/0005 的同版本有效证据；context 也记录总控恢复后实际复跑通过。审核未重复
执行这些未受版本变化影响的检查。Vite 原有 >500kB 大块警告仍在，未作为本任务扩展。

## UI-001-REV-001：Cancel 回归强制捕获瞬态，导致浏览器门禁重复失败

位置：`frontend/e2e/chat.spec.js:92`。优先级：P2，验收测试问题。

复现为上述两次真实 Edge 命令。用例点击取消后强制要求轮询看到
`cancel_requested`，实际轮询先读到 running、再读到 cancelled，等待 5000ms
超时。两次都已成功到达取消终态；不是取消卡住，也未看到原始滚动缺陷重现。
`git show HEAD:frontend/e2e/chat.spec.js` 确认该断言在本任务前已经存在。

从 trace 的 network 读取原始响应（未改请求/响应或伪造后端结果）：

| 运行 | Cancel API | 后续权威历史 | 页面状态 |
| --- | --- | --- | --- |
| 首轮，Run 2c6f7852-736f-4454-a723-24c4aadc2778 | POST 返回 202，status=cancel_requested；04:56:24.911 发起，395.975ms | 04:56:25.605 发起 GET，100.079ms，AI runtime_status=stopped | cancelled |
| 单例复跑，Run 477e6c41-3108-4787-9670-d080b4660ce5 | POST 返回 202，status=cancel_requested；04:57:59.377 发起，310.149ms | 04:58:00.003 发起 GET，110.011ms，AI runtime_status=stopped | cancelled |

上述时间为 Asia/Shanghai。按异步 Cancel 契约，中间态可以很快结束，DOM 轮询
没有保证必须在它消失前捕获它。证据支持现有测试的时序假设不足，没有证明本次
ChatWorkspace/CSS 产品修复引入 Cancel 回归。

建议责任角色：总控安排开发完善该直接回归的同步方式。验证稳定终态与公开请求
结果；若要验证 cancel_requested 的页面投影，应以确定性门控保留该状态，再放行
终态，避免依赖网络/渲染速度。审核员工没有改测试或产品。复核方法：保留本次证据，
针对修订后的测试及受影响产物重新绑定版本，先验证该用例，再按范围完成 UIA-005
的浏览器回归。当前不能建立“已确认有效”的开发验收检查点。

## 验收映射与限制

| 条件 | 结论 | 依据 |
| --- | --- | --- |
| UIA-001 | 通过 | 同版开发 0001 红灯量测和触发关系；源码加载分支与组件滚动实现独立核对 |
| UIA-002 | 通过 | 列表不卸载、终态历史真实回读、多屏最新回答可见；单测覆盖权威内容替换 |
| UIA-003 | 通过 | 主动阅读锚点、自动跟随、切换初载的实际 Edge 断言 |
| UIA-004 | 部分通过，待修订回归 | 重生成/分页/切换通过；综合用例正常/恢复/Resume/失败步骤完成后，在 Cancel 瞬态断言失败；API 与终态历史本身正确 |
| UIA-005 | 未通过 | 构建及哈希一致；同版单测/静态测试有效，但本轮完整浏览器命令退出 1，独立复跑仍失败 |

隔离与清理：运行前确认 18773、18774 无监听；每轮使用不同唯一 user_id
`stage-two-playwright-<run_id>`。已核对 server 的初末清理、用例 afterEach、
Session 所有权检查及 Coordinator 的 list_active_for_user，只涉及该用户。两轮
用例 afterEach 经正常 API 删除测试 Session；结束后只读 SQL 确认这两个专用用户
均无 Session（SELECT 分组返回 []），18773/18774 均无监听。删除的是本轮临时
测试数据，未涉及用户会话；没有启动/停止用户 Runtime 或操作 Docker/卷。

未触及 DB-001 的源代码、测试、台账或证据。没有提交、推送或负责人终验。
未覆盖所有浏览器引擎、>50 条加载历史跨终态窗口或一般分页策略；用户服务此前
8000 端口被拒绝的环境事实不作为本轮验证对象，不宣称用户现有服务已生效。

出口：`审核中 / current_role=dev-team-controller`，归总控处理 UI-001-REV-001。
负责人终验、任务提交、双远程同步门禁继续待满足。
