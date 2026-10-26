# DB-001 数据库表注释需求候选

## 1. 目标

为当前 AgentRuntime 自建的 PostgreSQL 业务表补充准确、简洁、可核验的中文表注释，
并把真实数据库中现有但不准确、含义不完整或格式错误的表注释替换为统一口径。

本需求只改变“表级注释”这一元数据，不改变表名、列、约束、索引、外键、数据内容、
业务语义、Stage 边界或已批准架构。

## 2. 已知事实与范围判定

- 当前代码定义 9 张项目业务表：`sessions`、`message_feedback`、`runs`、
  `runtime_events`、`run_interrupts`、`capability_tasks`、
  `capability_task_contexts`、`capability_operations`、
  `user_capability_permissions`。
- 真实 PostgreSQL 的 `public` schema 中存在上述 9 张业务表，以及 LangGraph
  Checkpointer 创建的 4 张依赖内部表：`checkpoints`、`checkpoint_blobs`、
  `checkpoint_writes`、`checkpoint_migrations`。
- 9 张业务表中，4 张当前表注释为空；5 张已有人工注释。已有注释中，`runs`、
  `runtime_events`、`run_interrupts` 的描述与批准职责不完全一致，`runs` 还含有
  `can、celled` 拼写/断句错误。`sessions` 与 `message_feedback` 的现有描述方向基本
  正确，但没有完整表达批准的数据职责，因此一并收敛为下表的精确口径。

## 3. 需求

### DBR-001 业务表范围

本任务必须覆盖第 4 节列出的 9 张项目业务表。表是否纳入范围以项目自建表定义、批准
需求/架构及真实数据库三方核对为准，不能仅以当前是否存在注释判断。

### DBR-002 精确中文注释

每张纳入范围的表必须具有第 4 节给出的单行中文表注释。实现后的数据库注释必须与候选
获批版本逐字一致，不得保留旧注释中的多行状态清单、错别字或超出表职责的表述。

### DBR-003 已有数据库与新建数据库一致

对已经存在业务表的当前 PostgreSQL 实例，执行本任务交付的正常升级或初始化流程后，
必须替换错误旧注释并补齐空注释；对从空数据库按项目正常流程新建的相同业务表，也必须
得到同一组注释。重复执行该流程后，注释结果必须保持不变。

### DBR-004 无业务副作用

本任务不得改动业务数据、表结构、列、约束、索引、外键或权限规则，也不得改变任何
运行时行为。注释处理失败时必须可观察为失败，不能把部分注释成功误报为全部完成。

### DBR-005 排除依赖内部表与其他对象注释

本任务不得为 LangGraph Checkpointer 的 `checkpoints`、`checkpoint_blobs`、
`checkpoint_writes`、`checkpoint_migrations` 添加或修正注释；也不自动扩大到列注释、
索引注释、约束注释、序列、视图、系统 schema 或 Redis 数据结构。

## 4. 业务表注释清单

| 表名 | 现状 | 获批后应使用的精确表注释 |
| --- | --- | --- |
| `sessions` | 已有注释，含义基本正确但需收敛 | 保存 Session 业务元数据；每条记录标识一个固定用户的会话，并作为该会话业务数据与删除流程的根记录。 |
| `message_feedback` | 已有注释，未说明最终值与清理职责 | 保存用户对已完成 AI 回答的最终点赞或点踩值；session_id 用于会话删除时清理。 |
| `runs` | 已有多行注释，职责表述过宽且 `cancelled` 拼写错误 | 保存每次普通消息或重新生成请求的持久执行生命周期、幂等与恢复投影；是 Run 状态的 PostgreSQL 权威数据源。 |
| `runtime_events` | 已有注释，仅描述状态经过，遗漏公开/内部事件及 S3 生命周期 | 保存 Run 的持久公开事件和内部事件，按 Run 序号记录执行、Interrupt、Capability Invocation 与 Task 生命周期投影；不保存瞬时消息增量或完整 AI 消息正文。 |
| `run_interrupts` | 已有注释，误窄化为 Agent 主动暂停，遗漏持久投影与恢复幂等 | 保存 Run 的中断及恢复幂等投影，包括中断状态、恢复输入和恢复请求标识；每个 Run 同时最多一个待处理中断。 |
| `capability_tasks` | 注释为空 | 保存 Capability Task 的生命周期、所属 Session 与 Capability、状态 Schema 版本及最近一次 Run/Invocation 投影；不保存业务载荷。 |
| `capability_task_contexts` | 注释为空 | 保存每个 Session 与 Capability 当前选中的 active Capability Task 指针；没有当前 Task 时不保留记录。 |
| `capability_operations` | 注释为空 | 保存 Capability 外部副作用操作的幂等账本状态，关联 Run、Invocation 和 Task；不保存业务响应载荷。 |
| `user_capability_permissions` | 注释为空 | 保存固定用户对 Capability 的显式允许或拒绝记录；无记录按拒绝处理，记录不随 Session 删除。 |

