# DB-001 表注释维护架构候选

## 1. 输入基线与适用边界

本候选只实现已批准需求 `.dev-team/tasks/DB-001/requirements.md`，版本
`sha256:c84d441897d4412c7b72abbe9ea46e9cae66edcc1e6e8747a3d82ac345e2a2b4`；
批准来源为 `handoffs/0002-controller-approval.md`。9 条注释以该需求第 4 节为唯一
文字依据，不在本文另拟文案。需求 DBA-003 前提中的“4 条旧注释”是数量笔误；同文
第 2、4 节和盘点证据均为 5 条，且该验收结果也逐一列出 5 张表。本方案据此验证全部
5 条旧注释和 4 条空注释，保留批准需求的原始字节。

沿用 `docs/architecture.md` 15.7 的 Repository 自建表及幂等 `setup()` 基线。
Session/Feedback、Run/Event/Interrupt、Task/Context/Operation/Permission 的职责
继续遵循已有权威文档及 D-043、D-044、D-048、D-055、D-056、D-058、D-059、D-062。
本维护任务不进入新 Stage，不修改上述决策，也不解除 TEAM-001 或本任务的提交、
双远程同步和负责人终验门禁。

## 2. 既有能力与选定落点

| 能力 | 已定义 | 已接入 | 本次调查已确认 | 尚待开发验证 |
| --- | --- | --- | --- | --- |
| Repository 幂等建表/索引 | 架构 15.7 | 9 表分属 9 个 setup，分布于 6 个源文件 | CREATE IF NOT EXISTS，显式 commit | 增加注释后的实际执行 |
| 正常初始化顺序 | 已有 composition root | `chat.open_chat_service()` 顺序调用 | Run setup 内调用 Interrupt setup，外层无吞错 | 新增注释失败不能进入服务就绪 |
| 持久化连接 | 已有 `open_database_connection()` | 每个 setup 独立连接 | 未启用 autocommit，关闭连接；setup 末尾提交 | COMMENT 在提交前执行、失败回滚 |
| 表注释 | 本需求已批准精确文字 | 尚未接入 | 源代码没有 COMMENT 声明 | 真实 PostgreSQL 精确值及重复执行 |

选择：在每张表所属模块声明一条固定 `COMMENT ON TABLE <表名> IS '<批准文字>'`
SQL 常量，并加入该表既有 `setup()` 的事务，在建表和已有索引语句之后、既有
`commit()` 之前执行。Capability 四个 setup 直接向已有 `_setup_schema()` 的
`statements` 加入各自常量。所有表名和注释均为开发时审核的固定 DDL 文本，不接受
请求参数、配置、用户文字或动态拼接；与现有静态建表 SQL 使用同一关系解析方式，
当前部署目标仍为 `public`，不引入新 schema/search_path 配置。

| 表 | 所属文件 / setup |
| --- | --- |
| `sessions` | `src/agent_runtime/sessions/repository.py` / `PostgresSessionRepository.setup` |
| `message_feedback` | `src/agent_runtime/feedback.py` / `PostgresFeedbackStore.setup` |
| `runs` | `src/agent_runtime/runtime/repository.py` / `PostgresRunRepository.setup` |
| `runtime_events` | `src/agent_runtime/runtime/event_repository.py` / `PostgresRuntimeEventRepository.setup` |
| `run_interrupts` | `src/agent_runtime/runtime/interrupts.py` / `PostgresRunInterruptRepository.setup` |
| `capability_tasks` | `src/agent_runtime/capabilities/persistence.py` / `CapabilityTaskRepository.setup` |
| `capability_task_contexts` | 同上 / `CapabilityTaskContextRepository.setup` |
| `capability_operations` | 同上 / `CapabilityOperationRepository.setup` |
| `user_capability_permissions` | 同上 / `UserCapabilityPermissionRepository.setup` |

每次 setup 都无条件重申本表注释，不先判断表是否新建、注释是否 NULL，也不按旧文字
匹配后替换。因此非空错误注释、空注释和正确注释走相同路径。空库先创建表再写注释；
既有库的 CREATE IF NOT EXISTS 保留原表，再写相同注释；重复执行得到相同元数据。
固定中文文本写入 PostgreSQL 表级元数据，不进入 Parent/Child State、RuntimeEvent
或 SSE。

不选“仅交付一次性 SQL”：后续新库会再次缺少注释。单独迁移框架也没有实际必要，
且与明确排除迁移框架的需求冲突。无需新增 Service、Registry 或通用注释管理抽象。

## 3. 事务、失败与可观察性

