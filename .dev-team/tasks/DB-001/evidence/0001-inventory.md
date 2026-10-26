# DB-001 需求盘点证据

## 1. 证据范围

本证据由需求分析阶段只读获得，覆盖批准文档、项目自建表定义和真实 PostgreSQL
元数据。没有执行 `COMMENT ON`、DDL、DML、Docker 修复、卷操作或业务代码修改。

## 2. 权威文档版本与直接依据

- `docs/requirements.md`：
  `sha256:0d540b18e330aaf6cdce50dc30cab44ff4322c072a4e410d0d23bba6050bad7c`
  - Session：62～85 行；Feedback：523～540 行；Run：631～691 行；
    RuntimeEvent：720～770 行；Interrupt：868～895 行；权限：1072～1112 行；
    Capability Task：1131～1168 行；Operation Ledger 与数据所有权：1235～1302 行。
- `docs/architecture.md`：
  `sha256:b7334cae078c1b09bcf8e5263146cf2c460d43e32b0dc6dfb0aead669e42f5c1`
  - Feedback：675～692 行；Run：779～835 行；RuntimeEvent：859～905 行；
    Interrupt：955～975 行；Task/Invocation：1276～1339 行；Stage 3 数据模型：
    1409～1513 行；Operation Ledger：1591～1636 行；RuntimeEvent 边界：1669～1690 行。
- `docs/decisions.md`：
  `sha256:c919ad658aafd8d18abcabbcb7deac3db44939e8aee9f4d0b363ed416d82e005`
  - D-039、D-043、D-044、D-048、D-055、D-056、D-058、D-059、D-062 均为
    Accepted，是注释职责口径的最高优先级依据。

## 3. 项目自建表定义

只读搜索命令：

```powershell
rg -n --glob '*.py' 'CREATE TABLE(?: IF NOT EXISTS)?' src tests
```

项目源代码中实际命中的业务表定义为：

| 表名 | 定义文件 | 文件版本 |
| --- | --- | --- |
| `sessions` | `src/agent_runtime/sessions/repository.py` | `sha256:5d2e1b670918092c517e388a7bce486522e8eabbdba458a6d30d8b694d65b23a` |
| `message_feedback` | `src/agent_runtime/feedback.py` | `sha256:74ecac2c8febdc75546b7d20f28b8b4b1ade9d576d270a7225ea884673d8c69d` |
| `runs` | `src/agent_runtime/runtime/repository.py` | `sha256:c1f0a0ea891c31f5e7c725bfe54fcf9b9e781c8570e821bf4ed95b0618f50488` |
| `runtime_events` | `src/agent_runtime/runtime/event_repository.py` | `sha256:9145c0c47e486293d85aadf4e9fbef222c73e02dafec69f1b72ec7bc32dd836c` |
| `run_interrupts` | `src/agent_runtime/runtime/interrupts.py` | `sha256:c73d8063e030b97503b925a22e62186fc37ccf233b5aeb1f48a64506c9ad946f` |
| `capability_tasks`、`capability_task_contexts`、`capability_operations`、`user_capability_permissions` | `src/agent_runtime/capabilities/persistence.py` | `sha256:5d4743e6a58e61e2683c9bf721e9fa9ac407cf2976f56f0420ddd4924837137c` |

全仓库搜索 `COMMENT ON TABLE` 与 `obj_description(` 无命中，说明盘点版本的代码尚未
声明或验证表注释。

## 4. 真实 PostgreSQL 只读盘点

`docker compose ps --format json` 显示 `agent-runtime-postgres` 为 `running` 且
`healthy`。使用容器内 `psql` 连接 `agent_runtime` 数据库，仅查询 `pg_class`、
`pg_namespace` 与 `information_schema.columns`。

表与注释查询的等价 SQL：

```sql
SELECT n.nspname AS schema_name,
       c.relname AS table_name,
       obj_description(c.oid, 'pg_class') AS table_comment
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
ORDER BY c.relname;
```

实际结果共 13 张表：

| 表名 | 当前表注释 |
| --- | --- |
| `capability_operations` | `NULL` |
| `capability_task_contexts` | `NULL` |
| `capability_tasks` | `NULL` |
| `checkpoint_blobs` | `NULL` |
| `checkpoint_migrations` | `NULL` |
| `checkpoint_writes` | `NULL` |
| `checkpoints` | `NULL` |
| `message_feedback` | 保存用户对 AI 回答的点赞/点踩结果 |
| `run_interrupts` | Agent 主动暂停执行，等待人类提供额外信息后继续同一个 Run。 |
| `runs` | 多行人工说明；包含“某一次 Agent 执行到底发生了什么”和错误文本 `can、celled` |
| `runtime_events` | Run 是怎样一步一步走到这个状态的。 |
| `sessions` | 保存会话本身的元数据，是整个会话数据的业务根节点 |
| `user_capability_permissions` | `NULL` |

列清单与项目定义一致。其中 `checkpoint_blobs`、`checkpoint_migrations`、
`checkpoint_writes`、`checkpoints` 的字段结构对应 LangGraph Checkpointer 内部存储，
项目源代码也没有自建这些表，因此排除；其余 9 张表全部纳入业务表注释需求。

## 5. 结论与限制

- 业务表注释现状：4 张为空，5 张非空。
- 必须修正的明显错误/缺口：`runs` 职责过宽且有拼写错误；`runtime_events` 遗漏
  durable、public/internal 及 Stage 3 生命周期事件；`run_interrupts` 把来源误窄化为
  Agent 主动暂停。`sessions`、`message_feedback` 方向正确但需补足批准职责。
- 本阶段未对数据库或代码做任何修改，没有验证未来实现的幂等性、既有库升级或空库
  初始化；这些属于需求批准后的架构、开发与独立审核范围。
