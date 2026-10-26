# DB-001 开发验证证据

工作目录为 `D:/workspace/agent_runtime`。日期为 2026-10-17，Asia/Shanghai。
精确被测实现、测试、核验脚本与批准基线见 `0010-tested-versions.json`，没有使用 Git
HEAD 代替未提交产物版本。所有实际运行均使用本地 Fake/数据库，不调用真实模型。

## 最终结果

- 6 个业务源文件为 9 张表声明固定中文 COMMENT，并在本表 setup 的 commit 前执行。
- Session / Feedback 新增中文初始化开始、完成、失败日志；失败保留原异常类型，其他
  Repository 保留原有错误包装。日志不记录 SQL、连接串、异常正文或业务正文。
- 当前 `agent_runtime` 数据库 9 条注释已实际更新并逐字验证；原 5 条旧注释替换、4 条
  空注释补齐。13 张表中另外 4 张 Checkpointer 表注释仍为 NULL。
- 当前库前后列、默认值、可空、identity/generated、约束/外键、索引、owner/ACL、九表
  行数与确定性数据摘要完全相同，其他对象注释完全相同。实际前快照为 0007，后为 0009。
- 真实隔离库九表各保留 1 条有效 fixture；空库/5 旧注释加 4 NULL/重跑逐字一致；
  在 Session COMMENT 已执行后注入 SELECT 1/0，断言异常抛出且整个快照回滚；解除故障
  重跑成功。四张依赖表由真实 AsyncPostgresSaver.setup 创建，保留列/索引/约束注释
  样本，所有排除注释保持一致。五阶段完整快照见 0005。

## 命令与退出结果

最终验证命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:RUN_POSTGRES_TESTS='1'
$env:DB001_TEST_DATABASE='db001_comments_7f6a83681c634b98a1500416e4f0cc24'
$env:DB001_SNAPSHOT_PATH='.dev-team/tasks/DB-001/evidence/0005-isolated-snapshots.json'
uv run python -m pytest tests/persistence/test_table_comments.py tests/persistence/test_table_comments_postgres_integration.py tests/sessions/test_repository.py tests/test_feedback_store.py tests/capabilities/test_persistence_repositories.py tests/runtime/test_run_repository.py tests/test_main_lifespan.py tests/core/test_logging.py tests/persistence/test_database.py -q --tb=short
```

实际退出码 `0`，`67 passed in 6.11s`，失败 `0`，跳过 `0`。其中真实 PostgreSQL
聚合用例 1 项，其余为本次影响范围的单元/初始化/日志检查。

单独隔离验证同名 PostgreSQL 用例实际退出码 `0`，`1 passed in 2.58s`；真实输出
`DB001_ISOLATED_PASSED ... {'tables': 9, 'rollback': True, 'data_rows_each': 1,
'excluded_comments': 18}`。每次仅删除本次 create 成功的同名临时库；当前库只读
`pg_database` 复查该临时库剩余 `0`，见 0007 与 0009。

当前数据库实际应用命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
uv run python .dev-team/tasks/DB-001/verify_table_comments.py --apply-current --before .dev-team/tasks/DB-001/evidence/0007-current-idle-before.json
```

实际退出码 `0`，真实结果为“九表精确注释已更新；结构、业务数据及排除对象注释无变化”。
完整回执见 `0009-current-after.json`。该脚本默认只读，实际应用依次调用批准的业务
Repository setup，不启动 Coordinator、模型或 Checkpointer 升级。

`git diff --check` 退出码 `0`。Git 输出 LF/CRLF 的既有工作区换行提示，无空白错误。

## 空闲窗口与操作恢复依据

执行前先保存 `0003-database-operation.json` 的隔离库意图，实际当前库应用前保存
`0008-current-operation.json` 的稳定操作键、目标、批准版本、原始前快照与执行中意图。
回执随后补入同一文件，未更换键或篡改原输入引用。

当前库 5 条 active Run 投影最后更新日期在 9/24、9/30、10/12。实际有 Runtime 进程
20788 监听 127.0.0.1:8000；数据库 7 条连接全部 idle、transaction_start=null，四条
Runtime 连接自当日约 12:09 上海时间无活动。间隔约 1 分钟的 0006 和 0007 结构、
九表数据摘要、注释完全相同。总控核对这些事实后确认支持批准架构的无并发写入窗口。
应用前再次核对窗口与原快照，应用后严格比较摘要；没有停止 Runtime 或修改 Run 投影。

初始核验脚本曾把 active 投影数量作为过严阻断条件，预检退出 1，尚未调用任何当前库
setup；经只读调查与总控解释，改为按真实连接活动/事务及稳定快照判断窗口。

## 开发过程中的非成功命令

- 先写新单元测试，再实现；使用正确 Python 入口时 `27 failed, 1 passed`，失败均能暴露
  缺失 COMMENT / 故障处理，随后实现并完成最终绿色验证。
- 最初 `uv run pytest` 使用脚本入口导致 `tests` 包导入失败，改为统一
  `uv run python -m pytest` 后执行上述验证；不是业务实现失败。
- 一次容器 psql 只读探查使用默认 postgres 角色失败（该角色不存在），随后始终经项目
  Settings 使用实际 runtime 角色核验；未创建角色、修改权限或触及 Docker 配置。
- 一次尝试经工具 stdout 收集五阶段大 JSON 因输出截断导致解析失败；测试数据库仍按
  finally 清理。改为测试显式输出生成的 JSON 证据，最终完整快照文件已保存并复跑验证。

## 验收映射及边界

DBT-001/004：九表 FakeConnection 语句与 commit 顺序、精确文本和白名单检查；
DBT-002/005：隔离库五阶段及当前库前后完整快照；DBT-003：九表 COMMENT/commit
故障注入与真实 PostgreSQL 事务回滚，加既有 lifespan 启动失败不上报成功测试；
DBT-006：上述67项相关验证与日志敏感标记检查；DBT-007：0008操作意图与0009真实回执。
对应 DBA-001～006 的开发验证已覆盖，独立 Reviewer 仍须按批准基线审核。

没有全量回归、真实百炼调用、提交或推送；未更改批准正文、数据结构、业务数据、权限、
依赖内部表注释或新 Stage。负责人终验和双远程等项目门禁保持待满足。
