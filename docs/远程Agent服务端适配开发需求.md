# 远程 Agent 服务端适配开发需求

本任务交给 `semantic-pattern-mining` 开发团队实施：在已有业务之外补齐 A2A 服务端门面、持久任务、幂等提交、查询、可靠取消、文本补参和文本结果。完成后由负责人转交连接及测试材料，再与 AgentRuntime 团队联调。本文包含实现与自测所需契约，不要求访问 AgentRuntime 私有仓库。

兼容参照：AgentRuntime 提交 `29aa34d1d5d39328ac0a39150c9f4dc06dc4fbfe`（简称 `29aa34d`）；协议语义 A2A 1.0，规范参照 v1.0.1，客户端互操作参照官方 Python `a2a-sdk==1.2.3`。SDK 版本用于验证线协议，不强制服务端采用 Python、该 SDK 或特定框架。本文是已批准 RA-001 需求与 Profile 的实施投影，不是新的业务算法或平台方案。

本文所有域名、端口、ID、日期、请求正文和结果数字均为**合成示例**；`agent.example` 不是实际服务地址，`EXAMPLE_TOKEN_NOT_A_CREDENTIAL` 不是凭据。示例不授权访问真实业务数据、执行指定日期任务或产生真实副作用。真实联调用的隔离数据、日期及副作用限制由对方交付、负责人确认。

## 1. 工作范围与职责

| 对方开发团队交付 | AgentRuntime 团队负责 |
| --- | --- |
| 在现有业务前增加符合下文的 A2A HTTP+JSON 服务边界 | 将该服务作为普通 Capability 接入现有 Router、Run、SSE 与 `/chat` |
| 自治管理业务 State、内部消息、结构化结果、业务数据库及任务执行资源 | 保存本地 Invocation 与远程 Task/context 的绑定，按稳定消息 ID 恢复 |
| 持久受理、按消息幂等、后台执行、Get、Cancel、补参与 Artifact | 轮询 Get，展示文本问题，把用户文本答案发送回同一 Task |
| 服务端单测、契约测试、部署材料、实际改造成本 | Runtime 自测及双方真实场景联合验收、分开汇总成本 |

复用当前语义挖掘算法，不要求重做业务算法；可以保留私有 `POST /api/agent/invoke`，但 Runtime 不调用它，也不以它的 JSON 结构为正式契约。内部 State、队列、存储与恢复实现由对方选择；不要求引入 LangGraph、共享 Runtime 数据库或增加指定表/字段。Runtime 不解析远程私有业务字段，也不写入对方业务数据。

首期仅支持：静态 Agent Card、HTTP+JSON/REST、SendMessage、GetTask、CancelTask、当前用户文本、同一次 Invocation 对应一个远程 Task/context、顺序文本补参、最终文本 Artifact。只通过 Get 轮询，不要求实现流式、Push、文件、跨 Run 上下文、动态表单、管理平台、OAuth2/mTLS、RBAC 或多实例改造。

## 2. 接口与线协议

### 2.1 固定接口

以下 base URL 示例为 `https://agent.example/api`，由 Card 的 `supportedInterfaces[].url` 决定。不要将 `/message:send` 当作 base URL，否则客户端会重复拼接路径。

| 操作 | HTTP 方法与示例路径 | 请求/成功响应 |
| --- | --- | --- |
| 发现 | `GET /.well-known/agent-card.json` | Agent Card；实际 Card URL 由静态配置交付 |
| 初次/补参 SendMessage | `POST /api/message:send` | SendMessageRequest / `{"task": Task}` |
| GetTask | `GET /api/tasks/{id}` | 无请求正文 / Task 本身 |
| CancelTask | `POST /api/tasks/{id}:cancel` | `{"id":"任务ID"}` / Task 本身 |

所有 Runtime 请求（含 Card）带 `A2A-Version: 1.0`。POST 请求发送 `Content-Type: application/json`，服务端须接受；官方 SDK 的成功操作响应使用 `application/a2a+json`，Card 与错误示例使用 `application/json`。成功响应须为合法 JSON，不能返回 HTML、空 202 或私有任务摘要。本文示例使用 HTTP 200 返回可查询 Task；Send 返回裸 Message 不满足本 Profile。

