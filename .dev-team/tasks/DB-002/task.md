# DB-002 补齐项目自有表字段中文注释并复核R07

## 范围与依据

负责人2026-10-29明确“LangChain生态表无需注释，补充说明，自己设计的表才需要注释”，
见RA-INT-01/handoffs/0010-数据库注释范围澄清与纠正授权.md，当前工程规范为AGENTS第10节。
这是已有注释纠正范围的收窄，无业务/架构设计变更；不重开DB-001既有批准。

补齐八张自有表69列的中文数据库注释：sessions、message_feedback、runs、runtime_events、
capability_tasks、capability_task_contexts、capability_operations、user_capability_permissions。
九张自有表已有表注释与run_interrupts十列正确注释保留。精确缺失字段见
RA-INT-01/evidence/独立审核验证.json 的 fresh_checks.local_postgres.metadata.missing_column_names_by_table。
LangGraph四张checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations及字段排除，
已有框架注释不修改。运行数据属于框架相关不等于表由框架设计。

## 最小实现与验收

- 在正常Repository setup初始化/存量升级路径执行COMMENT，中文准确说明字段业务含义和边界。
- 先补能暴露69列缺口及排除边界的真实元数据验证，再最小实现；覆盖新库、既有非空库、
  重复setup、失败事务回滚；核对表/列结构、索引、约束、权限与业务数据摘要不变。
- 保留原九条精确表注释断言。旧DB001测试的列注释排除规则只对项目自有列按新范围调整，
  框架表/列、索引/约束注释仍保持不变。不得只检查SQL字符串、ORM或docstring。
- 只使用项目Compose中的PostgreSQL以及专用DB002隔离数据库；允许在既有RA-INT隔离库
  agent_runtime_ra_int01_a9549d9c运行相同正常setup补齐注释，须先保存只读元数据/摘要，
  留存真实补齐后的元数据与不变性证据；不得读取或写出用户正文、Checkpoint正文或凭据。
- 不增删业务表/字段，不更改数据、权限、Run/Task/恢复行为，不改运行中的对方服务/源码，
  不重跑真实Agent或创建付费Task，不重置Docker/命名卷，不改MI内容或无关脏文件。

开发只持有本任务state写入权，交付待审核并归还总控。开发不改AGENTS/全局基线、其他任务
状态或历史交接，不自行验收、提交或推送。审核独立复核真实元数据后由总控处理RA-INT-R07。
负责人终验、提交和双远程同步沿用项目门禁。
