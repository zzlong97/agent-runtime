# RA-DEV-03 文本 Interrupt/Resume 与页面闭环

## 目标、依据与授权

交付已批准架构17.10第3项：远程input-required经Gateway控制信号、Parent中断、
真实持久Interrupt/Resume及现有/chat完成同Run文本补参，不建立另一聊天入口。
范围与精确技术验收直接引用父handoffs/0013-RA03实施落点核对.md；它是现有批准的
实施映射，不是新方案批准。需求9.2/9.3/9.5、架构17.6/17.10/17.11、D-068及
相关既有Run/Parent/Checkpoint/取消安全规则继续有效；MI后缀不在范围。

RA-DEV-02已终验并提交b25ff6bab89dda4f3be4006e4f1456538f18964c，GitHub/Gitee
master实际一致，回执见子02/0006。负责人本轮“通过”承接明确的RA02终验、提交/
双同步及再推进03请求，来源见子02/0005；不解释为本批新代码已经终验或授权提交。

## 范围与任务内顺序

1. 开发先建立父0013的六组真实PG失败探针，最小打通受控中断与恢复，再保存通过
   回执及版本。重点包括Checkpoint后投影前、Resume已入库但尚未Command即重建、
   同一Run连续两轮补参、补参Send受理后未知、取消竞争。Fake独立于本地服务重建，
   沿用同Run/Invocation/本地Task/远程Task/context，业务创建一次、最终公共AI唯一。
2. 首探针通过后直接继续已批准API/UI接线，不另等策略确认：公开Interrupt为关闭
   confirmation/text联合，旧持久confirmation可读，Resume按目标持久kind严格校验，
   不合法载荷不得消费中断。重复/冲突/跨归属Resume及事务后唤醒失败须可核对。
3. 现有React/Ant Design(X)页面接文本输入、确认兼容、稳定request_id重试和防双击；
   失败保留输入，成功同Run续连，回答不新增Human气泡。真实Runtime+Fake专用测试服务
   验证刷新、顺序补参、取消及唯一最终消息，不能用演示图冒充远程Gateway验收。
4. 定向更新RA02中input-required临时recovering期望并重验相关身份/取消/删除屏障，
   不全局替换状态、放宽断言或机械复用旧PG结果；真实网络未知仍保留恢复态。

允许修改父0013映射的Adapter/Client必要续发点、ExecutionController、Gateway、
Parent、Chat/Coordinator、Interrupt/Run Repository及Sequencer/Schema/API、前端与
对应Fake/测试装配和接入说明。只改当前调用链的必要部分，不新增表、通用框架、
配置模式或未来抽象。若确需改变职责/事实所有者或已批准契约，带失败探针返回总控。

## 验证、环境与出口

验收为RA-AC-03、RA-AC-06重复Resume及RA-AC-02页面闭环；精确场景/负向条件均见
父0013。必须提供首PG探针实际RED→GREEN、受影响后端与旧PG回归、稳定候选必要
默认离线全量一次、前端测试/构建及真实浏览器证据；独立审核复用有效全量并定向补查。
阶段全量联合验收与成本报告仍属RA-INT-01，不冒领。本批完成后交待审核/controller，
保留精确产物/基线/测试版本及短handoff，不自验收、不自动提交或推送。

数据库只用项目Compose，核对后可创建隔离库agent_runtime_ra03_probe并清理自己的
测试数据；不得动业务数据/命名卷或修Docker。前端测试使用独立端口/进程，不重启
负责人当前Runtime。只用Fake Model/Fake A2A，不调用真实Agent/模型，不读写对方
semantic-pattern-mining仓库，不启动MI。保持已有未提交内容，所有编辑用apply_patch。

开发只持写本子任务state及本批实现/验证，不能修改父state/index、其他任务记录或
已批准正式语义，不相互派工。负责人终验、提交、双远程同步门禁适用于本批新交付。
