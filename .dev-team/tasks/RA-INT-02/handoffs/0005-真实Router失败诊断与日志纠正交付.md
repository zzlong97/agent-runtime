# 真实Router失败诊断与日志纠正交付

2026-10-29，dev-team-developer。输入state v7，沿原操作键
`sha256:2eab0cf360d5c00873fe26777ca30607944f483e50e1d1aae3cec525d12b48d6`。
完整安全事实见evidence/真实Router失败诊断.json；本次结论是实际尝试失败，非联调通过。

## 实际执行与收敛

原输入0004批准后启动独立18782/专用库/固定用户，正式模型构造、普通Router、三能力
均就绪。初进程6860/16404在首个请求前确认固定请求404/无回执/无Run，发现受控配置
原LOG_LEVEL=DEBUG，仅对独立进程改既有环境变量INFO并Ctrl+C重启，未改.env或源码。
该次退出1包含KeyboardInterrupt/CancelledError，不冒称exit0。实际进程204/22180、
托管session46891的两次日志初始化均明确INFO，仍沿原固定身份及输入版本。

唯一一次POST于03:21:11 UTC返回202；Run为5d19887d-7914-47ca-aada-9705b635f9d8，
Session为604e3845-d4a7-4317-b93a-5689dd9ac5c5。真实PG确认03:21:42终态failed、
ROUTER_CALL_FAILED，执行段约30.92秒，恢复次数0，input_payload已清除。没有换ID、
重送或改写终态。回执顶层accepted表示原202被受理，内部Run已更新为failed，不是业务成功。

最后原生psql只读实查：仅1 failed Run，活动Run0，Task/Operation行及携带Invocation/
远程身份的事件均0，未到远程Send；故新增远程Task为0。持久公开事件seq1～4分别为
run.started/message.started/message.finalized/run.failed，最终消息为incomplete、能力为空。
成功远程公共回答及成功HTTP SSE闭环未覆盖，不能冒领非空远程结果或日期业务验收。
同库13表102列，其中自有9表79列均有真实中文注释；三条固定用户allow已核对。

执行期间因用量中断导致托管会话失联，root在只读确认终态与0活动资源、核对模块/PID后
定点结束22180，父204随之退出。依据evidence/中断恢复核对.json及本轮再次进程查询，
该测试模块剩余进程0。保留库/回执/幂等事实；不将这段受控清理冒称全程正常Ctrl+C。

## 安全诊断与最小纠正

原底层error_type未能取得：专用每日文件仅315字节、两条初始化，检查的共享文件也无
原Router运行事件；控制台记录已不可恢复。不以31秒猜测超时，也未改模型、超时或重试。
事后零业务DNS/TCP/TLS1.3检查通过，实际主机dashscope.aliyuncs.com:443，配置模型
为deepseek-v4-flash-0731，无代理环境。现有SDK max_retries=2、request/HTTPX四项超时
均None，实际传输尝试数未采集；已有一次真实Router调用，不把0远程Task当作0模型费用。

获root明确只读核对范围后，于10:07:26 UTC通过正常SDK GET models.list，重试0、
超时15秒，HTTP200且配置模型精确在目录、无下一页。没有生成请求、没有输出认证头或
目录/错误正文。这只能证明当前目录及认证链路可用，不证明原生成/结构化解析为何失败。
后续需在安全日志恢复后按root记录的受控范围取证，不能自行再创建请求。

明确的本地问题仅在测试入口日志生命周期：内联app先配置每日Handler，随后Uvicorn默认
log_config重配关闭Handler。零网络复现before_written=true/after_written=false。
最小提取build_server并传log_config=None；没有生产Python或模型/路由/接口行为改动。
原执行源码快照evidence/联调入口修正前源码.py与执行输入SHA
1c9af04cbbfaa61e9522029e4770b2aac8f072bb9ebba8761659b871e0204df2逐字相同，完整差异
已写入诊断JSON，当前源码SHA为30d678dd6580f71e67aff092fc7d68158513e2fb734f7fcba91e53464ecdecf1。
手册仅同步原拒绝/恢复事实、此次失败与日志修正尚未线上复测的限制。

回归先红后绿：新增日志回归最初失败；独立原生构造顺序复现确认为日志丢失。最终命令
`.venv/Scripts/python.exe -X utf8 -m pytest tests/e2e/test_real_router_preparation.py tests/capabilities/test_semantic_manifest.py -q --tb=short`
退出0，16 passed in 4.24s，全部Fake/MockTransport，修正后未重启线上入口或新增生成。
旧139项是原准备版本证据，不冒称已对当前修正版重跑139项。

## 交回

外部尝试已知失败、没有未终结Task或结果未知操作，诊断及清理已完成，可将原operation
标已完成且结果明确失败。交待审核，不写整体验收通过。原准备独审因入口/测试/手册新
字节变化标需要重新验证，原报告不覆盖新版本；本次开发交付绑定当前产物及失败事实。
Root另行记录后续受控新本地请求与独立回执，本开发不自动发起。累计最多1新远程Task
约束不变，旧failed Run和原回执保留；主Runtime/主库、对方服务/源码/源数据/卷、.env
均未改，无提交或推送。负责人终验/提交/双同步门禁保持待满足，state递增后归还总控。