- 事务边界沿用每个 Repository setup；该次建表/索引与本表注释一起提交。COMMENT
  失败时不得提交该事务，不得吞异常或继续报告初始化成功。
- 不新增跨 9 个 Repository 的全局事务。前面已提交的表注释可能保留，后续表失败时
  整体初始化必须明确失败；故障修复后重跑全套 setup 收敛。Run 与 Interrupt 当前
  就是两个事务，保留这个边界，不宣称 9 表原子升级。
- Run、Event、Interrupt 和 Capability 既有初始化异常包装、中文业务日志及错误码
  保持不变；增加 COMMENT 后自然进入同一失败分支。
- Session 与 Feedback setup 增加中文开始、完成和失败业务日志，统一调用
  `log_business_event`。失败日志使用固定 `SESSION_PERSISTENCE_SETUP_FAILED`、
  `MESSAGE_FEEDBACK_SETUP_FAILED`、`error_type`、`duration_ms`，随后原样上抛，
  不改变原异常类型或新增 API 错误契约。完成日志必须在成功 commit 之后。
- 所有新增日志只带固定事件、必要表名、状态、错误类型/错误码、耗时；本路径没有
  session/message/capability 实例时不虚构 ID，不记录 SQL/连接串/异常原文/消息正文。
- `open_chat_service()` 当前逐项 await，FastAPI lifespan 当前记录“应用启动失败”
  并上抛；保持该链路。直接执行维护验证时也只在全套 setup 和逐表复核全部成功后
  返回成功，失败退出非零并记录未完成项。
- 数据库对象 owner 不足、连接错误或 COMMENT 被拒绝时使用上述已有传播路径；
  不自动提升权限、不修改 owner/ACL，不修复 Docker Desktop。

## 4. 实际数据库执行与验证方案

开发阶段先在项目 compose 的 PostgreSQL 服务内建立专用临时测试数据库，使用唯一
名称并限定本任务前缀。隔离库使用现有 Settings/Repository setup 和 Windows
psycopg 兼容事件循环，不使用替代建表 SQL。连接串只在内存中派生，不输出、不写入
证据。完成后仅清理本次创建且已核对名称的临时库，不触碰业务库或命名卷。

集成验证覆盖以下三种场景：

1. 空库调用全部业务 Repository setup：Session → Feedback → Run（含 Interrupt）
   → RuntimeEvent → Task → Context → Operation → Permission；9 条注释逐字核对。
2. 同一隔离库准备“既有库”样本：5 张旧注释、4 张 NULL；保留有效业务 fixture，
   再运行相同流程，核对覆盖结果及数据/结构快照。旧注释样本只放隔离测试库。
3. 再运行一次相同流程，比较 9 条注释及不变性快照。必要时用真实
   `AsyncPostgresSaver.setup()` 准备四张依赖内部表，只记录其既有注释，不对它们
   发出本任务 COMMENT；业务 setup 前后排除对象的注释必须一致。

以上验证通过后，开发交付应把同一流程作用于负责人原先盘点的当前业务数据库，
核对数据库目标后执行全套业务 Repository setup，再逐表读取
`obj_description(c.oid, 'pg_class')`。单独调用业务 setup 即可完成维护，不为此
启动 Coordinator、模型、Checkpointer 升级或完整聊天服务，避免恢复/执行活动 Run
混入验证结果。当前库只允许批准的 9 条注释更新，不植入测试旧注释或测试业务行。

当前库核验应处于无业务写入的空闲窗口：先只读确认活动 Run 与数据库活动；若存在
并发业务写入，不强制停止 Run 或服务，先报告并等待可核验窗口。若前后摘要不同，
不能将并发修改推定为本任务无副作用，也不能忽略差异宣告成功，应调查并重新核验。

执行前后至少保存如下可比较快照，仅输出元数据及摘要：

- 9 张业务表：列名、顺序、类型、可空、默认值、identity/generated；约束（含外键）
  定义/验证与延迟属性；索引定义和有效性；表 owner/ACL。既有库前后必须相同。
- 9 表业务数据：逐表行数，以及数据库内对 `to_jsonb(row)::text` 计算行哈希后、
  按哈希排序聚合的确定性摘要。只导出最终摘要及行数，不导出正文或逐行内容；同次
  前后保持相同数据库会话格式设置。隔离样本应包含非空数据，避免只证明空表不变。
- 排除对象：4 张 Checkpointer 表的表注释，以及所有其他已存在对象的注释映射
  （含列/索引/约束等，排除允许修改的 9 个表级条目）。执行前后必须相同；没有新增
  范围外注释。空库建表导致的正常结构新增不套用“前后结构不变”，在表建成并准备
  既有样本后建立不变性基准。

