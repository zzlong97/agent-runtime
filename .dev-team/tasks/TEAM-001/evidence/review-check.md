# TEAM-001 迁移独立审核证据

审核日期：2026-10-17。模式：开发交付验收；入口为 state_version=3、待审核、
current_role=dev-team-reviewer。依据为已安装审核员工 Skill、项目 AGENTS、
index/task/state、最新开发交接及其引用的实际文件与只读核验脚本。
未读取 develop_team/MIGRATION_PLAN.md 或根代理评估结论。

结论：MIG-001～006 的本次迁移技术验收通过；未发现本范围的阻塞或重要问题，
无新增 REV 问题编号。此结论不批准业务基线或负责人终验，也不代表任务闭环。

## 相关输入与版本

完整审核操作输入见 `review-inputs.json`，其原始字节版本为
`sha256:ef2f65cd7049ff390abc31607a2d8ddc20e37ee720676f0a14c463719bdd0c8d`。
审核操作键由已安装 task_tools.operation-key 实际计算为
`sha256:e849dec9e1e1a5f1225a56d56f1abdee1112323f72bd0dc73b10e8eb3f50c749`。
输入仅含任务验收、实际产物、基线、开发交接/证据、技能快照和备份版本；
不含状态版本、员工、会话或执行时间。

| 被审产物 | SHA256 原始字节版本 |
|---|---|
| AGENTS.md | 42b07029752d3ae8350754ed9278d3e13e4bbcebcd712b653fbf6dcc893ba7c6 |
| README.md | 4831b0ab7e80e9ed593bb5151b20514d8b87361f27fca0b3a09278d94c623cca |
| docs/tasks.md | 6c8f7254eef54a988ec57def517482e246cca218106756d47a431552add7a6bc |
| .dev-team/project.md | 7a59657e9e3846758f8c24a0d2cf8257cad7b747fb1efb31d9577cfc625cec9b |
| .dev-team/index.yaml | 4aa72604c2d0eb0534e4c6426532403fdddad58fb8ef4d8790963332c80fea8c |
| .dev-team/tasks/TEAM-001/task.md | 26b4f86cc88dff83a74d8ad0cff8a8dbd0bdf18bd850202fd17623860f2dae81 |

入口 state 原始字节为 `sha256:150fa81dd10b4903a2f92fd711041d429fc85fccd1491eb093281b9ea8447ef6`，
仅用于确认本次写入所依据的视图，不把可变 state 文件放入验收产物指纹中。
开发交接版本为 `sha256:db71cf62f302ad61e5b733d61f7d47d67a5024d382145f950fba73a84f5ca342`，
开发证据版本为 `sha256:1b922c6d702ccaf5817df10215087c16a24b5afd39ca804e2fbd82a159a1f18e`。

## 逐项核验

| 验收 | 实际核验与结论 |
|---|---|
| MIG-001 | 实际读取备份目录和 RESTORE.md；核对 245 文件、原 Git index、完整历史 bundle、original-skills 与 retired-skills 均存在。重新运行 bundle verify 成功；备份 bundle SHA256 与恢复说明相同。回退说明要求先保存后续成果，仅恢复本次三个入口并保留移出的状态/技能副本，明确排除凭据、数据库、缓存和依赖；不要求整仓覆盖。通过。 |
| MIG-002 | 实际 Git diff 仅改变 AGENTS/README/tasks 的协作入口、状态权威及门禁；AGENTS 业务约束未见变更。需求、架构、决策的 Git blob 与被引用本地提交完全一致；tasks 从“状态枚举”至文末正文与备份一致。定向读取同一提交 S3-01 Notes 与 S3-14 Notes/Definition of Done，能追溯历史负责人裁决与终验，但没有把历史测试或批准用于新交付。通过。 |
| MIG-003 | 重新逐文件比较五员工源与用户目录副本，19 文件一致；五个 SKILL.md 均存在可读。重新 snapshot 得到与开发证据相同的集合版本；project-architect/project-reviewer 在两个发现目录均不存在，其 retired-skills 与 original-skills 文件集合/字节一致。本次未执行安装或卸载。通过。 |
| MIG-004 | index 只登记 TEAM-001 位置/依赖而没有缓存状态；state 的产物、基线、证据版本与实际字节匹配；没有第二份 context 状态。已安装 summary 实际输出同一 state 的 v3/待审核/审核角色。本审核员工也从同一入口恢复且没有重做安装。工具行为试验覆盖实际状态优先、丢失状态报错、内容变化和显式输入键稳定性。审核出口另按同目录候选 YAML 校验、写前比较 v3 与字节指纹、原子替换为 v4 的规则提交。通过。 |
| MIG-005 | 重新运行保留核验，245 个备份文件中除三个协作入口外全部逐字节相同，HEAD 与原索引不变；未跟踪 guide、已有 model 改动保留，README 接入手册链接仍在。没有将它们视为本次已验收业务变更，也未重做历史阶段。通过。 |
| MIG-006 | 对照 AGENTS 第 17/19 节、task、project 与 state：三项门禁均为待满足且 evidence_ref 为 null；独立审核后必须待负责人决策并保留 resume_from=已验收。HEAD 仍为原历史提交，索引未变，index 未登记下一业务任务；本次未运行任何远程命令。通过。 |

