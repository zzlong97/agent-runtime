# 首组真实 PostgreSQL 探针回执

2026-10-27T13:55:20Z，开发验证；不是独立审核或负责人终验。HEAD
`b25ff6bab89dda4f3be4006e4f1456538f18964c`，本批实现尚未提交。

环境：项目 `compose.yaml` 的 PostgreSQL 17，容器 `agent-runtime-postgres` healthy；
先查询 `pg_database` 确认不存在，再以 `docker compose exec -T postgres createdb -U runtime agent_runtime_ra03_probe`
创建本任务隔离库（退出码0）。Python3.12.7、langgraph1.2.11、psycopg3.3.5，Fake Model、独立 Fake A2A，未用业务库或真实模型/远程Agent。

工作目录 `D:\workspace\agent_runtime`。首次 RED：
`$env:RUN_POSTGRES_TESTS='1'; uv run pytest tests/integration/test_remote_interrupt_resume_postgres.py -k pending_rebuild -q --tb=short`
退出码1，1 failed / 6 deselected，5.93s，实际 recovering != interrupted。
中间全组退出码1，4 failed / 3 passed，32.02s；明确暴露 resumed 记录被再次 INSERT 的
UniqueViolation、取消未知仍停留 interrupted。之后一轮6 passed / 1 failed，40.02s，
两轮旧答案被重复 Command 误消费。定向修正后2 passed / 5 deselected，15.39s。

最终首组命令：
`$env:RUN_POSTGRES_TESTS='1'; uv run pytest tests/integration/test_remote_interrupt_resume_postgres.py -q --tb=short`
退出码0，**7 passed in 40.64s**。六组窗口覆盖：真实受控中断与Checkpoint先于投影、
pending后重建、Resume事务后Command前重建、补参受理后断连及同ID账本重放、两轮顺序问题、
Cancel/Resume双向竞争及stopped Checkpoint后终态投影前重建。正式Controller/Gateway/
Parent/Coordinator/Repository/Checkpointer未替换；仅网络、路由输入与崩溃边界注入。
检查同Run/Invocation/本地Task/远程Task/context、一次业务创建、唯一公共AI、Send账本且无Get账本、
唯一Invocation/Run终态及seq、不新增Human、新Run409、未知取消占用及删除409。

首组被测内容SHA256（以下精确版本有效，后续API/UI及受影响改动须补验）：

| 文件 | SHA256 |
| --- | --- |
| src/agent_runtime/capabilities/execution.py | 99bfd22bbadd8254edb1acce5a330bb04457a1af6cf68ef4cf6cc9042ebcf66b |
| src/agent_runtime/capabilities/invocation_gateway.py | 9ba549243e2f4067e4d61cfdb84a69bb687ce7decfedb8ab72f81d2ecdf6dd04 |
| src/agent_runtime/capabilities/remote_a2a/capability.py | 5838212dd83f888af9b7fbb9368982cdbac7e748a68fdb6e5db6da21e7974a42 |
| src/agent_runtime/graph/parent.py | 25c680b35d973f9e8b79036506d378ddf4be35919e77d58051ff5976217a07b5 |
| src/agent_runtime/chat.py | edb8a7a95b281462074f6cc16b7f0c25130e442e0e00945d7a66ec4df8f4dcd2 |
| src/agent_runtime/runtime/event_repository.py | 0c1f4dc70b25a58cb5fcd3d2a11e81b8246ee57569a4cbb49b38d64b63e89eb8 |
| src/agent_runtime/runtime/interrupts.py | 75973657339a50ba42db8db978294aa735c66ff1f13047fc68205506f8804687 |
| src/agent_runtime/runtime/models.py | e720102221790356cfac3931b1f41f319d75923b932fadab989391fb808ffaf7 |
| src/agent_runtime/runtime/sequencer.py | 016d418f66c63f93e47f6f71c2172c396879cd277f48243667bef106d0c2f128 |
| tests/capabilities/test_remote_invocation.py | 1336c681a7d5b6dbb0f0ae1c9c5cef52e9c0036d6e96f77b20cbff0d070320ba |
| tests/fake_a2a.py | 1db2af9820b53bad13c116896b27d2875df172579cf15f154b1ac7c70fa6fbe9 |
| tests/integration/test_remote_interrupt_resume_postgres.py | 07e676397cba39a4ac134f6a83ed660190534c90d5c21995d37fb3f9c3f08a4c |

本首组未覆盖产品关闭Schema、输入校验、页面或浏览器，按任务内顺序继续。