`tenant` 首期可省略。如果 Card 确实声明 `tenant: "tenant-a"`，客户端将调用 `/api/tenant-a/message:send`、`/api/tenant-a/tasks/{id}`、`/api/tenant-a/tasks/{id}:cancel`；Send 正文还含 `tenant`，Cancel 正文还含 `tenant` 和 `id`，Get 的 tenant 只在路径中。tenant 当前仅允许字母、数字、`_`、`-`。无需为本任务引入租户业务能力。

Task 的 JSON 字段是 `id`、`contextId`、`status`；后续 Message 使用 `taskId`、`contextId`。`id` 必须非空、稳定、可安全放进单段 URL：匹配 `^[A-Za-z0-9_-][A-Za-z0-9_.~-]*$`，且不是 `.` 或 `..`；推荐使用不含路径符号的 UUID。`contextId` 必须非空、稳定。所有 Send/Get/Cancel 都必须返回同一任务真实身份。

JSON 枚举使用下面的准确拼写，不能混用旧版 `working`、`input-required` 或 `cancelled` 字符串；本文正文中的这些词只是语义简称。

| JSON `status.state` | 本次用途 |
| --- | --- |
| `TASK_STATE_SUBMITTED` | 已持久受理、待执行 |
| `TASK_STATE_WORKING` | 正在执行；取消尚未确认时也只能保持真实非终态 |
| `TASK_STATE_INPUT_REQUIRED` | 等待本 Task 的文本补参 |
| `TASK_STATE_COMPLETED` | 确认完成，必须有非空文本 Artifact |
| `TASK_STATE_FAILED` | 已确认业务执行失败 |
| `TASK_STATE_CANCELED` | 已确认执行停止，注意只有一个 `L` |
| `TASK_STATE_REJECTED` | 已确认拒绝；范围拒绝还须满足第 7 节 |

`TASK_STATE_UNSPECIFIED` 不合法；不要发明 `CANCELING`、`UNKNOWN` 等状态。协议存在 `TASK_STATE_AUTH_REQUIRED`，但本 Profile 不提供交互鉴权流程，Runtime 会按任务失败处理；静态令牌问题应使用 HTTP 401/403。

### 2.2 最小 Agent Card 示例

以下给出含一个业务 skill 和静态 Bearer 声明的合法 Card。`version` 是 Agent 版本，`protocolVersion` 才是协议版本。Card 不声明三操作清单，三操作及幂等要通过契约测试验证；不要添加私有 `methods` 等未知字段。

```http
GET /.well-known/agent-card.json HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL

HTTP/1.1 200 OK
Content-Type: application/json

{
  "name": "语义模式挖掘 Agent",
  "description": "对给定日期范围内的授权数据执行语义模式挖掘，缺参时询问用户。",
  "version": "1.0.0",
  "supportedInterfaces": [
    {
      "url": "https://agent.example/api",
      "protocolBinding": "HTTP+JSON",
      "protocolVersion": "1.0"
    }
  ],
  "capabilities": {},
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "semantic-pattern-mining",
      "name": "语义模式挖掘",
      "description": "按用户指定日期范围分析授权数据并提供结果摘要。",
      "tags": ["语义模式挖掘"]
    }
  ],
  "securitySchemes": {
    "token": {"httpAuthSecurityScheme": {"scheme": "bearer"}}
  },
  "securityRequirements": [{"schemes": {"token": {}}}]
}
```

输入、输出默认模式须包含 `text/plain`；不要要求 Runtime 不支持的 required extension。`securityRequirements` 是 OR 列表，每项 `schemes` 内是 AND，本次只使用可由同一静态 Bearer 满足、无 scopes 的要求。

显式开发环境可使用 `none`：Card 移除 `securitySchemes`、`securityRequirements`，请求不带 Authorization；HTTP 及无认证还需要 Runtime 显式设置 `REMOTE_A2A_DEVELOPMENT_MODE=true`。默认环境要求 HTTPS 和 Bearer。不要把 Token 写入 Card。

### 2.3 初次 Send：先返回持久任务

```http
POST /api/message:send HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL
Content-Type: application/json

{
  "message": {
    "messageId": "11111111-1111-5111-8111-111111111111",
    "role": "ROLE_USER",
    "parts": [{"text": "请对隔离测试数据在2026-09-01至2026-09-03期间执行语义模式挖掘。"}]
  },
  "configuration": {
    "returnImmediately": true,
    "acceptedOutputModes": ["text/plain"]
  }
}

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "task": {
    "id": "demo-task-01",
    "contextId": "demo-context-01",
    "status": {"state": "TASK_STATE_SUBMITTED"}
  }
}
```