Docker 不可用或无法连接时只报告实际错误；临时库或真实库核验缺失就标记待验证，
不能用 FakeConnection 测试代替 PostgreSQL 元数据证据。

## 5. 技术验收与业务验收映射

| 技术验收 | 对应业务验收 | 可核验证据 |
| --- | --- | --- |
| DBT-001 每表所属 setup 在同一事务、commit 前执行固定 COMMENT，恰好覆盖 9 表 | DBA-001、002 | 代码差异、FakeConnection 调用记录，固定预期文本与批准需求比对 |
| DBT-002 从空库正常 setup、5 旧注释+4 NULL 既有样本、再次 setup 得到同一精确注释 | DBA-002、003、004 | 隔离 PostgreSQL 集成测试和逐表断言，不从实现常量生成预期值 |
| DBT-003 对 COMMENT 执行/commit 注入失败，不出现成功日志、异常上抛、服务不能就绪；故障解除重跑成功 | DBR-004、DBA-004 | 单元失败路径及既有 lifespan 传播测试；隔离库用事务内有效 COMMENT 后故意无效语句证明未提交注释被回滚 |
| DBT-004 不增加范围外 COMMENT；依赖表和其他对象既有注释不变 | DBA-001、005 | 静态 9 表白名单与真实元数据快照差异 |
| DBT-005 既有样本和当前库的列/约束/索引/外键/ACL/数据摘要相等 | DBA-006 | 执行前后快照及差异判定；实际代码只有增加表级 COMMENT 和相称日志/docstring |
| DBT-006 既有相关业务测试通过，新增日志遵守统一脱敏约束 | DBA-006、AGENTS 20/21 | 受影响 Repository、初始化/生命周期测试及 PostgreSQL 结果；日志不得出现故障注入敏感标记 |
| DBT-007 当前业务库 9 条注释已实际更新，并给出完整/部分失败的真实结果 | DBA-002～006 | 当前库只读前后快照、setup 命令退出结果及注释查询，不以隔离库通过替代 |

测试规模与风险相称：更新受影响的 setup 顺序/语句数断言，增加 PostgreSQL 对真实
COMMENT 语义、重复执行、不变性和事务失败的验证；不为固定字符串新增产品逻辑。
自动化使用 FakeConnection/Fake Model，不调用真实百炼。先运行受影响单元/初始化
测试及新的 PostgreSQL 集成用例；有新增失败或跨模块疑点才扩大回归范围。Skip 不是
PostgreSQL 验证通过，命令、退出码、通过/失败/跳过数及代码版本均写入本任务证据。

## 6. 最小开发拆解

只有一个开发单元 `DB-001-DEV-01`，不另建业务 Task，也不安排其他员工或排期。

| 字段 | 内容 |
| --- | --- |
| 依赖 | 本需求已批准；本候选 architecture.md 的精确版本经负责人批准；开发写入权由总控交接 |
| 范围 | 6 个既有源文件中 9 条固定注释接入所属 setup；Session/Feedback 初始化中文日志；相关 docstring；相称单元/集成测试；当前数据库实际应用与证据 |
| 非目标 | 列/索引/约束/依赖表注释，修改 DDL 结构/数据/权限，迁移框架、新管理命令/API、新 Stage、顺手重构、真实模型调用、提交或推送 |
| 源码预期 | 第 2 节列出的 6 个文件；无需改 composition root、连接生命周期或运行控制链路 |
| 测试预期 | 更新 `tests/sessions/test_repository.py`、`tests/test_feedback_store.py`、`tests/capabilities/test_persistence_repositories.py` 及实际受影响的 runtime 初始化测试；新增 `tests/persistence/test_table_comments.py`、`tests/persistence/test_table_comments_postgres_integration.py` 或等价聚合测试文件，避免重复测试 |
| 证据预期 | 本任务 evidence 下开发版本、命令与结果、当前库和隔离库前后快照/摘要及差异结论；开发交接与合法 state 更新 |
| 验收 | DBA-001～006 和 DBT-001～007 全部满足；明确隔离库/当前库实际核验结果；无范围外差异 |
| 后续 | 开发交还总控，由独立 Reviewer 验证；Reviewer 通过后仍需负责人终验，提交/双远程同步由总控按授权处理 |

## 7. 候选结论

技术落点不存在需要新选型的重大取舍。待负责人批准的是本候选的 setup 注释写入、
既有逐 Repository 事务边界、失败传播及对应验证/开发范围。本架构阶段只完成代码和
文档只读调查、静态映射验证；没有执行 COMMENT、业务测试、数据库写入、提交或推送。
