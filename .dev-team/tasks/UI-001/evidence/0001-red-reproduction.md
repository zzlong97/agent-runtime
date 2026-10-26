# UI-001 修复前复现

2026-10-17，专用端口 `18761`，浏览器 `msedge`，测试用户
`stage-two-playwright-ui001-red-20261017`。沿用 `tests/e2e/server.py` 的确定性
Runtime Demo（Fake Agent，无真实模型调用），Coordinator 仅扫描该专用用户，
前后及每例清理均仅通过该服务可见 Session 执行，未停止或清理用户 Runtime。

命令（工作目录 `D:/workspace/agent_runtime/frontend`）：

```powershell
npm run build
$env:PLAYWRIGHT_BROWSER_CHANNEL='msedge'
$env:PLAYWRIGHT_E2E_PORT='18761'
$env:PLAYWRIGHT_E2E_RUN_ID='ui001-red-20261017'
npx playwright test -g '多屏会话' --timeout=60000
```

构建退出 0。浏览器测试退出 1，预期失败：`during.listCount` 期望 1、实际 0。
12 个真实持久化轮次后提交 `/demo slow`；浏览器路由只挂起最终历史 GET 的返回，
Run、SSE、事件及历史正文均来自真实 Runtime。MutationObserver 记录原消息列表
是否仍连接文档，并量测滚动容器：

| 时点 | 外层 scrollTop | 外层 scrollHeight | 外层 clientHeight | 内层 clientHeight |
| --- | ---: | ---: | ---: | ---: |
| 流式输出中，用户滚到底部 | 2131 | 2489 | 358 | 2441 |
| 终态后历史 GET 挂起 | 0 | 358 | 358 | 列表已卸载 |
| 权威历史返回 | 0 | 2489 | 358 | 2441 |

内层 scrollHeight=2441、clientHeight=2441，没有有效滚动空间；外层
`.conversation-body` 是实际滚动容器。`during.detached=true`、`listCount=0`。
原消息列表在加载分支被移除时令外层高度收缩到视窗，浏览器将 scrollTop 归零；
重新挂载后位置没有恢复，因此闪烁并显示第一条。

同时读取已安装 `@ant-design/x@2.9.0` 的 `BubbleList.js`、
`style/list.js` 和 `hooks/useCompatibleScroll.js`：其内部倒序滚动容器负责贴底与
主动阅读位置锁定，但项目的自适应高度将滚动转移到外层，使这一既有能力失效。

对应原始实现版本：`frontend/src/App.jsx`、`frontend/src/styles/app.css` 为
`git:29dcca1e71d3b4faa7517fc95e5468ea6a826bb0` 内容；构建静态入口原始 JS 为
`index-CEs-UOgj.js`，CSS 为 `index-CVqfVnZG.css`。新增浏览器测试为本任务工作区
`frontend/e2e/chat.spec.js` 的首轮测试版本。原始 trace 位于忽略目录
`frontend/test-results/chat-多屏会话在流式结束与权威历史回读期间保留消息列表和阅读位置-browser/trace.zip`，
其中量测附件为 `attachments/9914fa230b884862a0c332a7f1b031a1bb7927c7`；后续测试
可能覆盖忽略目录，因此本文件保留完整关键数值。