Runtime 不发送完整对话、System Prompt、Checkpoint、私有 State 或业务 JSON。初次 Message 不带 `taskId/contextId`；服务端分配并持久保存二者。`returnImmediately=true` 必须落实为：完成持久受理和稳定身份分配后尽快响应，业务在独立后台推进。不能把现有同步长连接换个路径、等完整业务完成再返回。即时终态可以直接返回，但长任务不能依赖连接存活；后台任务必须能在服务重启后恢复或给出可证实的真实结果。

Runtime 当前默认单次 HTTP 超时为 10 秒，属于接入等待配置，不是对方业务任务的运行时长限制。请在交付中记录实际受理延迟及测试命令，不把业务耗时藏入 Send。

### 2.4 Get：轮询与完成结果

```http
GET /api/tasks/demo-task-01 HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "id": "demo-task-01",
  "contextId": "demo-context-01",
  "status": {"state": "TASK_STATE_WORKING"}
}
```

对同一 URL，确认完成后的响应示例为：

```http
HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "id": "demo-task-01",
  "contextId": "demo-context-01",
  "status": {"state": "TASK_STATE_COMPLETED"},
  "artifacts": [
    {
      "artifactId": "demo-result-01",
      "parts": [
        {"text": "实际采用日期：2026-09-01至2026-09-03；数据范围：隔离测试数据。\n"},
        {"text": "结果摘要：本合成示例识别出2类模式。"}
      ]
    }
  ]
}
```

Get 是只读操作，重复查询不得启动或推进第二份业务任务。重复终态查询必须仍是同一真实终态及可读取结果，不能回到 working、重新执行或丢失 Artifact。

## 3. 持久任务、幂等与恢复

1. 在确认受理前，持久保存足以恢复的 Task 身份、状态与消息受理事实；响应丢失后仍能取回相同任务。仅用进程内字典、线程 ID 或随 HTTP 连接结束而取消的后台协程不满足要求。
2. 以 `messageId + 请求指纹` 去重。相同 ID、相同请求内容的任意重送必须返回相同 `taskId/contextId`，允许返回任务当前快照；不得重复创建或执行业务。同 ID、不同请求内容稳定返回 HTTP 409，原任务不得被覆盖或推进。这些保证需跨并发重送、服务重启继续成立。
3. 初次消息 ID 由 Runtime 的 Invocation 确定性生成；对方将其视为不透明标识，不自行重算或替换。初次消息指纹必须区分实际请求内容，包括有序文本 Parts、角色、目标 Task/context（初次缺省）、配置及使用时的 tenant；JSON 键顺序变化不应造成误冲突。指纹的具体散列和存储实现由对方决定，不得凭去空格、改日期、截断文本把不同业务请求合并。
4. 补参使用新的稳定 `messageId`，并明确带原 `taskId/contextId`。Runtime 按同一 Invocation 与远程问题 `messageId` 确定补参 ID；同问题恢复重送 ID 不变。每条补参同样比较完整请求指纹：同 ID 同答案只消费一次，同 ID 改答案冲突，不重新推进任务。先核对已受理消息，再判断当前是否仍在等待该问题，避免重复旧答案被当作下一题答案。
5. 远程已受理但 Runtime 尚未保存绑定时，Runtime 会用原 ID、原请求重送初次 Send；已保存绑定后主要使用 Get。补参发送响应丢失时也会按原 ID 重送。两条路径均不得创建第二个远程 Task 或重复业务副作用。
6. Runtime 断线、SSE/浏览器断开或 Runtime 进程重启都不等于取消；远程 Task 独立存活。对方服务重启后须能凭 Task ID Get，或凭原消息 ID 幂等取回任务，不把丢失内存状态伪装成任务 failed。

不指定内部新表或队列设计。请说明当前持久化、任务接管和副作用去重边界，以及重启如何恢复已受理任务。不要仅凭“使用 SDK”宣称具备持久化或业务幂等；业务写入也不能因重试重复。

## 4. 文本补参：同 Task 内顺序完成

日期等业务参数缺失时由 Agent 提问，不猜日期、不默认今天，也不要求 Runtime 理解业务字段。已有 `needs_input` 在服务边界转换为 `TASK_STATE_INPUT_REQUIRED`。

