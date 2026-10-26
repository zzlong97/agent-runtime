# DB-001 独立开发验收证据

- 模式：开发验收；审核员工 `dev-team-reviewer`，从 state v11 / 待审核接管。
- 日期：2026-10-17，Asia/Shanghai。
- 结论：通过。未发现批准范围内的阻塞问题，DBA-001～006、DBT-001～007 均有有效证据。
- 批准需求：`sha256:c84d441897d4412c7b72abbe9ea46e9cae66edcc1e6e8747a3d82ac345e2a2b4`。
- 批准架构：`sha256:0018229383ac83955f932ec617706717a3e67f011ac6b3b4a5044bfa48064d7e`。
- 被审 12 个实现/测试/核验脚本的精确版本逐项沿用 `0010-tested-versions.json`，其版本为
  `sha256:aa11c1042be1dddbf9052ed7f0088ea5eee56a9b77ea7ad3f486874726d75f77`；并在本次
  开始、复测后逐项读取实际字节核对。state 全部 41 处 path/version 引用均与实际 SHA-256 相同。

## 实现与契约核验

独立阅读全部 6 个源文件的实际 diff、新增测试/核验脚本、受影响 setup 及连接生命周期，
并定向核对架构 15.7、D-043/044/048/055/056/058/059/062。新增产品实现限于 9 条
固定表级 COMMENT、所属 setup 调用、中文 docstring 与 Session/Feedback 初始化日志。

- 每条注释位于本表既有建表/索引语句之后、同连接 commit 之前；九表白名单恰好覆盖批准范围。
  四个 Capability Repository 继续使用 `_setup_schema()`；Run 与 Interrupt 保持两个事务。
- Session/Feedback 失败记录固定错误码、错误类型和耗时后原样上抛；其他 Repository 继续
  已有异常包装。成功日志位于 commit 完成后，COMMENT 或 commit 故障不产生成功日志。
- 连接未使用 autocommit，异常退出关闭连接会回滚未提交事务；真实故障注入也验证这一点。
- `open_chat_service()` 逐个 await setup 且没有吞错；FastAPI lifespan 失败传播测试验证
  启动失败不会进入运行期或记录启动成功。
- 日志走统一脱敏入口，无 SQL、异常原文、连接串或消息正文；故障敏感标记不出现在日志中。
- 没有新增业务结构、数据写入、权限变化、依赖内部表注释或范围外产品能力。

## 本次独立复测

实际执行命令（工作目录 `D:/workspace/agent_runtime`）：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:RUN_POSTGRES_TESTS='1'
$env:DB001_TEST_DATABASE='db001_comments_834c3ae6f2234b548b2c8ce9fd19ebf4'
$env:DB001_SNAPSHOT_PATH='.dev-team/tasks/DB-001/evidence/0011-review-isolated-snapshots.json'
uv run python -m pytest tests/persistence/test_table_comments.py tests/persistence/test_table_comments_postgres_integration.py tests/sessions/test_repository.py tests/test_feedback_store.py tests/capabilities/test_persistence_repositories.py tests/runtime/test_run_repository.py tests/test_main_lifespan.py tests/core/test_logging.py tests/persistence/test_database.py -q --tb=short -s
```

实际退出码 **0**，`67 passed in 6.55s`，失败 0、跳过 0；未调用真实模型。
真实隔离用例输出：`DB001_ISOLATED_PASSED`，tables=9、rollback=True、data_rows_each=1、
excluded_comments=18。审核后独立只读查询 `pg_database`，上述新随机测试库计数为 **0**。

本次五阶段完整快照保存为 `0011-review-isolated-snapshots.json`，没有覆盖开发证据。
审核再次解析开发 0005 及本次 0011 两份快照，均满足：

- fresh、after、repeated 的九条注释逐字等于批准预期；既有样本为五条旧注释及四条 NULL。
- before 九表各 1 条有效业务 fixture，故意在 Session COMMENT 后执行无效语句，
  rolled_back 与 before 整体相同，证明失败后未提交新注释。
- 解除故障运行全部 setup，结构/owner/ACL、数据摘要及排除对象注释保持相同。
- 再次全部 setup 后 after 与 repeated 整体相同，证明可重跑且结果不漂移。

`git diff --check` 实际退出 0，仅有工作区 LF/CRLF 提示，无空白错误。

## 当前数据库的独立只读复核

本次没有执行当前库 setup 或 `--apply-current`。读取项目 Settings 连接后显式进入
只读事务，并直接查询 PostgreSQL：

```sql
SELECT current_database(), current_user;
SELECT c.relname, obj_description(c.oid, 'pg_class')
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname;
```

目标实际为 `agent_runtime / runtime / public`。从批准 requirements.md 第 4 节表格
独立提取预期文字，直接 SQL 结果与其逐字匹配，九表全部非空；另四张 Checkpointer 表
注释仍为 NULL。当前库排除对象既有注释映射为 64 条，与开发后证据一致。

复用已审核的只读 snapshot 查询逐项获得当前完整元数据和数据摘要；Python inline
校验实际退出 0：`assert current == delivered_after['snapshot']`，并独立运行
`assert_unchanged(0007['snapshot'], 0009['snapshot'])`。九表数据行数分别为：
sessions=9、message_feedback=2、runs=25、runtime_events=157、run_interrupts=1、
capability_tasks=5、capability_task_contexts=5、capability_operations=0、
user_capability_permissions=467；逐表确定性摘要完全相同，未导出业务正文。

读取并核对 0008 操作意图与回执、0007 原始前快照、0009 实际后快照：实际更新与批准
范围一致，五条错误/不完整旧注释已替换、四条空注释补齐。空闲窗口采用连接 idle、
无活动事务和间隔快照稳定的实际证据；五条历史 active 投影未被改写。审核未停止服务或 Run。

## 验收映射、问题与边界

| 验收 | 独立核验依据 | 结论 |
| --- | --- | --- |
| DBA-001/002、DBT-001 | 实际 diff、九表固定文本、直接查询与批准需求比较 | 通过 |
| DBA-003/004、DBT-002 | 当前库旧/新快照、复跑隔离库 fresh/before/after/repeated | 通过 |
| DBR-004、DBT-003 | 九表 COMMENT/commit 失败注入、真实回滚、lifespan 传播路径 | 通过 |
| DBA-005、DBT-004 | 白名单、四张依赖表及其他对象注释映射前后比较 | 通过 |
| DBA-006、DBT-005/006 | 前后元数据/数据摘要、67 项复测及中文脱敏日志检查 | 通过 |
| DBT-007 | 0008 实际操作回执、0009 及本次当前库只读直接复查 | 通过 |

没有阻塞问题或返工项。需求 DBA-003 的“4 条旧注释”数量笔误已由获批架构第 1 节明确
按五条旧注释处理，本次按五条验证，没有改写批准正文。

未运行全量历史业务回归或真实模型；本任务只涉及表元数据和初始化日志，已完成相称验证。
这不构成负责人最终验收，也不代表提交和双远程同步；三个 project_gates 继续待满足。
