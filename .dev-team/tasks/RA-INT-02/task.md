# RA-INT-02 日常聊天入口接入准备与真实Router联调

## 授权、设计与边界

负责人在已说明“交付收尾、日常静态接入、普通/chat真实Router验收”后回复“那继续联调开发”。
本轮按该明确续行要求完成同一RA阶段的接入准备，不启动MI或新Stage。原RA需求9.6～9.8
RA-AC-01/02/09、架构17.3～17.11及D-064～069继续有效，复用现有SDK、Registry、权限、
Run、Interrupt和SSE。RA-INT-01技术审核有效，R07已关闭；其成本/终验/提交/双同步门禁
仍保留，准备工作不等于整个RA阶段闭环，也不以本任务绕过进入下一Stage的门禁。

方案沿用现有静态机制：正式远程Manifest进入默认目录但enabled=false，按Registry既有
禁用分支不实例化远程Capability、不访问Card；启用时仅改该字段、显式allow固定用户、
配置既有REMOTE_A2A_DEVELOPMENT_MODE并正常重启。无新Source/配置项、默认授权、自动
发现、后台服务或产品管理入口。远程端仍为127.0.0.1:18080同源HTTP/none本机开发环境。
日常普通Router必须看到两个原本地能力及semantic_pattern_mining，不能固定远程选路或
使用RUNTIME_DEMO_MODE；当前真实模型配置存在，只检查布尔状态不输出凭据。

## 实施清单（已确认设计的最小执行计划）

- [ ] 先在tests/capabilities中新增正式Manifest/默认静态发现验证：禁用时无远程构造/Card，
  原两本地能力仍可用；启用的隔离测试配置进入真实StageOneRouter候选，保留四字段边界
  与显式权限规则。使用Fake模型/Remote作为自动测试，记录红灯及绿灯。
- [ ] 新增src/agent_runtime/capabilities/semantic_pattern_mining/manifest.yaml，沿用原联调
  参数，仅将enabled改为false并说明启用步骤；Schema、并发1、invocation、automatic+
  idempotent、900秒与取消宽限2秒及10秒HTTP规则保持不变。默认启动不连接真实Agent。
- [ ] 在现有docs/capability_development_guide.md直接更新当前交付状态并补本机日常接入
  的准确启用/授权/启动/恢复步骤；正式Manifest为事实源，不增加重复设计稿。授权使用既有
  Repository正常接口，用户来源为Settings.local_user_id，不在业务应用中默认授权。
- [ ] 在tests/e2e提供最小普通Router验证入口/探针（可复用现有工具，但不要修改已验收
  真实联调回执或旧测试事实源）。保留正式open_chat_service/settings/真实模型构造，
  与线上模式分离的离线测试仍注入Fake；不得把FixedRouterModel结果冒充真实选路。
  在线入口使用独立回环Runtime、独立测试库/用户和三个正常候选，测试目录/单次启用
  替换必须与主Runtime隔离，不在主源码目录临时开启远程。默认不启动、不调用在线模型，
  不自动POST；固定请求ID和先查旧结果规则必须可验证，未知结果不自动重试。
- [ ] 执行必要的Manifest/Registry/Bootstrap/Router及新增探针离线定向测试；新默认目录
  有三个Manifest但只有两本地active，修正受影响的精确发现数断言，禁止削弱默认安全断言。
  不因新增配置重跑已有效的六真实Task或全量UI/Stage测试。
- [ ] 保存精确产物、命令/结果、交接和待执行的真实操作范围，交独立审核。只有准备部分
  通过时登记“准备通过、真实Router尚未实跑”，不宣称日常接入或本任务全部验收完成。

## 外部操作与最终验收

本轮可做代码/配置/文档及本地Fake验证、受信Card只读预检。开发不改.env或凭据、不授予
主库权限、不启停主Runtime、不创建付费Task/在线模型请求、不访问对方源码或重置任何
Docker/卷/数据。本任务不是提交或推送授权，MI和既有无关脏文件原样保留。

准备和独立审核后，由总控集中给负责人准确操作清单：目标Runtime/库/用户、启用差异、
主Runtime是否需重启、同一已受信合成50条5类及2026-10-01～20数据范围、最多1个新远程
Task的普通Router闭环候选、固定请求ID和停止/回滚条件。原六Task额度已耗尽，不能沿用。
授权前不执行依赖步骤；若选择独立真实Router Runtime先验收，则先不影响主Runtime，
但必须如实说明它尚不是日常主环境启用。新增真实任务确认后沿同一Run追踪至实际终态。
任务最终验收需普通/chat经真实Router调用远程形成唯一非空公共回答、正确能力ID及持久
终态，并经过独立复核；负责人终验、提交和双同步门禁仍按项目规则办理。

总控持父RA-001及其他Task状态，开发仅持本任务state；不得跨员工派工或自行验收。
