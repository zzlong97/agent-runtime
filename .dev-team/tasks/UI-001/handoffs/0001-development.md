# UI-001 开发交付

本次开发已修复报告缺陷并完成必要验证，交总控安排独立审核；不表示负责人终验。
授权为负责人原文“自行决策修复，无需向我确认具体修复策略”，沿用现有 Stage
2.5/3 批准基线，没有新增业务需求、架构裁决或 Stage。

## 交付范围与根因

真实 Edge 浏览器中复现 12 轮会话：终态回读触发 `messagesLoading`，原先全列表
被 Spin 替换，实际滚动外层高度收缩使 scrollTop 从 2131 归零，重挂载后没有
恢复。另有项目高度设置让 Ant Design X 的内部自动跟随容器没有滚动空间。

- `frontend/src/components/ChatWorkspace.jsx`：加载且没有消息时才替换为 Spin，
  终态背景回读保持消息列表挂载。`App` 的终态权威历史、Sender 禁用和请求隔离
  完整保留。
- `frontend/src/styles/app.css`：让消息列表填满定位边界，给予组件内部滚动容器
  有效高度，复用组件自动跟随和主动阅读位置保护；同时确认窄屏欢迎内容不裁剪。
- `frontend/src/App.test.jsx`：新增 completed、failed、cancelled 三个终态 DOM
  保留和权威替换回归，均已见过红灯。
- `frontend/e2e/chat.spec.js`：新增空会话、真实多屏终态、主动阅读/重新生成、
  历史分页/会话切换浏览器回归。
- `src/agent_runtime/static/`：已通过原有 Vite 流程重建 HTML、带哈希 JS/CSS，
  替换上一次构建的两个旧资源文件。

## 验证与精确版本

精确产物与相关输入指纹：`evidence/0005-tested-versions.json`。
详细命令、退出码、范围与限制：`evidence/0002-development-validation.md`。
根因量测：`evidence/0001-red-reproduction.md`。
最终空会话截图：`evidence/0003-empty-desktop.png`、`0004-empty-mobile.png`。

结果：29 项前端测试通过、生产构建通过、6 项真实 msedge 浏览器测试通过、
2 项 FastAPI 静态页面测试通过，`git diff --check` 通过。浏览器使用隔离端口与
专用用户，真实 Runtime Demo/Fake Agent，不调用收费模型；测试数据和服务已清理。

UIA-001～005 已有对应证据。没有改后端契约、数据库或 DB-001 工作；未覆盖所有
浏览器引擎或一般分页策略，具体限制已在验证证据写明。构建仍有原有大块 JS 警告。
总控只读探查发现当前用户 `127.0.0.1:8000` 服务连接被拒绝；本交付没有启动或
修复服务环境，不宣称已在用户当前服务加载生效，证明范围为生产构建及隔离页面。

## 审核建议与出口

请重点独立核对：终态加载期间保留同一列表但 Sender 继续禁用，权威历史仍替换
本地草稿；内部滚动容器正常贴底且主动阅读不被强制拉走；切换与空会话、分页的
直接 CSS 回归；静态产物与源码版本一致。

出口为 `待审核 / current_role=dev-team-controller`。负责人终验、任务提交、
GitHub/Gitee 双远程同步门禁均保留待满足；本次没有提交或推送。
