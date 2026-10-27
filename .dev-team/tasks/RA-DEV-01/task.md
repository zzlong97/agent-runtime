# RA-DEV-01 远程传输、Agent Card 与 A2A Client 基础

## 目标与批准依据

交付父任务 RA-001 已批准 architecture 17.10 的第一项：Manifest transport、Agent Card
校验、官方 A2A Client 接口和 Fake；验收 RA-AC-01，及 RA-AC-07 的协议/安全单元部分。
不把这项局部交付称为整个 RA 阶段可用或真实接入已完成。

父任务 requirements 第 9 节、architecture 第 17 节、decisions D-064～069 已在规划时批准。
负责人已明确“现在开始RA-001任务”，并同意前序提交双远程同步；详见父任务
handoffs/0001～0003。技术落点以 0004-实施前技术核对.md 为准（sha256:daf7afed746b06d8f35c688eef70c1466c7569c9e42c413432a9859b5768f2fa）。
本拆分不改变批准语义，也不需要重新确认未变需求。MI 追加段不属于授权范围。

当前整文件指纹（仅批准所引用 RA 章节）：

- requirements：sha256:4c4737947a6b2dcafc04a94b62301f7b47e3614b42dea103ed3a85f965d8ff9b。
- architecture：sha256:3f4d0231880ec42975248165ad2cd0fde4380ffea17d3e80939b83f6800d60da。
- decisions：sha256:2157ebb51b5a309886e96759bf35b41f39d686b684acd1d47db51b78df6ef8e2。

前序产品基线 b3e855d 已推送并从 GitHub/Gitee master 实值确认一致；Compose PostgreSQL/
Redis healthy。开发前默认离线测试 679 passed、74 skipped；架构定向 129 passed、
1 deselected。它们只是变更前基线，不是本任务验收证据。

## 允许范围

- capabilities/manifest.py：关闭的 local/remote_a2a transport 判别联合；entrypoint 移入 local，
  拒绝旧顶层字段，不保留双轨；远程只允许 invocation scope。
- 两个内置 Manifest、测试 fixtures、Registry 用例及 docs/capability_development_guide.md
  契约示例做机械迁移；必要时同步 README 的直接契约引用，不改本地能力行为。
- capabilities/registry.py、现有 health.py：远程可信基础设施构造、启动期静态发现和健康，
  保留本地统一工厂、故障隔离、TTL 与四字段 Router 投影。
- capabilities/remote_a2a/：官方协议 v1.0.1、a2a-sdk==1.2.3 的仅 HTTP+JSON Client，
  Agent Card/Profile 校验、Send/Get/Cancel 三 REST 接口和受控错误；不走旧版/JSONRPC 回退。
- core/config.py、chat.py:open_chat_service：只接通本任务当前需要的安全策略、全局 HTTP
  超时、环境 Token 引用和资源清理。最小开发安全开关与 runtime_demo_mode 分离；
  不为将来增加未使用配置，不向普通 Capability 暴露 Client 或凭据。
- capabilities/regeneration.py：remote_a2a 统一在 Run/Parent Fork 创建前拒绝 Regenerate。
- pyproject.toml/uv.lock：实际解析并锁定最小官方 SDK 依赖，不装 all/gRPC/数据库等无用途 extra。
- 测试内共享 Fake A2A/ASGI：官方 1.0 wire，completed/input-required/working/cancel、
  messageId 幂等冲突和断连控制；不启动真实远程 Agent。

## 验收清单

- [ ] local/remote 关闭 Schema、中文字段说明、invocation 限制和旧格式拒绝；既有本地能力仍工作。
- [ ] 合法远程通过启动发现/健康进入四字段 Router；非法/不可达远程只隔离自身，Card 刷新
  不替换静态身份/接口，不从 Card 臆测其包含不存在的操作列表。
- [ ] 先校验来源再发凭据；HTTP(S)、userinfo/fragment、有效 origin、受控重定向、
  required extension、none 开发显式开关和 Card 安全方案校验均有负向测试。
- [ ] 使用官方 SDK 类型/REST 路径、A2A-Version:1.0、returnImmediately=true、文本模式；
  Get/Cancel/非阻塞 Send、Task 而非裸 Message 的 Profile 约束有可重复互操作测试。
- [ ] remote Regenerate 创建 Run/Parent Fork 前稳定拒绝；日志/事件/错误不泄露正文或凭据。
- [ ] Fake、相关回归和默认全量通过；先 RED 再实现，失败/未覆盖如实记录，不弱化原断言。
- [ ] 产物精确指纹和短验证交接保存，提交待审核并归还总控；独立审核不是开发自验。

## 禁止与后续门禁

本批不实现 Run 远程绑定/Operation 执行闭环、取消/恢复/删除屏障、文本 Interrupt/API/UI、
真实 Agent 适配/联调、MI/Lease/动态注册/跨 Run 远程上下文、新业务表或下一任务的产品分支。
本批远程发现与 Client 是后续依赖，不得伪报已接通远程 Run；默认运行配置不得挂入
尚不具备执行闭环的真实远程能力。

现有 Controller/Gateway 中断缺口可只做夹具级诊断，不能把诊断冒充正式探针通过。
正式 Fake A2A+PostgreSQL Interrupt/open Invocation 探针按 0004 放在 RA-DEV-03 开头；
受理后绑定前/绑定后崩溃恢复探针归 RA-DEV-02。它们通过前禁止真实联调。

不得读写对方 semantic-pattern-mining 仓库、修改 MI 规划、覆盖前序修复或操作 Docker
内部文件/命名卷。不连接真实模型或真实 Agent、不重启现有服务。本轮不提交、推送或发布。
后续独立审核、负责人终验、任务提交与双远程同步门禁按 AGENTS 记录，不自行关闭。
