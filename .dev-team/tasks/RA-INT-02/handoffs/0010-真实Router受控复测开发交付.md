# 真实Router受控复测开发交付

2026-10-29，dev-team-developer。输入state v13；沿0009执行意图和操作键
`sha256:13110a56c0ee088974987390ceb6d2ce26685c12201f7b4e5aabe2cbcee1ce6d`实际执行。
完整身份、原生PG/HTTP/SSE/日志安全事实见evidence/受控复测真实结果.json。只交真实结果
待独立复核，不替负责人终验，不自行关闭R01或宣称日常主环境已启用。

## 执行结果

新请求d3e517ba-0747-5446-a0c6-8e061d045f0e仅显式POST一次，10:49:49 UTC返回202；
Run f40fca5f-928c-4591-97a5-9788005d766a于10:51:34 completed，恢复次数0、活动输入已清除。
固定原BATCH/专用DB/用户/Redis13及模型均未改。当前真实Router面对原两本地能力和
semantic_pattern_mining三个候选，实际选择远程，confidence0.98、业务日志耗时1110.31ms；
open_chat_service没有注入Fake或固定选路，演示模式关闭。当前四项执行输入SHA完全一致，
本轮没有代码或配置变更。

唯一Invocation aa2626e1-f6f9-4d33-9b16-858e6c480bce，local CapabilityTask
32c25214-86f6-4a7c-9b9c-30e13007e742，remote Task 2202a2e9-4159-4597-91f2-2772b8fdc85e，
context fbda3c18-d7f2-4c77-aafb-07cedbc0b5e4，初始message cd28b5ef-b302-55aa-acce-c089927f2588。
持久身份48f71b662b021f044ff81ba2d42da4d7589992ed009fae462d124b8984aedf08与受信输入一致。
started/completed/bound及remote_send账本均唯一，Operation succeeded；没有补参、取消或
替代请求。整个RA-INT-02远程Task额度已用1/1，不能再创建Task。

远端Task为TASK_STATE_COMPLETED；去重消息只有1条且ID匹配。唯一business Run
824e4405-903d-49fd-b149-6377c229f72b的thread=a2a:<同TaskID>、turn=<同TaskID>精确对应，
status=completed。实际UTC范围09/30 16:00～10/20 16:00，等于上海10月1日至20日含结束日。
源表聚合计数仍50条/5类。业务产物common_clauses=0、trace=0，这是本次实际结果，
不作算法质量或GPU推理已验收的结论。

## 真实验证

按原已审工具启动独立Runtime：RA_INT02_APPROVED固定BATCH、ALLOW_TEST_SETUP=true、
既有LOG_LEVEL=INFO；托管session13380，进程链36700→35916。启动日志明确INFO，专用
每日文件实际出现应用启动/HTTP等运行事件，证明log_config=None已在这次启动生效。
提交前health=production/三健康候选、新请求404、新回执不存在；随后原probe submit
退出0，202形成上述Run。根预检旧failed/零远程副作用已保留，不把本次当未知自动重试。

刚执行原工具命令 `.venv/Scripts/python.exe -X utf8 -m tests.e2e.real_router_probe verify`，
退出0。实际公共历史只有1 Human+1 AI，AI 330字符、非空、completed、正确能力和消息ID；
包含起止日期。AI MD5为1a0e47097989d26c0f45be5539e5d585，与远端Artifact逐字内容的MD5一致，
证据仅存长度/哈希/布尔，未输出正文。/chat静态页面GET200，仅作可访问验证，不冒称
本轮交互浏览器回归。

实际GET SSE严格关闭Schema及event头匹配，seq为1/2/8/10/11，允许缺号且唯一递增；
包含唯一message.finalized及run.completed，均属同Run。Last-Event-ID越过终态后重连为空。
本地和远端分别原生psql BEGIN READ ONLY查询固定身份、状态、日期、聚合与元数据，
退出0，不读取业务私有State/Checkpoint。新库13表102列，自有9表79列实际中文注释完整。
旧failed Run保留；本地CapabilityTask行仍active属于独立生命周期投影，未手工结束它。

## 关闭与保留事实

只在本地Run和远程Task确认终态后，向已知托管13380发送一次Ctrl+C。退出1，包含
KeyboardInterrupt/CancelledError，日志有应用关闭完成、聊天服务关闭开始；不报clean
exit0，也未强杀任何进程。随后本测试模块进程0、18782可绑定；再查PG仅旧failed与新
completed两Run，active_runs=0、Operation succeeded1。专用库、回执和幂等事实全部保留。
主Runtime/主用户权限/主库、对方API/Worker/源码/卷、.env均未动。

原失败请求、Run、输入、源码快照和操作键保持历史；旧回执SHA
6293fd8a1789e54311eaf01486cb7718ff9ff2df3b7e278908e85951c5ca9c9d未变。本次证明当前
配置的普通真实Router及远程完成链路可用，不倒推首次ROUTER_CALL_FAILED原因。失败旁路
本次未触发，原安全观测及21项离线证据继续有效；没有为本轮重跑全量或新增模型目录请求。

## 交回

新回执为普通Router受控复测回执.json，顶层accepted只表示受理，内部Run及verification
真实为completed/true。R01交独立审核本次成功证据后判断关闭；所有owner/commit/sync
门禁仍待满足，无提交推送。当前外部操作已真实收敛并完成，保存本交付与精确结果版本后，
按v13核对原子递增v14待审核、归还controller。日常主环境仍disabled，后续启用不在本次
独立环境执行范围内。