## 本次实际命令与结果

以下均由审核角色实际执行，未仅复述开发结果。

工作目录 `D:/workspace/develop_team`，已先检查脚本只读范围：

1. `python -X utf8 -m unittest verification.test_project_switch.ProjectSwitchTests.test_original_project_files_preserved_except_three_workflow_entries verification.test_project_switch.ProjectSwitchTests.test_legacy_history_is_frozen_not_regenerated verification.test_project_switch.ProjectSwitchTests.test_installation_and_recoverable_uninstall -v`
   退出码 0，3 项通过。
2. `python -X utf8 -m unittest discover -s verification -p test_task_tools.py -v`
   退出码 0，10 项通过、1 项因 Windows 符号链接权限而跳过。测试在隔离临时目录中验证工具行为；未修改项目实现。
3. `python -X utf8 verification/check_bundle.py`
   退出码 0，五员工打包检查通过；该结果仅说明身份/YAML/内部链接/registry/模板隔离。

工作目录 `D:/workspace/agent_runtime`：

4. `git bundle verify D:/workspace/dev-team-backups/20261017-123705/agent_runtime-all-refs.bundle`
   退出码 0，bundle is okay，记录完整本地历史。bundle SHA256：
   `ebf4116f611937f43a500f3aec96191c9ac796e99ab6b3994edfa7e6eff0fc10`。
5. `git ls-tree 29dcca1e71d3b4faa7517fc95e5468ea6a826bb0 docs/requirements.md docs/architecture.md docs/decisions.md`
   与 `git hash-object -- docs/requirements.md docs/architecture.md docs/decisions.md`
   返回的三个对应 blob 相同，分别为 requirements `bd955ef0362c7ab413fc0a9d1eb356ac1fb336af`、
   architecture `6dddf03ad1adeda622c4b3991c5b9b138a0caf68`、
   decisions `195a225745f34df1c6a43338d5ba17cee5e0ae2a`。
6. `git diff -- AGENTS.md README.md docs/tasks.md`，人工检查修改边界；
   `git diff --check` 退出码 0，仅有 Git CRLF 提示。
7. 已安装 controller 的 `task_tools.py snapshot --root C:/Users/zzlong/.agents/skills --paths dev-team-controller dev-team-requirements-analyst dev-team-architect dev-team-developer dev-team-reviewer`
   退出码 0，files_count=19、missing=[]，集合版本
   `sha256:a11aab03c03297e336796cdf333d27cfeb9496326c09bf1c6529552417d50d21`。
8. 同一工具 `summary --root D:/workspace/agent_runtime --format json`
   退出码 0，仅输出 TEAM-001/v3/待审核/dev-team-reviewer，位置为唯一 state_file。

本次复用的核验脚本版本：

| 脚本/规则 | SHA256 |
|---|---|
| develop_team/verification/test_project_switch.py | 96c89e5d20b8830ee78f9b469863d55143478a2800afe841878b75f069f1ee9f |
| develop_team/verification/test_task_tools.py | 31cc4250dcffe2ea9985243ba8f0d2315fb334b679796a37e7459369a0624968 |
| develop_team/verification/check_bundle.py | 722b942e8192db2848f851b2fa4f0dcbac3d4f6e28796c48fd255c62b98d4d46 |
| 已安装 controller/scripts/task_tools.py | 29eaee58d9f70b16548bd8b9199a05417c1857da395636f2e9dc5183b257beb2 |
| 已安装 reviewer/SKILL.md | 0962d4c4b09216b113fce545cc898148c9dbb69d25a467055dc6088b93a8d2c0 |

## 未覆盖项与历史观察

- 未重跑业务单测、全量业务回归、前端、数据库、真实模型或远程查询；这些不在本次迁移技术验收内。
- 未实际执行灾难恢复、恢复覆盖、安装、卸载或重新开启会话的技能选择界面；检查的是可读安装、实际副本、恢复材料和指引。
- Windows 符号链接分支因权限跳过；没有把该分支算作通过。
- 原 model 参数修改为 `sha256:d688078dee7cad8281789ebfced70df730f44c45c315852a0bcc10de95da2821`，
  原 guide 为 `sha256:8f88ab142e52a80afe9dcad14999246dfb607d178bfbbe89d286faa44e47cfa4`。
  仅核验原字节保留，其业务适用性需要另行批准范围后重新验证。
- 开发证据记载 v2 的 updated_at 曾被未递增版本地校正。本审核未见当时写入，
  不将该记载伪装为独立重现；当前 v3 的来源和字节已核对，不能因此重做迁移。
  本次状态写入必须比较当前 v3 并递增 v4；该历史观察保留。
- 历史批准来源的 approved_at 未编造精确时间；已用固定提交与定向历史章节追溯。
  本地 remote-tracking 引用不用于证明当前双远程状态。

下一责任角色为 dev-team-controller：读取审核交接和真实状态，向负责人提交本次终验结果。
审核不替负责人批准，不更新三项项目门禁，不提交、推送或启动下一业务任务。