每个等待问题必须带非空文本提示与稳定、非空 `status.message.messageId`。同一个待回答问题在重复 Get、重复 Send 取回及双方进程重启后 ID 不变；产生新问题必须用新 ID，即使提示文字完全相同。当前 Runtime 将文本 Parts 原序连接后去掉两端空白作为提示，长度需在 1～4000 字符内。

下列独立示例任务的初始文本为“请对隔离测试数据做语义模式挖掘。”，按第 2.3 节格式发送，初次消息 ID 改为 `22222222-2222-5222-8222-222222222222`。初次 Send 可返回下列 Task 的 `{"task": ...}` 外包；Get 响应则为：

```http
GET /api/tasks/demo-task-02 HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "id": "demo-task-02",
  "contextId": "demo-context-02",
  "status": {
    "state": "TASK_STATE_INPUT_REQUIRED",
    "message": {
      "messageId": "demo-question-01",
      "role": "ROLE_AGENT",
      "parts": [{"text": "请补充开始日期。"}]
    }
  }
}
```

用户在 Runtime 的 text Interrupt 填写答案后，Runtime 发送普通文本 Message，沿用同一 Task/context，不新增远程任务，也不把日期改造成私有 JSON：

```http
POST /api/message:send HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL
Content-Type: application/json

{
  "message": {
    "messageId": "33333333-3333-5333-8333-333333333333",
    "role": "ROLE_USER",
    "taskId": "demo-task-02",
    "contextId": "demo-context-02",
    "parts": [{"text": "2026-09-01"}]
  },
  "configuration": {
    "returnImmediately": true,
    "acceptedOutputModes": ["text/plain"]
  }
}

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "task": {
    "id": "demo-task-02",
    "contextId": "demo-context-02",
    "status": {
      "state": "TASK_STATE_INPUT_REQUIRED",
      "message": {
        "messageId": "demo-question-02",
        "role": "ROLE_AGENT",
        "parts": [{"text": "请补充结束日期。"}]
      }
    }
  }
}
```

第二轮答案再次发至相同路径：

```http
POST /api/message:send HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL
Content-Type: application/json

{
  "message": {
    "messageId": "44444444-4444-5444-8444-444444444444",
    "role": "ROLE_USER",
    "taskId": "demo-task-02",
    "contextId": "demo-context-02",
    "parts": [{"text": "2026-09-03"}]
  },
  "configuration": {
    "returnImmediately": true,
    "acceptedOutputModes": ["text/plain"]
  }
}

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "task": {
    "id": "demo-task-02",
    "contextId": "demo-context-02",
    "status": {"state": "TASK_STATE_WORKING"}
  }
}
```

随后 Get 必须仍为 `demo-task-02/demo-context-02`，并最终返回第 2.4 节形式的 completed 文本 Artifact。等待时一次只呈现一个问题，允许两轮及以上顺序补参。答案格式、日期合法性和日期先后顺序均由 Agent 判断；非法业务答案需产生新问题 ID 和清楚的文本提示，不要求 Runtime 提供日期解析器或多字段表单。重复已受理答案不得再触发一次业务执行或生成第二次相同问题。

## 5. 可靠取消与终态

Cancel 必须到达真实执行层，并停止后续业务推进；无法撤销已经发生的业务事实时按事实说明，不能假装回滚。本次不承诺回滚或补偿已经发生的业务副作用。只有已经确认任务执行停止，且不会继续进行该任务的业务工作，才能报告 `TASK_STATE_CANCELED`。

```http
POST /api/tasks/demo-task-03:cancel HTTP/1.1
Host: agent.example
A2A-Version: 1.0
Authorization: Bearer EXAMPLE_TOKEN_NOT_A_CREDENTIAL
Content-Type: application/json

{"id": "demo-task-03"}

HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "id": "demo-task-03",
  "contextId": "demo-context-03",
  "status": {"state": "TASK_STATE_CANCELED"}
}
```

以上响应仅表示**已确认停止**的例子。若仍在取消中，应尽快返回该 Task 的真实受支持非终态，例如 `TASK_STATE_WORKING`；实际仍等待输入时可保留 `TASK_STATE_INPUT_REQUIRED`。不增加取消中枚举，不以收到 Cancel、设置标志、停止响应或发出队列消息当作停止完成。Runtime 会再 Get 确认同一 Task/context 的终态。

