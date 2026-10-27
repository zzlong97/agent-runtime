# RA-DEV-02 远程执行绑定、恢复与取消闭环

## 目标及授权

按父任务已批准 architecture17.10 的第二项，交付可验证的远程后端执行闭环，而不是
若干辅助函数：当前消息经现有 Router/Gateway 调用 Fake A2A、持久绑定、返回唯一公共
AIMessage；重建Runtime恢复、取消及删除均守住远程未知结果屏障。
负责人原始“现在开始RA-001任务”和本轮“继续执行”见父0002/0007；前置RA-DEV-01
代码4f6791369ee562c52539a260ee7320f38e7f34c8已推送GitHub/Gitee同一master，实际回执见
子01 handoffs/0005。本任务不授权修改对方仓库、真实联调或自动提交本批新代码。

原技术落点见父0004、0006；0006的范围拒绝冲突已由负责人明确答复“批准此窄例外”，
正式条件与版本见父handoffs/0008-远程范围拒绝窄例外裁决.md。不重问已确认结论。
基线只批准RA及相关既有安全章节，MI后缀不属于授权：

- requirements：sha256:8ab1332d68e928a0dd8c49843e1d218fd19e0a655a48aff1ed372f5e7552fe7a。
- architecture：sha256:92f95702b266990c061b7825ec7ca66a7d21f57a1516845e7d09137c3444f87b。
- decisions：sha256:78ac1122defa9df497fd154d13bacd8dc04a15ef1b4c53024d448cbf3ee681af。

## 实施边界

1. 复用01官方SDK Client/Card/Fake，补受信remote Adapter；当前Human文本及稳定UUIDv5
   messageId，不传历史/System/State；普通Capability SDK不暴露HTTP、凭据或生命周期。
2. runtime/event_schemas.py、event_repository.py、sequencer.py：固定内部task_bound/
   task_terminal事件及受控读取、归属、幂等检查；不增加表，不向公开SSE/Redis暴露这些事件。
3. operation_ledger.py及remote Adapter：Send/Cancel用已批准稳定operation_key，Get只读；
   绑定持久化先于Send正常退出；未知保持pending，不能将succeeded倒退或盲目重送。
4. invocation_gateway.py、recovery.py、execution.py、chat.py、coordinator.py：复用同一
   open Invocation/Task/Run；无绑定同ID重送取回、有绑定只Get；身份改变或未知结果保持
   recovering/cancel_requested。覆盖早期失败、强制取消、超时和异常收尾，不仅正常循环。
5. 移除远程恢复次数3的终止限制及内部/公开Schema上限，计数真实增长、有界退避；本地
   三次策略不变。已受理但尚未task_bound的open remote Invocation也须覆盖。
   父0009/0010已细化既有身份安全要求：仅started内部Schema增加可选远程身份hash，
   首次Send前持久；原hash不得回填/覆盖，来源和稳定身份使用同一确定性算法。
   旧事件无可信hash且无绑定保持恢复态，身份替换后不得Send/Get/Cancel。
6. Cancel/超时及Session删除：先确认远程终态，再允许本地终结/清理。未知时删除返回
   可重试409，保留全部证据且同Session新Run仍被阻塞；不实现MI持久删除屏障或租约。
   父0011细化既有终态语义：内部cancel_requested仅增reason=explicit|timeout，缺省explicit；
   首次状态事务持久意图获胜。跨重建确认后，timeout仍CAPABILITY_TIMEOUT/failed/incomplete，
   explicit仍cancelled/stopped；公共协议不扩字段。该两项仅落实原目标，无新增业务批准。
7. completed按文本Artifact/Part顺序拼接并发单个delta，Parent写唯一非空公共AIMessage
   后才推进本地终态。补稳定错误白名单；认证/协议/任务失败与未知/不可用严格区分。
8. 仅按0008批准实现远程显式OUT_OF_SCOPE窄例外：Get确认同Task/context的rejected终态，
   零输出/业务State/ChildCheckpoint；同事务精确清理本次受控remote_send（succeeded或
   因崩溃pending但结果已确认）、恢复原current、回滚provisional Task并写内部结果。
   保留绑定/远程终态证据。其他业务/Cancel/其他Send/未知pending不得清理；本地规则不变。

现有Task/Operation持久化Repository、TaskService、必要Run API Schema及对应测试可随
上述真实调用链定向调整。仅增加本任务轮询/退避实际使用的最小全局配置，不加未来抽象。
正式文档和接入手册按真实契约同步引用，禁止自行改变已批准语义。

## 验收与验证清单

- [ ] RA-AC-02后端：经现有Runtime入口完成Fake任务，公共AI唯一非空，Run/SSE契约不变。
- [ ] RA-AC-05首个真实PG探针：远程已受理、绑定前故障；重建本地Service/Registry/Coordinator，
  保持独立Fake和同库，同Invocation/messageId/Task/context连续恢复至少4次，业务创建始终1。
- [ ] 绑定后终态前、绑定已写Operation仍pending、Parent已写Run未投影的崩溃均正确重放，
  已绑定不重复Send，Get不写Operation，不新增Run或重复公共AI。
- [ ] RA-AC-04/08：取消未知、重启后确认、超时/Cancel竞态和删除未知屏障均在真实入口验证；
  不能把本地asyncio Task退出当远程终态，也不无限挂住DELETE。
- [ ] 0009/0010身份替换、旧缺字段、started提交失败、started/bound不一致及来源变化均
  不向错误身份远程I/O；0011双向意图竞争、提交失败和跨重建保持原终态语义通过。
- [ ] RA-AC-06初次提交、07：稳定ID幂等/冲突、身份变化、错误映射、日志/事件脱敏与普通
  本地恢复/取消回归通过。重复Resume及文本补参完整验收不在本批冒领。
- [ ] 0008全部正负条件、事务失败整体回滚、提交前后重建对账，在单元及真实隔离PG验证；
  本地带Operation后拒绝仍失败，普通远程rejected仍失败，不仅按key前缀放行。
- [ ] 行为先RED再实现；开发在稳定候选上执行必要默认全量一次并保存精确版本，独立审核
  定向复核关键路径，总控核对证据而不为换角色重复全量。

数据库验证仅使用项目Docker Compose及隔离测试库/测试数据，先核对目标和既有夹具，
不清空业务数据或删除命名卷。Docker异常只报告，不修复/重置。若结构实际有必要变化，
先说明当前使用方并返回总控；不能擅自新增表，也不能以mock代替PG探针。

## 明确不实现及出口

文本Interrupt/Resume API、页面控件和正式Interrupt/open Invocation PG探针归RA-DEV-03。
本批input-required只保留受控内部待接通边界，不当failed/completed，不宣称文本补参已通。
没有真实远程Manifest默认启用；不连接真实模型/Agent、不重启当前服务、不读写对方仓库。
MI、多实例、Lease、Remote Registry、跨Run上下文、文件及业务算法均禁止。

开发只持写本子任务state及本批代码/测试，交付精确版本、短验证交接和必要长证据后回到
待审核/controller，不相互派工、不自验收、不改父state/index或其他任务。不自动提交/推送。
负责人终验、任务提交及双远程同步门禁适用于本批正式交付，技术审核不替代负责人终验。
