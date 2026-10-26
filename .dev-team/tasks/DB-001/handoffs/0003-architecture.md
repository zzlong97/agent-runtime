# DB-001 架构交付

- kind：架构候选、技术验收与最小开发拆解交接。
- 输入状态：`state_version=4`、`架构设计中`、写入角色 `dev-team-architect`。
- 已批准需求：`requirements.md`，
  `sha256:c84d441897d4412c7b72abbe9ea46e9cae66edcc1e6e8747a3d82ac345e2a2b4`，
  批准源 `handoffs/0002-controller-approval.md`。实际文件再次核对一致。
- 架构正文：`.dev-team/tasks/DB-001/architecture.md`，
  `sha256:0018229383ac83955f932ec617706717a3e67f011ac6b3b4a5044bfa48064d7e`。
- 静态证据：`evidence/0002-architecture-validation.md`，
  `sha256:2d662f052bcd12340806c5591ba85744dddb3e5b8e769e5664443cfad5d3b1f4`。
- 操作输入：`evidence/0002-architecture-operation.json`，
  `sha256:c4448c737f6bf64e812a1ec18ce65a2bb97c1216b50811d72a375067514128bc`。
- 操作键：`sha256:0acf0dfc5a2a3e313f8ad14289d69542e79342272e043d1b78fc06baf64944bd`；
  由需求版本、架构候选版本与相关文档/源码快照版本计算，不含 state_version 或时间。

## 技术决定

在 6 个既有源文件、9 个表所属 setup 中，各加入一条批准文字的固定 COMMENT SQL，
在既有事务 commit 前执行；新库、旧库、已有错误/空注释和重跑使用同一路径。保持
逐 Repository 事务边界，部分表已经提交但后续失败时整个初始化仍必须失败，修复后
重跑收敛；不增加迁移框架。补齐 Session/Feedback 的初始化中文日志并原样上抛失败，
其他 Repository 沿用既有失败日志和错误码。

最小开发单元为 `DB-001-DEV-01`，含 9 条注释接入、相称测试、隔离空库/既有样本/
重跑验证、当前真实业务库的实际更新及前后结构/数据/排除对象注释快照。技术验收
DBT-001～007 全部映射 DBA-001～006。没有新增产品能力或改变运行生命周期。

## 验证与未决

输入 12 文件快照、批准需求指纹、9 表/技术验收静态映射及 `git diff --check` 通过。
这不是未来实现的测试通过证明；没有运行业务测试、连接或修改真实数据库、提交/推送。
需求中的“4 条旧注释”按同文详细清单解释为 5 条，批准需求未改字节。

没有需改变 Stage/选型的重大技术取舍。待负责人批准本文引用的精确架构候选版本后，
总控才可交给开发角色；Reviewer 通过后仍须负责人终验，项目提交/同步门禁继续保留。

出口：`架构待确认`、`state_version=5`，写入权归还 `dev-team-controller`；
下一角色建议为负责人架构确认后的 `dev-team-developer`。