必须验证运行中及等待补参时都可取消。等待任务取消后，不能再因旧答案重送恢复业务。Cancel 重复调用不得重复产生副作用或改变已经成立的终态；如果任务已 completed，保持 completed 及原 Artifact，可以返回真实终态 Task，或按标准返回 `TASK_NOT_CANCELABLE`，后续 Get 仍须给出原真实终态，不能为了迎合取消改成 canceled。取消与完成竞态也按真实先成立的终态处理。

网络中断、取消请求/响应丢失、依赖失联或暂时不能确认任务是否停止时，不返回虚构 failed/canceled。保留 Task 身份和恢复证据，待恢复后由 Get 给出真实状态。Runtime 在未知期间保持 recovering/cancel_requested，并阻止同一 Session 新任务；这不要求服务端理解或写入 Runtime 状态。

交付时说明活动任务、终态 Task/Artifact、已受理消息幂等事实和取消证据的保留/清理策略及可用恢复窗口。恢复窗口内不得提前遗忘仍可能被查询或重送的任务。本文不擅定新的保存天数，也不要求跨系统删除 API；本地 Session 删除不自动删除 Agent 业务数据。需要缩短保留期或提前清理时，应先交负责人评估对恢复验收的影响。

## 6. 最终文本 Artifact

completed Task 必须包含至少一个 Artifact，且至少一个 Part 的 `text` 去掉空白后非空。Runtime 将 `artifacts[]` 的数组顺序、各 `parts[]` 顺序中的文本直接连接，不自动插入换行或分隔符；服务端应在文本中提供必要换行，并在 Get 中提供完整最终全文，不能只返回最后一个增量片段。

最终文本须包含实际采用的日期范围、影响本次执行的关键参数和用户可读结果摘要；通过补参得到的参数也须体现，因为补参文本不会成为 Parent 新 HumanMessage。无匹配数据时仍给非空的事实说明。结构化 `data`、业务记录、内部 State 和工具结果由 Agent 自治保存，首期不靠 Data/File Part 向 Runtime 交付结果。

## 7. 确定失败、普通拒绝与合法范围拒绝

业务已经确定失败用 `TASK_STATE_FAILED`；普通 `TASK_STATE_REJECTED` 也会被 Runtime 按远程任务失败处理，不能借其文本内容变成 unsupported。不要把临时 HTTP 失败、状态未知、内部存储失联或取消未确认强行投影成确定业务失败。

只有在**业务接受、业务 State/Checkpoint 持久化和任何业务副作用之前**认定当前任务超出能力范围，才可承诺合法 `OUT_OF_SCOPE`。该路径允许为协议去重/查询保留最小 Task 受理事实，但不能推进业务、输出公开文本或生成业务/Child Checkpoint；服务端契约测试须证明这些条件。

Get 的精确响应示例（初次 Send 需在同一对象外包 `task`）：

```http
HTTP/1.1 200 OK
Content-Type: application/a2a+json

{
  "id": "demo-task-rejected",
  "contextId": "demo-context-rejected",
  "status": {"state": "TASK_STATE_REJECTED"},
  "metadata": {"control_signal": "OUT_OF_SCOPE"}
}
```

控制字段严格位于 Task 的 `metadata.control_signal`，值严格为 `OUT_OF_SCOPE`；没有 `artifacts`，也不在 status.message、history 或其他公开输出中附带拒绝正文。不要改成 `controlSignal`、字符串错误描述或自定义顶层控制字段。Runtime 将通过 Get 再确认同一 Task/context；认证失败、协议错误、执行后的失败和含 Artifact 的拒绝均不能使用该例外。范围判断通过后再发现业务参数不合法，应按文本补参或真实业务失败处理，不能声称“零业务推进”范围拒绝。

## 8. HTTP 错误与安全

### 8.1 错误返回

HTTP 错误和 Task 状态是两个层次。服务端应使用正确 HTTP 状态及脱敏、稳定的 JSON 错误，不能 HTTP 200 包私有 `{"success":false}`，也不能在 HTTP 错误中编造任务终态。标准 A2A 错误可使用第一个示例的 ErrorInfo；本 Profile 的消息 ID 冲突使用 HTTP 409，不要求发明 A2A 标准 reason。

