# MI-001 候选规划交接

kind：架构交付。输入 state_version=5；交付后等待负责人验收完整规划，不开始开发。

## 已确认的方向

负责人逐项选择：

1. “A. 先做应用层多实例，基础设施高可用另设阶段（推荐）”。
2. “A. 多活，共同分担任务并自动故障接管（推荐）”。
3. “A. 采用统一入口和内部自治，按服务契约验收（推荐）”。
4. “A. 采用 30 秒接管目标（推荐）”；问题已明确依赖/容量正常及业务完成时间另计。
5. “A. 采用 PostgreSQL 调度、同构副本（推荐）”。

这些确认只对应方向，不替代本次新生成完整规划的最终验收，也不是开始实施授权。

## 交付版本

- docs/requirements.md 第 10 节，MI-AC-01～11：
  sha256:4c4737947a6b2dcafc04a94b62301f7b47e3614b42dea103ed3a85f965d8ff9b。
- docs/architecture.md 第 18 节，含 MI-DEV-01～05：
  sha256:3f4d0231880ec42975248165ad2cd0fde4380ffea17d3e80939b83f6800d60da。
- docs/decisions.md D-070～D-074；方向 Accepted、技术细节 Proposed：
  sha256:2157ebb51b5a309886e96759bf35b41f39d686b684acd1d47db51b78df6ef8e2。
- 本任务 task.md 只维护定位、范围、拆分引用和暂停点：
  sha256:3febabd400ec26f8085b5ed9fb0d75e28b1db4d95ab8dcde86f7ed3363baa315。

## 验证与限制

- 对照真实 Coordinator 的进程内任务/锁、Sequencer 的缓存号段、无执行权 Guard 的
  PostgreSQL saver、进程内删除集合和 Capability Semaphore 制定变更；没有把现有
  单进程实现误称为已支持多实例。
- 对照 PostgreSQL 17 官方 SELECT 和连接/事务超时文档核对领取与短事务原语。
- Markdown 代码围栏平衡，无 TODO/TBD/FIXME 等占位；MI-AC-01～11 均存在，技术
  拆分覆盖对应验收。git diff --check 对三个正文通过，仅有 LF/CRLF 提示。
- 校验每个新增 MI 节之前的完整字节前缀，其 SHA256 与 RA-001 已登记的三个全局
  基线 SHA256 完全相同。没有修改 RA-001 原批准内容或擅自更新其批准版本。
- 远程规范与真实远程实现区分，MI 核心不硬依赖 RA；真实双端联测才依赖 RA 与
  对方适配。未修改 semantic-pattern-mining。
- 本轮仅编写规划和任务记录；没有产品代码、运行配置、数据库、服务、真实故障演练、
  提交或推送。本记录不作为实现可用或 30 秒接管已达成的证据。

## 下一步

负责人验收完整 MI-001 规划后再比较 MI-001 与 RA-001 的实施优先级。未明确选定并
授权开始前，不创建开发子任务，不实施任何候选阶段。
