# RT-001 结构化路由兼容性修复

## 授权与边界

负责人要求：“我在跟模型对话时，遇到了路由模型返回的结构化结果不符合 Schema的问题，需要现在修复。”
这是既有 Stage 3 的局部缺陷修复，依据 requirements 2.4/7.3、architecture 5/15.4；
不改变已批准 Schema、权限、Parent/Child 隔离、Run 或 SSE 契约，不启动 RA-001/MI-001。
本轮只授权实现、验证与审核，不提交、推送，不重启或修改用户已有会话。

## 已定位原因与最小方案

当前 LangChain OpenAI 实现默认采用 json_schema，但当前兼容端点未可靠遵守该约束。
无业务内容的真实请求复现 Router 缺少 capability_id、额外 capability/task_id；
英译汉守卫同样返回错误字段。显式 function_calling 的 Router 探针通过。
三个现存结构化调用均显式选择 function_calling，传递完整 Schema 并强制指定工具。
保留关闭 Schema 与候选集校验；不猜测缺失字段、不增加重试、配置项或模型替换。

## 验收与实施清单

- RT-A1：先用真实 ChatOpenAI/OpenAI SDK 加隔离 HTTP 替身复现故障，再修复三个入口。
- RT-A2：路由、普通聊天和英译汉守卫通过；模拟分段 SSE 工具参数，验证完整请求 Schema。
- RT-A3：缺失/额外字段和候选外 ID 仍拒绝；守卫拒绝前无可见 token 或 Child 历史推进。
- RT-A4：相关回归与默认自动化验证通过；真实端点合成请求验证，不读取/记录用户正文或凭据。
- RT-A5：保存精确产物指纹与实测结果，独立审核后仍待负责人终验；不自动提交或推送。

直接影响文件为 graph/router.py、两个 Capability 守卫及相应测试。短实施清单替代额外设计/
计划文档，遵守负责人关于局部纠正不重走审批、最小规划的偏好。
