# DB-001 架构静态验证证据

## 1. 范围与结果

2026-10-17，Asia/Shanghai，架构员工仅只读调查代码/文档，并写入本任务架构、技术
拆解与交接。没有连接或修改数据库，没有运行未来实现测试或业务测试，没有提交/推送。
需求盘点证据 `0001-inventory.md` 的 9 表/4 排除表结论复用，不重复盘点。

已确认需求版本仍为
`sha256:c84d441897d4412c7b72abbe9ea46e9cae66edcc1e6e8747a3d82ac345e2a2b4`，
批准记录为 `handoffs/0002-controller-approval.md`。架构候选版本：
`sha256:0018229383ac83955f932ec617706717a3e67f011ac6b3b4a5044bfa48064d7e`。
`task.md` 是可变引用视图，不作为架构正文或批准基线。

## 2. 输入版本

实际执行只读工具：

```text
uv run python <dev-team-controller skill>/scripts/task_tools.py snapshot
  --root D:\workspace\agent_runtime
  --paths docs/requirements.md docs/architecture.md docs/decisions.md
          src/agent_runtime/sessions/repository.py src/agent_runtime/feedback.py
          src/agent_runtime/runtime/repository.py
          src/agent_runtime/runtime/event_repository.py
          src/agent_runtime/runtime/interrupts.py
          src/agent_runtime/capabilities/persistence.py
          src/agent_runtime/persistence/database.py src/agent_runtime/chat.py
          src/agent_runtime/main.py
  --details
```

退出码 0；12 文件，无缺失；输入快照版本
`sha256:872579140fdb3bca56144ee7f076d8039960b87ff705252778e79846e48b3ff7`。

| 输入 | SHA256 |
| --- | --- |
| `docs/requirements.md` | `0d540b18e330aaf6cdce50dc30cab44ff4322c072a4e410d0d23bba6050bad7c` |
| `docs/architecture.md` | `b7334cae078c1b09bcf8e5263146cf2c460d43e32b0dc6dfb0aead669e42f5c1` |
| `docs/decisions.md` | `c919ad658aafd8d18abcabbcb7deac3db44939e8aee9f4d0b363ed416d82e005` |
| `src/agent_runtime/sessions/repository.py` | `5d2e1b670918092c517e388a7bce486522e8eabbdba458a6d30d8b694d65b23a` |
| `src/agent_runtime/feedback.py` | `74ecac2c8febdc75546b7d20f28b8b4b1ade9d576d270a7225ea884673d8c69d` |
| `src/agent_runtime/runtime/repository.py` | `c1f0a0ea891c31f5e7c725bfe54fcf9b9e781c8570e821bf4ed95b0618f50488` |
| `src/agent_runtime/runtime/event_repository.py` | `9145c0c47e486293d85aadf4e9fbef222c73e02dafec69f1b72ec7bc32dd836c` |
| `src/agent_runtime/runtime/interrupts.py` | `c73d8063e030b97503b925a22e62186fc37ccf233b5aeb1f48a64506c9ad946f` |
| `src/agent_runtime/capabilities/persistence.py` | `5d4743e6a58e61e2683c9bf721e9fa9ac407cf2976f56f0420ddd4924837137c` |
| `src/agent_runtime/persistence/database.py` | `f39f505f5dc28df3a68207080441966bbc6a9612180483312d0dfbf0811479a6` |
| `src/agent_runtime/chat.py` | `b01950a4718c8572e8c6ac3ba7429aa6c3482aa44ca1774ee42e2a64ef6c29f8` |
| `src/agent_runtime/main.py` | `13f71ebf08906e5a65206052c3c159c00539b2e774e7f5d13c92d0ce87f01672` |

三份权威文档与六个自建表源文件均与需求盘点版本相同。新检查连接/启动调用链以
上述 3 个实际文件版本为准，没有用 HEAD 或历史测试数量替代当前文件事实。

## 3. 静态验证

- 已读取 9 个 setup 的建表、索引、commit 和已有异常日志分支；确认正常调用顺序为
  Session、Feedback、Run（内部初始化 Interrupt）、Event、Task、Context、Operation、
  Permission；没有另一个独立必须执行的表初始化入口。
- 已读取 `open_database_connection()`，确认不启用 autocommit；关闭未提交连接的
  现有模式适用于 COMMENT 失败路径，实际 PostgreSQL 回滚语义留待开发验证。
- 已读取 `main.create_app()` lifespan，确认 setup 异常传播时没有服务就绪，且已有
  统一中文应用启动失败日志；只为 Session/Feedback 补齐局部初始化日志即可。
- 已读取相关 setup FakeConnection 测试与 opt-in PostgreSQL 集成测试。现有 Session
  和 Feedback 测试各断言两条 setup 语句，开发需保留建表/索引约束并更新新增注释断言。
  现有 Windows 测试使用 psycopg 兼容循环；新集成验证沿用该约定。
- PowerShell 静态断言：批准需求 SHA256 精确相同；候选含全部 9 个表映射和
  DBT-001～007。命令退出码 0。候选第 5 节已映射 DBA-001～006，无削弱业务验收。
- `git diff --check` 退出码 0。Git 对已有 `.dev-team/index.yaml` 提示 LF/CRLF 归一化，
  未报告差异格式错误；该提示不代表业务或数据库验证。
- 文档原文保持不变；需求 DBA-003 的“4 条旧注释”笔误只在架构第 1 节解释为 5 条。

## 4. 有效性与限制

本证据仅确认架构候选的输入一致性、现有技术调用链及验收映射。DBT-001～007
描述的是未来开发验收，不是本阶段通过的业务测试。实际 COMMENT 执行、新库/旧库/
重跑、故障注入、结构/数据摘要和当前库最终注释均待架构批准后开发验证。

候选交付通过静态核对不等于负责人批准。状态应进入“架构待确认”，归还总控，保留
全部项目门禁，不进入开发。
