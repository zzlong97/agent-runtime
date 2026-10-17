# 已验收工作区成果提交前验证

验证日期：2026-10-17（Asia/Shanghai）。验证对象为负责人授权时的全部未提交成果，
并包含本次新增的终验、提交输入与验证记录；本记录不授权远程 push。

## 实际结果

- PostgreSQL、Redis：`docker compose ps` 均为 `healthy`。
- 真实依赖完整套件：设置 `RUN_POSTGRES_TESTS=1`、`RUN_REDIS_TESTS=1` 后运行
  `uv run --locked pytest -q`，结果 `712 passed, 1 skipped in 107.50s`；唯一跳过项
  是显式可选的真实百炼 smoke test。
- `uv lock --check`：退出码 0。
- `uv pip check`：退出码 0，65 个已安装包兼容。
- `uv run python -m compileall -q src tests`：退出码 0。
- `uv build`：退出码 0，sdist 与 wheel 构建成功。
- `docker compose config --quiet`：退出码 0。
- `git diff --check`：退出码 0，仅有既有 LF/CRLF 提示。
- Capability 手册：UTF-8、代码围栏、README 链接与五个完整 Python 示例编译通过。
- TEAM-001 产物：state 中登记的四个产物原始字节 SHA256 与实际文件一致。
- 团队切换定向验证：3 项通过；task_tools 行为验证 10 项通过、1 项因 Windows
  符号链接权限跳过；五员工打包检查通过。
- 提交范围敏感信息扫描：17 个文件未发现 OpenAI 风格密钥、AWS Access Key 或私钥。

## 范围说明

提交包含团队切换成果、Capability 开发者接入手册与 README 导航、既有
`enable_thinking=False` 模型参数修改，以及对应任务台账。负责人已明确确认“当前代码
全部审核通过”并授权提交全部未提交内容。未执行真实百炼 smoke，不影响项目规定的
默认门禁；未执行 GitHub 或 Gitee push。