同 ID 不同内容的冲突示例：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{
  "error": {
    "code": 409,
    "status": "ALREADY_EXISTS",
    "message": "messageId 已被不同请求使用"
  }
}
```

确实不存在的 Task 查询示例，`error.code` 是 HTTP 数字码，不是旧 JSON-RPC 的负数码：

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{
  "error": {
    "code": 404,
    "status": "NOT_FOUND",
    "message": "任务不存在",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "TASK_NOT_FOUND",
        "domain": "a2a-protocol.org",
        "metadata": {}
      }
    ]
  }
}
```

服务暂时不可读取持久存储时使用适当 5xx，而不是把“无法查询”当成“确实不存在”。已受理 Task 重启后变成 404 属于持久恢复不合格。

| 远程事实/响应 | Runtime 的受控分类与本次要求 |
| --- | --- |
| HTTP 401 / 403 | `CAPABILITY_REMOTE_AUTH_FAILED`；令牌缺失/无效或权限拒绝，非暂态重试 |
| HTTP 400、404、409，或非法 JSON/Schema/身份 | `CAPABILITY_REMOTE_PROTOCOL_ERROR`；接口/契约错误或冲突，不能解析错误正文推断任务结果 |
| 网络断连、请求超时、HTTP 5xx | `CAPABILITY_REMOTE_UNAVAILABLE`；仅在幂等保证下可安全恢复，请求是否已受理仍可能未知 |
| 已查询确认 `TASK_STATE_FAILED`、普通 rejected | `CAPABILITY_REMOTE_TASK_FAILED`；确定的远程任务失败 |
| 已受理但无法确认状态；Cancel 无法确认 | 非终态诊断 `CAPABILITY_REMOTE_STATE_UNKNOWN` / `CAPABILITY_REMOTE_CANCEL_UNCONFIRMED`；保留绑定继续恢复，不等于 run.failed |

上述 `CAPABILITY_REMOTE_*` 是 Runtime 生成的内部受控码，不要求远程把它们作为 A2A 错误或业务状态输出。Runtime 在已有远程任务事实时会把 HTTP 异常保留为待确认，不会仅凭 4xx/5xx 擅自结束远程业务。标准不可取消错误可按 HTTP 400、`status=FAILED_PRECONDITION`、ErrorInfo `reason=TASK_NOT_CANCELABLE` 返回；它不证明任务已经停止，必须仍可 Get 确认实际结果。

### 8.2 来源、认证与脱敏

- 静态认证只用显式开发 `none` 或 Bearer。真实令牌只经双方批准的安全环境配置注入；交付环境变量名（如 `SEMANTIC_AGENT_TOKEN`），不在文档、Card、URL、代码、日志、测试回执或公开响应交付令牌值。Card 与业务接口都要能接受同一受控身份；不要依赖浏览器登录、Cookie 或临时跳转。
- 已批准架构允许同源，或将来由配置显式允许的来源；**兼容参照 29aa34d 当前只支持同源，没有额外来源白名单**。本次 Card 和 interface 必须同 scheme、规范化 host、有效 port；路径可不同。生产 HTTPS；拒绝 URL userinfo、片段及全部重定向，interface URL 不能带 query。确需跨源属于待负责人另行裁决的差异，不在本任务默许新增配置。
- Agent 名称、版本、提供方、来源 URL、接口、tenant、输入输出模式、认证声明参与 Runtime 的稳定身份核对。未终结 Task 的恢复期间须保持这些身份稳定，不能每次启动随机生成或悄然替换身份/地址；计划变更须先与 Runtime 团队交接，不能让旧 taskId 被送到替代服务。
- 记录受理、重复/冲突、开始、补参、取消、完成/失败等中文业务事件；只记录所需的关联 ID、状态、受控错误类型/码、耗时。不得记录 Authorization、Token、数据库凭据、用户/模型正文、System Prompt、完整请求/响应、私有 State/Checkpoint 或工具原始参数/结果；异常日志、HTTP 中间件与 SDK 遥测也需核查。最终 Artifact 是授权返回给用户的正文，不是可以复制进日志的正文。
- 错误 `message` 与 `details.metadata` 只放脱敏诊断，不把 SDK 异常原文、堆栈、内部地址或业务数据照搬给调用者。

## 9. 对方实施与验收要求

按“协议门面与持久受理 → 幂等/恢复 → 补参/取消/结果 → 契约测试与交付材料”完成同一适配任务即可，不要求额外平台建设。对方先在自己的隔离环境运行以下测试并提交真实命令、结果、基线/最终版本、可核对的 ID 与执行计数。状态栏应填实际通过/失败/未执行，不能把本文示例当测试通过证据。

