# UI-001 开发验证

验证日期：2026-10-17（Asia/Shanghai）。精确源码、测试、构建、基线和输入版本见
`0005-tested-versions.json`。工作区 HEAD 为 `7b493182d034ea5247f8fc2cb63dd67e1154ea20`，
本任务未提交；不能仅凭 HEAD 代表实际被测版本。

## 根因与实现

- UIA-001：真实浏览器修复前量测见 `0001-red-reproduction.md`，确认终态历史 GET
  触发全列表卸载，外层高度从 2489 收缩到 358、scrollTop 从 2131 归零，重新
  挂载后仍为零。读已安装组件源码确认内部倒序滚动能力被项目高度设置绕开。
- UIA-002：`ChatWorkspace` 仅在 `messagesLoading && messages.length === 0` 时
  展示首屏 Spin；已有历史/草稿在权威回读期间持续挂载。保留 `App` 的完整
  `messagesLoading`、Sender 禁用、请求序列隔离及终态 Parent 历史替换。
- UIA-003：`.conversation-body` 提供定位边界，`.message-list` 用 `inset:0` 填满
  可用区域，`Bubble.List` 获得有效高度，直接复用 Ant Design X 既有内部倒序滚动、
  自动跟随与阅读位置锁定。没有新增自动滚到底部的应用脚本。
- 窄屏回归：第一轮百分比高度在窄屏未确定，截图中欢迎标题可见但图标被裁剪；
  补 `.welcome-icon` 视口断言后准确红灯（viewport ratio=0），再改为定位边界内
  填满。最终桌面及 390×844 窄屏欢迎图标、标题均在视口，截图人工复核通过。

## 实际验证

| 命令 / 工作目录 | 退出码 | 结果 |
| --- | ---: | --- |
| `npm test -- src/App.test.jsx -t '终态回读历史期间'` / `frontend`，修复前 | 1 | completed/failed/cancelled 共 3 项均因原 MessageList 脱离文档而失败 |
| `npm test` / `frontend` | 0 | 5 个文件，29 项通过；包含三种终态保留 DOM、保持 Sender 禁用、最终权威内容替换 |
| `npm run test:e2e -- --timeout=60000` / `frontend`，最终构建 | 0 | 生产构建成功，6 项真实 msedge 浏览器测试通过，约 1 分钟 |
| `uv run pytest tests/test_static_chat.py -q` / 项目根，最终构建后 | 0 | 2 项通过，FastAPI `/chat` 和带哈希 JS/CSS 可用，协议内部字段不泄漏 |
| `git diff --check` / 项目根 | 0 | 无空白错误；仅 Git 提示工作区 LF/CRLF 规范 |

浏览器 6 项范围：

1. 空会话桌面及窄屏欢迎图标、标题在视口，并保存截图。
2. 既有真实 Run 正常完成、刷新恢复、Interrupt/Resume、失败、Cancel。
3. 既有 HTTP 202 延迟时重复提交仅一个 Session/Run。
4. 12 轮多屏会话，流式结束与挂起的历史 GET 期间列表原 DOM 保持连接；最终回答
   留在视口，无闪烁跳回首条；最终内容经过真实持久化历史回读。
5. 用户在流式中主动滚到早期历史，结束回读保持首条可视锚点误差小于 2px；
   重新生成完成后活动分支消息数正确、最新回答可见且列表没有卸载。
6. 按真实历史 API 限制为每页 12 条以触发分页，加载更早消息后已有最新消息锚点
   误差小于 2px；切换另一个 Session 的活动 Run 查询挂起时显示首屏加载、输入
   禁用、没有上一会话内容，完成后显示正确新会话。

## 隔离、清理与限制

最终浏览器环境：专用端口 18761，唯一用户
`stage-two-playwright-ui001-final-20261017`。沿用现有开发 Demo 的确定性 Fake Agent，
真实 Run、PostgreSQL、Redis、SSE、Parent 历史均参与链路，没有真实模型请求。
Coordinator 的 `list_active_for_user`、API 的用户检查及前后 Session 清理均已核对。
删除仅涉及本轮专用测试用户创建的测试 Session，已通过正常 Session 删除链路清理；
服务退出后 18761 没有监听。没有停止用户 Runtime、清理其他用户或操作 Docker 内部
文件/命名卷。

未改 API/SSE/后端运行语义、数据库或批准需求/架构。没有覆盖所有浏览器引擎、
大于 50 条已加载历史的跨终态窗口维护或一般分页策略；本次只验证直接 CSS/加载
行为影响，不扩大历史协议。现有生产 JS 大块警告（约 1.15 MB）及 NO_COLOR /
FORCE_COLOR 环境提示不是测试失败，本次未扩大为打包优化任务。

DB-001 的已有实现、测试、状态和证据均未编辑，未自行提交、推送或终验。

总控另做当前用户服务的只读 HTTP 探查，`127.0.0.1:8000` 连接被拒绝；没有启动、
停止或修复该服务。因此本交付证明生产构建与隔离端口页面行为，不宣称已在用户
当前服务中加载生效。此为使用时需启动现有服务并刷新页面的环境事实，不阻塞源码
修复的技术审核。
