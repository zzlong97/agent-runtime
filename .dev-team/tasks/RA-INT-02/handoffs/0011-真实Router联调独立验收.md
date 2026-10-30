# 真实Router联调独立验收

2026-10-29，dev-team-reviewer；输入state v15，审核键 `sha256:d1c26838e50b083e5f41c77ecb9134be2dbd5fb24f1d7268a18ef62fa9c00c0f`。
真实外部操作仍为13110a56…，执行输入与原键未改。本审核未参与本次真实执行。

## 结论

**当前独立环境的普通真实Router→远程Task→公共回答完成链路技术验收通过。**
关闭RA-INT02-R01中“真实成功链路尚未验证”的阻塞；首次ROUTER_CALL_FAILED原因仍未知，
保留为非阻塞事实，不能由本次成功倒推根因已修复。主Runtime未启用，语义算法/GPU质量
及负责人终验、提交、双同步不包含在本技术通过中。

## 独立刚执行的只读核验

- 本地原生PostgreSQL在固定agent_runtime_ra_int02_29e46ecf执行BEGIN READ ONLY/COMMIT，
  退出0。新d3e517ba…请求的Run f40fca5f…为completed/恢复0/input清空，旧56a29c21…的
  5d19887d…Run仍failed/ROUTER_CALL_FAILED；全库活动Run0。Invocation aa2626e1… started/
  completed各1，binding1；local Task32c25214…与remote2202a2e9…/contextfbda3c18…一致，
  指纹48f71b…、初始messagecd28b5ef…对应唯一remote_send账本且succeeded。
  public持久事件seq1/2/10/11，finalized能力/消息ID及completed终态精确匹配，未混入内部数据。
  本地Task仍active是独立业务生命周期投影，本审核不人为结束它。自有9表、79/79字段中文注释完整。
- 独立HTTP GET唯一已知远程Task，带A2A-Version:1.0、禁重定向，200/Task与context匹配、
  TASK_STATE_COMPLETED。Artifact非空330字符，MD5 1a0e47097989d26c0f45be5539e5d585，
  含两端日期，与已保存公开AI长度/MD5相同；未输出正文、metadata私有值或原始响应。
- 远端原生PostgreSQL固定semantic_a2a_integration执行READ ONLY，退出0。该Task仅1条
  去重消息且cd28…匹配；唯一business Run824e4405…的thread/turn指向同Task、completed，
  UTC09/30 16:00至10/20 16:00正确覆盖上海10月1～20日含结束日。源计数50/5；本次
  common_clauses/trace均0，非空汇总和完成状态不等于语义挖掘或GPU推理质量通过。
- 35项当前输入artifacts一致；代码三项及core/model.py与实际执行记录SHA精确相同。
  原受审日志旁路未改真实route/模型/Prompt/结果，21项同版离线证据复用，本轮不重跑21/139。
  实际专用日志安全投影显示普通Router面对三候选选择semantic_pattern_mining、
  confidence0.98、1110.31ms；没有固定模型注入或演示模式。
- 模块进程及18782监听均0。关闭记录为确认双方终态后单次Ctrl+C、exit1且有
  KeyboardInterrupt/CancelledError及应用关闭日志，不将其改写为clean exit0或强杀。
  原回执SHA6293fd8a…和执行前源码SHA1c9af04c…保持原字节，累计远程Task额度已用1/1。

## 同版HTTP证据和文档范围

本次执行前已核对实际工具字节，复用0010及真实结果JSON中刚执行的probe verify退出0、
公共Human/AI各1、AI完成/非空/正确能力和消息ID的证据；SSE关闭Schema、event头和Run
身份一致，seq1/2/8/10/11唯一递增、唯一finalized与terminal，终态后重连空。
seq8为实时delta，PG持久公共事件不含它是既有契约；不把两种证据来源混为同一查询。
Runtime已关闭，本审核未为复验启动会写setup/权限的入口，也未冒称重新运行浏览器交互；
/chat GET200仅支持页面可达，既有未改UI的相关证据复用。

指南新SHA c34a0d9a…只有表头改已执行、额度改用满及末尾追加本次completed开发事实三处。
独立反向恢复这三处，精确得到执行时原SHA f96f4ab0…；新增文字与本次核验及限制一致。
当前两个相关checkpoint的文档引用据本报告刷新；原0007/0008及执行输入仍只证明各自原
版本，不反向声称它们已审核新字节，不更换原执行输入/key或修改真实回执。

## 交回及限制

无新增阻塞。首次模型异常未追溯、结果表0条、主环境未启用及未重复交互UI均如实保留。
本审核只做两库原生只读、已知Task GET、文件/日志安全投影与资源状态查询，无生成、
新Task、Runtime启动、DDL/DML、主环境/对方服务操作、被审代码修改或提交推送。

新增开发验收checkpoint绑定当前产物、真实结果和本报告；原子保存state v16为
待负责人决策、resume_from=已验收，归还controller，清除R01当前成功链路阻塞。
负责人终验/任务提交/双远程同步仍待满足，不宣称整个RA阶段已经闭环。