| 编号 | 对方自测/契约测试步骤 | 必须证明的结果 |
| --- | --- | --- |
| EXT-01 | 获取 Card，按其 base URL 依次 Send/Get/Cancel；分别测试 Bearer 与显式开发 none（如交付） | Schema、header、枚举、方法、路径、认证一致；三操作真实可用；不依赖私有接口 |
| EXT-02 | 用业务可用的完整文本参数启动耗时任务；记录 Send 延迟后轮询 | 持久受理即响应，独立后台推进，唯一 Task/context，completed 非空结果含实际参数 |
| EXT-03 | 缺少日期启动；Get 多次、重启服务再 Get；补充合法日期 | 非空问题、稳定问题 ID；不猜日期；同 Task/context 继续并完成 |
| EXT-04 | 顺序两轮提问及回答；再提交非法业务答案；测试同文新问题 | 每个新问题 ID 不同，旧问题重启仍稳定；业务校验在 Agent 侧；一次仅一待答问题，非法答案再提新问题 |
| EXT-05 | 同 ID 同初次请求串行/并发重送；同 ID 改文本/目标/配置；服务重启后重复 | 同内容回同 Task，仅一份业务受理/执行；不同内容稳定 409，原任务不变 |
| EXT-06 | 同一补参 ID 同答案重复；同 ID 改答案；已进入第二题后重送第一题答案；重启后重复 | 同 Task；每答案仅消费一次；冲突不推进；旧答案不误用为新答案 |
| EXT-07 | Send 已持久受理后丢弃响应/断线；服务重启；原 ID 重送并 Get | Task/context 不变、幂等事实存在、业务创建/副作用不重复，无内存任务丢失 |
| EXT-08 | 模拟调用者重启：已取得 ID 后只 Get；未取得 ID 时原请求重送；初次/补参各覆盖 | 两种恢复路径都不新建任务、不重复业务；不因调用连接断开取消 |
| EXT-09 | 运行中 Cancel；等待补参 Cancel；取消后重送旧答案；重复 Cancel/Get | 只有确认停止才 canceled；业务停止且不再由旧答案恢复，终态真实稳定 |
| EXT-10 | Cancel 请求受理但响应丢失；暂时中断查询/取消依赖再恢复；完成与 Cancel 竞态 | 未知不伪造 failed/canceled；恢复可确认原任务；已 completed 保持原结果 |
| EXT-11 | completed 多 Artifact/多 text Part、空白结果与无匹配数据 | 文本按数组顺序可完整拼接；最终文本非空，含实际日期及关键参数；空结果不能假 completed |
| EXT-12 | 确定业务失败、普通 rejected、合法范围拒绝及带 Artifact 的拒绝分别测试 | failed 与拒绝有别；仅精确 metadata 且零输出/业务推进/Checkpoint 的路径合法 OUT_OF_SCOPE |
| EXT-13 | 缺失/错误令牌、非法 JSON/版本、ID 冲突、不存在 Task、存储暂不可用；检查错误与日志 | HTTP 错误与 Task 状态区分；稳定、脱敏；无 Token/正文/State/原始工具结果泄露 |
| EXT-14 | 核对持久化、幂等与终态证据保留和部署重启流程 | 交付实际恢复窗口及清理条件；部署升级不意外遗忘活动任务或去重事实 |

服务端重启演练由对方控制自己的隔离部署，测试中记录业务执行/副作用计数及持久 Task/消息关联证据；不能仅用 HTTP 响应数量证明幂等。用测试替身模拟底层业务失败、取消未知等，不以真实生产副作用制造故障。

对方自测通过后，双方另行执行真实 Runtime 四场景，均需基于负责人确认的目标、认证环境变量与隔离数据：

| 联合场景 | 双方共同观察的验收结果 |
| --- | --- |
| 参数齐全直接执行 | 现有 `/chat` 经 Router 调用真实 Agent；唯一非空 AIMessage、实际参数、Run/SSE 结果一致 |
| 缺少日期后补参 | 页面出现文本输入；沿同一 Run/Invocation/远程 Task/context 恢复，不新增 HumanMessage；最终摘要体现补参日期 |
| 执行中取消 | Cancel 到达真实业务并确认停止；确认前 Runtime 保留 cancel_requested，未知不假结束 |
| Runtime 重启恢复 | 在“远程受理、本地绑定前”和“绑定后、终态前”演练，连续恢复超过3次仍不新建远程业务任务；实际最终结果唯一 |

