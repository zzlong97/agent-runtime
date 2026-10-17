# TEAM-001 接管交付验证

记录时间：2026-10-17T12:44:20+08:00。验证目标仅 MIG-001～006，不是业务回归。

## 被核验版本

| 产物 | SHA256（原始字节） |
|---|---|
| AGENTS.md | 42b07029752d3ae8350754ed9278d3e13e4bbcebcd712b653fbf6dcc893ba7c6 |
| README.md | 4831b0ab7e80e9ed593bb5151b20514d8b87361f27fca0b3a09278d94c623cca |
| docs/tasks.md | 6c8f7254eef54a988ec57def517482e246cca218106756d47a431552add7a6bc |
| .dev-team/project.md | 7a59657e9e3846758f8c24a0d2cf8257cad7b747fb1efb31d9577cfc625cec9b |

五个已安装技能的聚合内容快照：
`sha256:a11aab03c03297e336796cdf333d27cfeb9496326c09bf1c6529552417d50d21`。
它是 task_tools.snapshot 对下列五个目录的路径/文件名/内容集合指纹，不是单文件 SHA。
根：`C:/Users/zzlong/.agents/skills`；paths 为 dev-team-controller、
dev-team-requirements-analyst、dev-team-architect、dev-team-developer、dev-team-reviewer。
19 个文件，missing 为空；安装副本与源目录逐文件相同。

## 实际命令与结果

工作目录：`D:/workspace/develop_team`。

1. `python -X utf8 -m unittest verification.test_project_switch.ProjectSwitchTests.test_original_project_files_preserved_except_three_workflow_entries verification.test_project_switch.ProjectSwitchTests.test_legacy_history_is_frozen_not_regenerated verification.test_project_switch.ProjectSwitchTests.test_installation_and_recoverable_uninstall -v`
   退出码 0，3 项通过：245 个备份文件中除三个协作入口外的所有文件逐字节不变，
   Git HEAD/索引不变；旧 tasks 状态枚举起至文末正文相同；README 原接入手册链接保留；
   新技能副本一致，旧技能仅移出发现目录且与备份一致。
2. `python -X utf8 -m unittest discover -s verification -p test_task_tools.py -v`
   退出码 0，10 项通过、1 项因 Windows 符号链接创建权限而跳过。未宣称覆盖跳过分支。
3. `python -X utf8 verification/check_bundle.py`
   退出码 0，五员工的身份、YAML、内部引用、registry 与模板实例隔离检查通过。
4. 对五个已安装员工分别执行 `python -X utf8 <skill-creator>/scripts/quick_validate.py <员工目录>`：
   五次退出码均 0，均输出 Skill is valid。格式验证不等于行为验证。
5. `git -C D:/workspace/agent_runtime diff --check`：退出码 0；仅提示现有 Git CRLF 设置，
   未执行格式化、提交、暂存或推送。
6. `git bundle verify D:/workspace/dev-team-backups/20261017-123705/agent_runtime-all-refs.bundle`：
   退出码 0，包含完整本地历史。245 文件备份复制时逐个 SHA256 校验通过。
7. 已安装 controller 的 `task_tools.py snapshot --root <技能根> --paths <上述五目录>`：
   退出码 0，结果如上。证明本地文件可读取，不声称已观察全新会话的技能选择界面。

## 已有变化与未覆盖项

原模型文件保持 `sha256:d688078dee7cad8281789ebfced70df730f44c45c315852a0bcc10de95da2821`；
原未跟踪接入手册保持 `sha256:8f88ab142e52a80afe9dcad14999246dfb607d178bfbbe89d286faa44e47cfa4`。
未对此两项变化做业务验收；相关结论需要重新验证，不沿用历史 Stage 3 测试结果。
未读取 .env、未启动服务、未调用真实模型、未查询/推送远程，未重跑全量业务测试。

恢复试验发现登记状态 v2 的 updated_at 曾被根代理校正而未同时递增版本。
根代理确认是时间字段修正，不是业务输入变化；交付时重新读取 v2、核对原操作键和
实际产物，在新修订提交完整交付，并保留此说明。不删除该观察、不重复安装或卸载。
之后状态通过校验后的同目录临时文件原子替换，并在成功转换时递增版本。

技术交付等待独立审核；负责人终验、任务提交、双远程同步均未发生，不记为已满足。