## 5. 排除项

- 不修改 LangGraph Checkpointer 内部表及其注释。
- 不新增业务表、迁移框架、管理 API、后台页面或新的 Stage 能力。
- 不处理列、索引、约束、序列或视图的注释。
- 不重命名表或字段，不修正与本任务无关的数据，不删除或重置 Docker 卷。
- 不在需求阶段决定注释应写入建表 SQL、独立升级步骤或二者组合；该技术落点由后续
  架构阶段在本需求获批后确定。

## 6. 可观测验收条件

### DBA-001 纳入范围完整

前提：项目代码、批准文档和目标 PostgreSQL 实例均可读取。  
动作：列出 `public` schema 的普通表，并与项目自建表定义核对。  
结果：纳入范围恰为第 4 节的 9 张业务表；4 张 Checkpointer 内部表明确标记为排除项。

### DBA-002 注释逐表精确一致

前提：本任务实现已作用于目标数据库。  
动作：对 9 张业务表使用 PostgreSQL `obj_description(..., 'pg_class')` 或等价只读方式
查询表注释。  
结果：每张表的结果与第 4 节对应文本逐字一致，且不存在 `NULL`、空字符串或仅空白值。

### DBA-003 错误旧注释已替换

前提：目标数据库包含盘点时的 4 条旧注释。  
动作：执行正常升级或初始化流程后重新读取表注释。  
结果：`runs` 不再包含多行状态枚举或 `can、celled`；`runtime_events` 不再只描述
“Run 怎样一步一步走到这个状态”；`run_interrupts` 不再把中断限定为“Agent 主动
暂停”；`sessions` 与 `message_feedback` 也更新为第 4 节的精确口径。

### DBA-004 既有库、新建库和重复执行一致

前提：准备一个包含当前表的既有数据库和一个按项目正常流程创建业务表的空数据库。  
动作：分别执行一次交付流程，并在其中一个数据库重复执行一次。  
结果：三次读取均得到第 4 节同一组注释；重复执行不报错、不产生不同结果。

### DBA-005 排除对象保持不变

前提：记录 4 张 Checkpointer 内部表及其他数据库对象在执行前的注释状态。  
动作：执行本任务交付流程后复查。  
结果：排除对象的注释未因本任务发生变化；没有新增列、索引、约束、序列或视图注释。

### DBA-006 无结构和数据副作用

前提：记录 9 张业务表的列、约束、索引、外键及可核对的业务数据摘要。  
动作：执行本任务交付流程并复查。  
结果：除表级注释外，记录的结构与业务数据均未改变；现有业务测试和与数据库初始化
相关的验证保持通过。

## 7. 负责人待确认

- 确认第 4 节 9 条精确中文表注释作为本任务需求基线。
- 确认 4 张 LangGraph Checkpointer 内部表及列/索引等其他对象注释保持排除。
- 本候选不包含技术落点；批准后再由架构角色确定既有库升级、新建库初始化和幂等验证
  的实现方式。