联合验收还将覆盖等待补参恢复、幂等答案、Session 删除屏障及错误脱敏。Runtime 的数据库、页面、恢复次数与 Session 占用由 Runtime 团队验证；对方配合提供同一远程 Task 的持久状态和执行计数。双方真实四场景尚未执行，Fake 或服务端单测不能替代真实验收。

## 10. 交付清单与接入成本

对方完成编码后，请按以下表提供材料，负责人据此转交 Runtime 团队准备联调。缺少材料明确写“缺项/未执行”及原因，不猜测值。不要直接发送真实 Token。

| 材料 | 要求 |
| --- | --- |
| 版本与变更范围 | 适配前基线 commit、最终 commit 或不可变 tag、工作区状态；修改文件清单/数量、有效代码及测试增减行数与统计命令 |
| 发现与操作地址 | 可访问的实际 Agent Card URL、Card JSON（无密钥）、HTTP+JSON interface base URL、是否使用 tenant；确认同源 |
| 认证 | Bearer 或显式开发 none；Runtime/服务端所需环境变量名、安全注入方式及部署限制，不交 Token 值 |
| 启动与部署 | 可复现的启动/部署/停止命令、端口、依赖版本、必要环境配置名、持久存储准备与服务重启方法 |
| 生命周期实现说明 | 持久受理与后台执行边界、Task/context 生成、指纹语义、初次/补参/副作用去重、Get、业务取消确认、失败与未知区分 |
| 补参与输出 | 真实业务所需参数的文本示例、缺日期提示、非法答案处理、问题 ID 稳定性；脱敏的最终文本 Artifact 样例 |
| 保留与恢复 | 活动/终态任务、Artifact、消息去重事实、取消证据的实际保留及清理策略、恢复窗口与限制 |
| 测试回执 | EXT-01～14 的实际状态、命令、结果、运行版本；重启/断线/重复执行计数证据；未执行项和原因 |
| 隔离联调数据 | 实际可用的隔离数据、合法日期范围、完整/缺参输入、可安全取消的耗时场景；真实副作用及禁止操作；负责人确认前不执行 |
| 真实改造投入 | A2A 门面、持久任务、幂等、Cancel、补参、Artifact、测试和部署的实际工时记录；未记录则标缺项，不反推或猜测 |
| 新增依赖与增量成本 | 新依赖、端口/配置/部署改动，契约测试用例数、人工步骤；同类第二 Agent 需复用或另做的改造项 |

成本报告将区分 Runtime 一次性通用建设与本 Agent 适配，不将双方投入混为单 Agent 接入成本。Runtime 团队只负责本项目接入和验证，不进入对方仓库代写业务或服务端实现。对方交付不等于双方集成验收通过；最终以真实联调和负责人验收为准。

## 11. 契约依据与核对说明

本文已按兼容参照的真实 Client、Card 校验、Adapter、Fake 与 wire/Profile 测试核对：非阻塞 Send 配置、路径及 tenant 规则、Task 外包、枚举、稳定身份、文本补参、Artifact 拼接、精确 OUT_OF_SCOPE metadata、错误边界。正式需求对应 RA-001 第 9.6～9.8 节；架构第 17.3～17.11 节；决策 D-064～D-069。这些私有仓库引用用于双方内部追溯，实施要求已在本文完整展开。

协议定义可查 [A2A v1.0.1 的 a2a.proto](https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto)；REST 请求构造参照 [官方 SDK v1.2.3 RestTransport](https://github.com/a2aproject/a2a-python/blob/v1.2.3/src/a2a/client/transports/rest.py)，成功响应封装参照 [RestDispatcher](https://github.com/a2aproject/a2a-python/blob/v1.2.3/src/a2a/server/routes/rest_dispatcher.py)。错误 envelope 和 ErrorInfo 参照 [error_handlers.py](https://github.com/a2aproject/a2a-python/blob/v1.2.3/src/a2a/utils/error_handlers.py)，标准 reason/HTTP 映射参照 [errors.py](https://github.com/a2aproject/a2a-python/blob/v1.2.3/src/a2a/utils/errors.py)。本 Profile 另行强制幂等、可靠持久恢复和受控范围拒绝；不能仅凭遵循基础 A2A 推定这些保证已经具备。
