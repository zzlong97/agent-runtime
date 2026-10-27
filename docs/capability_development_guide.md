# AgentRuntime Capability 开发者接入手册

本文面向需要把第三方 Agent 接入 AgentRuntime 的开发者。目标是让开发者在不了解
Parent Graph、Run Coordinator、SSE Gateway 等内部实现的前提下，能够完成一个本地
Capability 的开发、注册、授权、测试和运行。

本文的本地能力流程基于 **Stage 3 静态 Capability Runtime**，Manifest 示例已迁移为
显式 `transport` 关闭联合，不再接受顶层 `entrypoint`。RA-DEV-01 目前只提供远程
发现和 Client 基础；远程 Run 执行尚未接通，不能将远程 Manifest 放入默认运行配置。
远程协议契约见文末“远程 A2A 基础接入”。热加载、管理 API 和多实例协调仍未提供。

## 1. 开始前先理解五个概念

一次用户消息从提交到产生最终回答时，各组件的职责如下：

```text
HTTP 202 Run
   ↓
Parent Graph：保存完整公共 messages，负责有限 OUT_OF_SCOPE 回流
   ↓
Router：只根据 capability_id / name / description / enabled 选择候选
   ↓
Runtime / Invocation Gateway：权限、健康、并发、超时、Task、Checkpoint、恢复
   ↓
Capability：判断自身边界，执行具体业务，返回 AgentResult
   ↓
Parent Graph：保存唯一最终公共 AIMessage
```

| 概念 | 谁创建和管理 | Capability 开发者需要做什么 |
| --- | --- | --- |
| Capability | Registry 在启动期按 Manifest 创建 | 实现固定生命周期和 `invoke()` 业务逻辑 |
| Parent | Runtime | 不修改；它是完整公共消息的唯一权威源 |
| Router | Runtime | 不修改；只需在 Manifest 中写清楚能力边界 |
| Run | Runtime | 不创建、不结束；通过 `RunContext` 读取稳定标识、取消和受控出口 |
| Invocation | Runtime | 每次 Capability 调用的稳定 UUID；不自行创建或持久化 |
| Task | Runtime | 表示 Session 内某个 Capability 的持续业务任务；Capability 只返回生命周期意图 |
| Child State | Capability + Runtime Checkpointer | 使用 Runtime 提供的 Checkpointer、`thread_id` 和配置保存私有状态 |

必须始终遵守以下边界：

- Capability 不直接读写 Parent State、公共历史、SSE、Redis、Session、顶层 Run 状态
  或 Runtime Repository。
- `TaskInput.messages` 是从 Parent 公共历史派生并深拷贝的本轮输入视图。修改这些对象
  不会修改 Parent，也不应把它们当成持久化权威源。
- 实时文本只能通过 `run_context.events.emit(AgentTextEvent(...))` 输出；最终规范全文
  必须通过 `AgentResult.content` 返回。
- Capability 私有 State 只能进入自己的 Child Checkpointer，不能放入
  `AgentResult.metadata`、公开事件或业务日志。
- `OUT_OF_SCOPE` 是合法业务拒绝，不是异常。拒绝之前不能输出文本、登记 Operation
  或写入 Child Checkpoint。

开发一个新 Capability 不需要修改 Parent Graph、Router、Invocation Gateway 或 SSE
代码。

## 2. 快速接入：实现 `text_statistics`

下面实现一个不同于仓库内 `general_chat` 和 `en_to_zh` 的最小能力：用户发送
`/stats <文本>` 时，返回该文本的字符数和按空白切分的词数。它不调用模型、不产生
外部副作用，适合作为接入模板。

### 2.1 创建目录

在项目根目录执行：

```powershell
New-Item -ItemType Directory -Force `
  src/agent_runtime/capabilities/text_statistics
New-Item -ItemType Directory -Force `
  tests/capabilities/text_statistics
New-Item -ItemType File -Force `
  src/agent_runtime/capabilities/text_statistics/__init__.py
```

最终目录如下：

```text
src/agent_runtime/capabilities/text_statistics/
├── __init__.py
├── entrypoint.py
└── manifest.yaml

tests/capabilities/text_statistics/
└── test_capability.py
```

`__init__.py` 可以为空。所有可运行代码先放在 `entrypoint.py`；复杂能力再按业务拆分
为 `agent.py`、`graph.py`、`state.py` 等文件。

### 2.2 实现同步工厂和 Capability

创建 `src/agent_runtime/capabilities/text_statistics/entrypoint.py`：

```python
"""文本统计 Capability 的 Stage 3 标准入口。"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    CapabilityBootstrapContext,
    CapabilityError,
    HealthResult,
)
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.agent_contract import (
    AgentContext,
    AgentTextEvent,
    RunContext,
    TaskInput,
)

CAPABILITY_ID = "text_statistics"
COMMAND_PREFIX = "/stats "
logger = logging.getLogger(__name__)


class TextStatisticsCapability:
    """统计显式 `/stats` 请求中的字符数和词数。"""

    async def initialize(self) -> None:
        """当前实现没有需要异步初始化的私有资源。"""

    async def health_check(self) -> HealthResult:
        """返回不含业务数据和凭据的稳定健康结果。"""

        return HealthResult(
            status="healthy",
            summary_code="TEXT_STATISTICS_READY",
        )

    async def invoke(
        self,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> AgentResult:
        """先判断能力边界，再输出一次类型化文本事件和最终全文。"""

        log_business_event(
            logger,
            "文本统计能力调用开始",
            run_id=run_context.run_id,
            session_id=agent_context.session_id,
            message_id=run_context.response_message_id,
            invocation_id=run_context.invocation_id,
            task_id=agent_context.task_id,
            capability_id=agent_context.capability_id,
        )
        latest_human = next(
            (
                message
                for message in reversed(task_input.messages)
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_human is None or not isinstance(latest_human.content, str):
            raise CapabilityError(
                code="CAPABILITY_EXECUTION_FAILED",
                message="文本统计输入中缺少字符串 HumanMessage",
                retryable=False,
            )

        content = latest_human.content
        if not content.startswith(COMMAND_PREFIX):
            log_business_event(
                logger,
                "文本统计能力范围拒绝",
                run_id=run_context.run_id,
                session_id=agent_context.session_id,
                invocation_id=run_context.invocation_id,
                capability_id=agent_context.capability_id,
                status="rejected",
            )
            return AgentResult(
                status="rejected",
                content="",
                metadata=AgentResultMetadata(
                    control_signal="OUT_OF_SCOPE",
                    task_transition="keep_active",
                ),
            )

        target = content[len(COMMAND_PREFIX) :].strip()
        if not target:
            raise CapabilityError(
                code="CAPABILITY_EXECUTION_FAILED",
                message="文本统计命令没有提供待统计文本",
                retryable=False,
            )

        await run_context.cancellation.raise_if_requested()
        answer = f"字符数：{len(target)}；词数：{len(target.split())}。"
        run_context.events.emit(AgentTextEvent(text=answer))
        log_business_event(
            logger,
            "文本统计能力调用完成",
            run_id=run_context.run_id,
            session_id=agent_context.session_id,
            message_id=run_context.response_message_id,
            invocation_id=run_context.invocation_id,
            task_id=agent_context.task_id,
            capability_id=agent_context.capability_id,
            status="completed",
        )
        return AgentResult(status="completed", content=answer)


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> TextStatisticsCapability:
    """按固定同步工厂契约创建文本统计 Capability。"""

    if bootstrap_context.manifest.capability_id != CAPABILITY_ID:
        raise ValueError("文本统计入口只能绑定 text_statistics Manifest")
    return TextStatisticsCapability()
```

这里有四个不能改变的签名要求：

1. Manifest 必须指向名为 `create_capability` 的同步函数。
2. 工厂必须只有一个无默认值参数，参数名必须是 `bootstrap_context`。
3. `initialize()`、`health_check()`、`invoke()` 必须是 `async def`。
4. `invoke()` 的三个位置参数必须依次命名为 `run_context`、`agent_context`、
   `task_input`，不能改为仅关键字参数，也不能增加额外参数。

工厂不能执行异步初始化。需要网络连接或异步客户端时，在同步工厂中构造实例，在
`initialize()` 中启动资源，并通过 `bootstrap_context.register_cleanup()` 注册无参数
异步清理函数。

### 2.3 编写 Manifest

创建 `src/agent_runtime/capabilities/text_statistics/manifest.yaml`：

```yaml
manifest_schema_version: 1
capability_id: text_statistics
name: 文本统计
description: 统计以 /stats 开头的文本字符数和按空白切分的词数，不处理其他请求。
enabled: true
transport:
  type: local
  entrypoint: agent_runtime.capabilities.text_statistics.entrypoint:create_capability
version: 1.0.0
state_scope: invocation
state_schema_version: "1"
compatible_state_schema_versions: []
allow_degraded: false
concurrency:
  mode: unlimited
  max_concurrency: null
  acquire_timeout_seconds: 1.0
execution:
  timeout_seconds: 10.0
  cancel_grace_seconds: 1.0
recovery_policy: automatic
side_effect_policy: none
```

Manifest 是关闭 Schema：顶层和嵌套对象中的未知字段都会导致当前来源被隔离。不要把
API Key、Prompt、工具参数或任意业务配置追加到 YAML 中。

### 2.4 添加最小单元测试

创建 `tests/capabilities/text_statistics/test_capability.py`：

```python
"""文本统计 Capability 的接入契约测试。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import yaml
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.checkpoint.memory import MemorySaver

from agent_runtime.capabilities.contracts import CapabilityBootstrapContext
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.text_statistics.entrypoint import create_capability
from agent_runtime.graph.context import build_refreshed_child_input
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    RunContext,
    TaskInput,
)


def test_text_statistics_contract() -> None:
    """工厂、生命周期、类型化输出和范围拒绝均符合 Stage 3 契约。"""

    async def scenario() -> None:
        manifest_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "agent_runtime"
            / "capabilities"
            / "text_statistics"
            / "manifest.yaml"
        )
        manifest = CapabilityManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        capability = create_capability(
            CapabilityBootstrapContext(
                manifest=manifest,
                child_checkpointer=MemorySaver(),
                model_factory=lambda: object(),
                capability_config={},
                register_cleanup=lambda _cleanup: None,
            )
        )
        assert await capability.initialize() is None
        assert (await capability.health_check()).status == "healthy"

        emitted: list[str] = []

        async def never_cancelled() -> bool:
            return False

        session_id = uuid4()
        invocation_id = uuid4()
        run_context = RunContext(
            run_id=uuid4(),
            request_id=uuid4(),
            input_message_id=uuid4(),
            response_message_id=uuid4(),
            invocation_id=invocation_id,
            cancellation=AgentCancellation(never_cancelled),
            events=AgentEventOutlet(lambda event: emitted.append(event.text)),
        )
        agent_context = AgentContext(
            session_id=session_id,
            capability_id="text_statistics",
            thread_id=f"test:{session_id}:{invocation_id}",
            config={
                "configurable": {
                    "thread_id": f"test:{session_id}:{invocation_id}",
                    "checkpoint_ns": "",
                }
            },
            task_id=uuid4(),
            state_scope="invocation",
            state_schema_version="1",
        )

        formal_messages = tuple(
            build_refreshed_child_input(
                [HumanMessage(content="/stats Agent Runtime")]
            )
        )
        assert isinstance(formal_messages[0], RemoveMessage)
        completed = await capability.invoke(
            run_context,
            agent_context,
            TaskInput(messages=formal_messages),
        )
        assert completed.status == "completed"
        assert completed.content == "字符数：13；词数：2。"
        assert "".join(emitted) == completed.content

        emitted.clear()
        rejected = await capability.invoke(
            run_context,
            agent_context,
            TaskInput(messages=(HumanMessage(content="介绍 AgentRuntime"),)),
        )
        assert rejected.status == "rejected"
        assert rejected.metadata.control_signal == "OUT_OF_SCOPE"
        assert emitted == []

    asyncio.run(scenario())
```

运行该测试：

```powershell
uv run --locked pytest -q `
  tests/capabilities/text_statistics/test_capability.py
```

### 2.5 在启动前检查 Manifest

下面的命令使用 Runtime 自身 Schema 检查文件，而不是另写一套规则：

```powershell
@'
from pathlib import Path
import yaml
from agent_runtime.capabilities.manifest import CapabilityManifest

path = Path("src/agent_runtime/capabilities/text_statistics/manifest.yaml")
raw = yaml.safe_load(path.read_text(encoding="utf-8"))
manifest = CapabilityManifest.model_validate(raw)
print(manifest.model_dump_json(indent=2))
'@ | uv run --locked python -
```

命令退出码为 0 且输出 `"capability_id": "text_statistics"` 的 JSON，说明 Schema 校验
通过；这还不代表 entrypoint、初始化和健康检查已经通过，后续必须实际启动 Runtime。

### 2.6 准备配置并启动依赖

如果项目根目录还没有 `.env`，先从字段清单创建文件；已有 `.env` 时不要覆盖：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，至少确认下列字段。示例数据库用户名、密码和库名与 `compose.yaml` 的
默认值一致；模型标识必须替换为当前百炼账号已经开通的真实模型。不要提交真实密钥：

```dotenv
DATABASE_URL=postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime
REDIS_URL=redis://localhost:6379/0
DASHSCOPE_API_KEY=<百炼 API Key>
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=<已开通的百炼模型标识>
LOCAL_USER_ID=capability-guide-user
RUNTIME_DEMO_MODE=false
```

`Settings` 使用的准确环境变量名就是上面的全大写名称。进程环境变量优先于 `.env`；
因此若使用 `$env:LOCAL_USER_ID` 临时覆盖，授权脚本和 Runtime 必须在同一终端运行，
或在两个终端都设置完全相同的值。最简单、最不易出错的方式是先把专用用户写入
`.env`，再执行后续所有命令。

确认 Windows Docker Desktop 已由负责人启动。若 Docker Desktop 未运行或异常，只
报告具体错误并通知负责人恢复，不要重置或修复 Docker Desktop 内部文件。然后启动
项目声明的 PostgreSQL 和 Redis：

```powershell
docker compose up -d postgres redis
docker compose ps
```

只有两个服务都显示 `healthy` 后，才能执行授权脚本和真实 Runtime 验证。

### 2.7 显式授权固定本地用户

Stage 3 权限规则是“无记录即拒绝”。仓库当前没有 Capability Admin API，因此需要通过
已实现的 Repository 写入显式权限。以下 PowerShell 脚本读取同一个 `.env` 和
`LOCAL_USER_ID`，在 Windows 上使用 psycopg 兼容事件循环：

```powershell
@'
import asyncio
from datetime import UTC, datetime

from agent_runtime.capabilities.persistence import (
    UserCapabilityPermissionRepository,
)
from agent_runtime.capabilities.persistence_models import (
    UserCapabilityPermission,
)
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory


async def main() -> None:
    settings = Settings()
    repository = UserCapabilityPermissionRepository(settings)
    await repository.setup()
    now = datetime.now(UTC)
    permission = await repository.set_allowed(
        UserCapabilityPermission(
            user_id=settings.local_user_id,
            capability_id="text_statistics",
            allowed=True,
            created_at=now,
            updated_at=now,
        )
    )
    print(permission.user_id, permission.capability_id, permission.allowed)


with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
    runner.run(main())
'@ | uv run --env-file .env python -
```

授权结果应以 `True` 结束。`allowed=false` 是显式拒绝；调用 Repository 的
`revoke(user_id=..., capability_id=...)` 会删除权限行，但无记录仍然是拒绝。

权限读取不缓存，授权变化会在 Router 前和 Invocation 前实时生效，不要求重启。
Manifest、entrypoint、实现代码、`enabled` 或运行策略发生变化时必须重启 Runtime，
因为 Registry 只在进程启动时构建一次静态快照。

### 2.8 启动 Runtime

授权成功后，在读取同一 `.env` 的终端启动 Runtime：

```powershell
uv run --env-file .env python -m agent_runtime
```

如果上一节使用进程环境变量覆盖了 `LOCAL_USER_ID` 或 `RUNTIME_DEMO_MODE`，启动
Runtime 的终端也必须设置相同值。正式 Capability 链路必须保持
`RUNTIME_DEMO_MODE=false`，因为演示模式会绕过正式 Router 和 Registry。

启动日志应依次出现 Manifest 扫描、Registry 构建、Capability 初始化和启动健康强检。
修改 Capability 后要先停止进程，再重新执行启动命令。

### 2.9 通过真实 HTTP、SSE 和公共历史验证

在另一个 PowerShell 窗口提交消息：

```powershell
$requestId = [guid]::NewGuid().ToString()
$body = @{
  request_id = $requestId
  message = @{ content = "/stats Agent Runtime" }
} | ConvertTo-Json
$run = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
$run
```

成功时 HTTP 状态为 202，响应中包含 `run_id`、`session_id`、
`response_message_id` 和当前 `status`。使用返回的 Run ID 读取公开事件：

```powershell
curl.exe -N `
  "http://127.0.0.1:8000/api/v1/chat/runs/$($run.run_id)/events?after_seq=0"
```

最终应看到带 `capability_id=text_statistics` 的 `message.delta`、
`message.finalized` 和 `run.completed`。再读取 Parent 公共历史：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8000/api/v1/chat/sessions/$($run.session_id)/messages"
```

最后一条 assistant 消息应为 `字符数：13；词数：2。`，其
`runtime_status=completed`、`capability_id=text_statistics`。也可以直接访问
`http://127.0.0.1:8000/chat` 完成同样验证。

## 3. Manifest 完整字段说明

Stage 3 Manifest 的顶层字段全部必填，没有隐式默认值。字符串会按 Schema 规则去除
首尾空白；控制字段使用严格类型，不会把字符串 `"true"`、`"1"` 或 `"30.0"`
自动转换成布尔值或数字。

### 3.1 顶层字段

| 字段 | 类型与约束 | 作用与典型值 |
| --- | --- | --- |
| `manifest_schema_version` | 严格整数，当前只能是 `1` | Manifest 契约版本；不能写成字符串 |
| `capability_id` | 字符串，正则 `^[a-z][a-z0-9_]{0,63}$` | 稳定公开 ID，如 `text_statistics`；会进入 Router、消息和事件 |
| `name` | 非空字符串，最长 80 | 人类可读名称，只用于候选描述和诊断 |
| `description` | 非空字符串，最长 500 | Router 能力边界；应明确“处理什么、不处理什么” |
| `enabled` | 严格布尔值 | `true` 才实例化；`false` 被发现但不进入 active Snapshot |
| `transport` | `local` / `remote_a2a` 判别关闭对象 | 本地填写 `type: local` 和 `entrypoint: package.module:create_capability`；远程契约见文末 |
| `version` | 合法 SemVer | 当前实现版本，如 `1.0.0`、`1.1.0-beta.1` |
| `state_scope` | `invocation`、`run`、`session` | Child 私有 State 的共享范围，必须显式选择 |
| `state_schema_version` | 1～64 字符，字母数字开头，仅字母、数字、`.`、`_`、`-` | 新 Task 固化的 State Schema 版本，如 `1` 或 `v2` |
| `compatible_state_schema_versions` | 唯一字符串数组 | 当前实现仍能读取的旧 Task 版本；无兼容版本写 `[]` |
| `allow_degraded` | 严格布尔值 | `health_check()` 返回 degraded 时是否仍可服务 |
| `concurrency` | 关闭对象 | 当前进程内并发准入策略 |
| `execution` | 关闭对象 | 单次 Invocation 执行和取消时限 |
| `recovery_policy` | `automatic` 或 `manual` | 崩溃后是否允许 Runtime 自动恢复调用 |
| `side_effect_policy` | `none`、`idempotent`、`unsafe` | 外部副作用策略；决定恢复和 Regenerate 安全边界 |

`capability_id` 在一个启动快照中必须唯一。同一 ID 的所有来源都会被隔离，Registry
不会挑选其中一个。Stage 3 也不允许同一 ID 的多版本实现并存。

### 3.2 `concurrency`

| 字段 | 类型与约束 | 行为 |
| --- | --- | --- |
| `mode` | `unlimited` 或 `bounded` | 单进程内不设槽位，或使用固定容量 Semaphore |
| `max_concurrency` | `null` 或大于等于 1 的严格整数 | `unlimited` 必须为 `null`；`bounded` 必须为正整数 |
| `acquire_timeout_seconds` | 有限正浮点数 | 等待槽位超时后返回 `CAPABILITY_BUSY`；不会提前创建 Task |

示例：最多同时执行 4 个 Invocation，等待 2 秒：

```yaml
concurrency:
  mode: bounded
  max_concurrency: 4
  acquire_timeout_seconds: 2.0
```

该限制只对当前 Runtime 进程有效，不是分布式限流。

### 3.3 `execution`

| 字段 | 类型与约束 | 行为 |
| --- | --- | --- |
| `timeout_seconds` | 有限正浮点数 | 整个 `invoke()` 的最长执行时间 |
| `cancel_grace_seconds` | 有限非负浮点数 | 超时或取消后等待协作退出的宽限期 |

超时后 Runtime 先让 `run_context.cancellation` 返回 true；宽限期后仍未退出，才取消
本进程内的 asyncio Task。Capability 必须在模型调用、工具调用和循环边界检查取消。

### 3.4 恢复与副作用组合

| `recovery_policy` | `side_effect_policy` | 是否合法 | 开发者责任 |
| --- | --- | --- | --- |
| automatic | none | 是 | 不产生外部副作用，可按 Checkpoint 自动恢复 |
| automatic | idempotent | 是 | 每个外部副作用必须使用 `ctx.operation()` 和供应方幂等键 |
| automatic | unsafe | 否 | Manifest 校验直接拒绝 |
| manual | none | 是 | 崩溃恢复时 Runtime 安全失败，不重新调用 Capability |
| manual | idempotent | 是 | 同上；当前没有人工恢复 API |
| manual | unsafe | 是 | 正常执行允许，崩溃后不会自动重放 |

Regenerate 的限制更严格：只有 `state_scope=invocation` 且
`side_effect_policy=none` 的 Capability 可以在创建新 Run 前通过门禁。其他组合返回
HTTP 409 `CAPABILITY_REGENERATE_UNSUPPORTED`。

## 4. 标准接口和上下文

### 4.1 `create_capability()`

Registry 动态导入 `transport.entrypoint` 后会检查本地工厂：

```python
def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> MyCapability:
    return MyCapability(...)
```

工厂必须同步返回实例，不能返回 coroutine。创建失败、签名不符或实例协议不符时，
只有当前来源会被标记为 `entrypoint_failed`；其他 Capability 继续加载。

### 4.2 `CapabilityBootstrapContext`

| 字段 | 当前实现提供的内容 | 正确用法 |
| --- | --- | --- |
| `manifest` | 已严格校验且冻结的 `CapabilityManifest` | 校验工厂绑定 ID，读取静态策略；不要修改 |
| `child_checkpointer` | 当前 Capability 独立的 PostgreSQL Saver 句柄 | 编译 LangGraph/create_agent；不要读取其他能力的线程 |
| `model_factory` | 当前 Runtime 的公共模型同步工厂 | 先检查可调用，再调用；内置能力还校验返回 `BaseChatModel` |
| `capability_config` | 深度只读映射 | 当前正式 `open_chat_service()` 未注入配置，因此实际为空映射 |
| `register_cleanup` | 注册无参数异步清理函数 | 资源创建后立即注册；Registry 关机时按逆序幂等尝试清理 |

当前版本没有 Capability 自定义配置中心，也不允许在 Manifest 中追加配置字段。需要
私有 API Key 或能力级配置的集成在当前基线没有正式注入入口；不要把密钥写入
Manifest、Prompt、异常详情或日志，应先向项目负责人提出受控配置接入需求。

异步资源的推荐结构：

```python
class MyCapability:
    def __init__(self, bootstrap_context: CapabilityBootstrapContext) -> None:
        self._bootstrap_context = bootstrap_context
        self._client = None

    async def initialize(self) -> None:
        self._client = await build_async_client()

        async def cleanup() -> None:
            if self._client is not None:
                await self._client.aclose()

        self._bootstrap_context.register_cleanup(cleanup)
```

如果资源在工厂中同步创建，应在工厂返回之前注册清理，以便后续初始化失败时也能释放。
清理函数必须无参数并返回 Awaitable。

### 4.3 `initialize()`

```python
async def initialize(self) -> None:
    ...
```

- 只返回 `None`。
- 用于建立异步客户端、加载本地索引或准备业务资源。
- 失败时当前 Capability 被标记为 `initialize_failed`，其清理函数仍会执行。
- 不要在这里修改 Runtime、自动授权用户或启动后台热加载器。

### 4.4 `health_check()`

```python
async def health_check(self) -> HealthResult:
    return HealthResult(status="healthy", summary_code="MY_CAPABILITY_READY")
```

`status` 只能是 `healthy`、`degraded`、`unhealthy`；`summary_code` 必须匹配
`^[A-Z][A-Z0-9_]{0,127}$`，且不能含异常正文、业务正文或凭据。

Runtime 在启动时顺序强检一次，之后按全局 TTL 在请求路径中单飞刷新。检查异常会被
收敛为 `unhealthy / HEALTH_CHECK_FAILED`，只影响当前 Capability。`degraded` 是否
可服务由 Manifest 的 `allow_degraded` 决定。健康详情不会进入 Router 或公开 SSE。

### 4.5 `invoke()`

```python
async def invoke(
    self,
    run_context: RunContext,
    agent_context: AgentContext,
    task_input: TaskInput,
) -> AgentResult:
    ...
```

`invoke()` 是唯一正式业务入口。Runtime 在进入前已经完成 Registry、健康和权限复查，
创建 Invocation，获取并发槽，并解析 Task/State Scope；Capability 不应重复这些工作。

### 4.6 `RunContext`

| 字段或方法 | 含义 |
| --- | --- |
| `run_id` | 当前持久 Run UUID；正式链路非空 |
| `request_id` | 客户端全局幂等请求 UUID；部分兼容场景可空 |
| `input_message_id` | 当前 HumanMessage 的稳定 UUID；部分兼容场景可空 |
| `response_message_id` | 预分配最终 AIMessage UUID |
| `invocation_id` | 当前 Invocation UUID；正式 Stage 3 链路非空 |
| `cancellation.is_requested()` | 只读检查取消请求 |
| `cancellation.raise_if_requested()` | 已取消时抛出 `asyncio.CancelledError` |
| `events.emit(AgentTextEvent(...))` | 发出唯一允许的用户可见实时文本事件 |
| `idempotency_key(operation_key)` | 生成当前 Run + Invocation 下的稳定副作用幂等键 |
| `operation(operation_key)` | 进入 Runtime 管理的 Operation Ledger 上下文 |

不要捕获并改写 `asyncio.CancelledError`。Capability 超时由 Runtime 映射为
`CAPABILITY_TIMEOUT`，显式 Cancel 保持取消语义。

### 4.7 `AgentContext`

| 字段 | 含义与限制 |
| --- | --- |
| `session_id` | 当前 Session UUID |
| `capability_id` | 当前 Manifest 的动态 ID |
| `thread_id` | Runtime 已按 scope、Task 和 Schema 版本生成的 Child 线程 ID |
| `config` | 包含 `thread_id` 和 `checkpoint_ns` 的 `RunnableConfig` |
| `task_id` | Runtime 解析出的 Capability Task UUID；正式链路非空 |
| `state_scope` | 当前 Manifest 的 `invocation`、`run` 或 `session` |
| `state_schema_version` | 当前 Task 创建时固化、且已验证兼容的版本 |

将 `agent_context.config` 原样传给 LangGraph、`create_agent` 或模型 Runnable。不要自行
拼接 `thread_id`，不要替换 `checkpoint_ns`，也不要把这些值发送给客户端。

### 4.8 `TaskInput`

`TaskInput.messages` 是 `tuple[BaseMessage, ...]`。正式 Invocation Gateway 传入的元组
**第一个元素是** `RemoveMessage(id=REMOVE_ALL_MESSAGES)`，随后才是从 Parent 权威
历史派生并深拷贝的本轮消息视图：

- 当前 HumanMessage 之前少于 10 个已完成轮次时，包含全部已完成轮次；
- 已有至少 10 个已完成轮次时，只包含最近 5 个完整轮次；
- 再追加当前 HumanMessage；
- System Message 始终保留且不计轮次；
- unsupported、incomplete、stopped 不进入后续模型上下文。

这个首元素是 LangGraph `add_messages` reducer 的控制消息，用于先清空同一 Child
thread 中的旧消息，再写入本轮完整派生视图。处理方式取决于 Agent 类型：

- 使用持久化 `create_agent` 或包含 `add_messages` reducer 的 LangGraph 时，像仓库内
  两个标准 Capability 一样，把**完整列表（含 `RemoveMessage`）**传入图，不能先过滤；
- 普通 Python Agent 或不理解 LangGraph 控制消息的外部 SDK 必须先过滤控制消息，
  只把业务消息传给 SDK：

```python
from langchain_core.messages import RemoveMessage

business_messages = tuple(
    message
    for message in task_input.messages
    if not isinstance(message, RemoveMessage)
)
```

不要把 `RemoveMessage` 序列化到模型 Prompt、HTTP 请求或业务日志。快速教程通过查找
最新 `HumanMessage` 自然忽略它；教程测试直接调用
`build_refreshed_child_input()`，可确定性验证正式 Gateway 的输入形态。Capability
可以把元组转换为新列表，但不应在其中保存私有业务 State。

### 4.9 `AgentResult`

以下代码片段是放在 `invoke()` 方法内的返回或抛错语句，不是独立模块。成功结果：

```python
return AgentResult(
    status="completed",
    content="面向用户的非空最终全文",
    metadata=AgentResultMetadata(task_transition="keep_active"),
)
```

范围拒绝：

```python
return AgentResult(
    status="rejected",
    content="",
    metadata=AgentResultMetadata(
        control_signal="OUT_OF_SCOPE",
        task_transition="keep_active",
    ),
)
```

`AgentResultMetadata` 只允许：

- `control_signal`: `OUT_OF_SCOPE` 或 `None`；
- `task_transition`: `keep_active`、`completed`、`failed`、`cancelled`。

不要在 metadata 中放任意 JSON、工具结果、私有 State 或用户正文。Runtime 会把
`completed` 的 `content` 构造成唯一最终公共 AIMessage；Capability 不直接写 Parent。

失败应抛出 `CapabilityError`：

```python
raise CapabilityError(
    code="CAPABILITY_EXECUTION_FAILED",
    message="内部可诊断但不含凭据和业务正文的说明",
    retryable=False,
    details={"provider": "example"},
)
```

`code` 必须是大写字母开头的大写下划线标识。`details` 根节点必须是受控 JSON 对象，
构造后不可变，且不会进入公开事件。当前 Runtime 只公开映射固定错误白名单；未知或
自定义错误码会安全收敛为 `CAPABILITY_EXECUTION_FAILED`，所以不要假定自定义 code
会原样暴露给客户端。

## 5. 三种 Agent 封装方式

### 5.1 普通 Python Agent

快速教程中的 `text_statistics` 就是完整普通 Python 实现。适用于确定性计算、调用
现有 SDK 或轻量工具编排。关键规则是：先判断范围，再执行；检查取消；实时输出走
`AgentTextEvent`；最终全文走 `AgentResult`。

### 5.2 LangChain `create_agent`

下面是一个可直接放入独立 `entrypoint.py` 的完整模板。它用 `/ask ` 作为确定性
范围边界；正式能力可以换成模型或业务规则判断，但判断仍必须发生在调用 Agent 之前。
传给 `create_agent` 的消息列表必须包含 `TaskInput` 开头的 `RemoveMessage`，让其
`add_messages` reducer 清除同一 Child thread 的旧消息：

```python
"""使用 LangChain create_agent 封装 Capability 的完整模板。"""

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    CapabilityBootstrapContext,
    CapabilityError,
    HealthResult,
)
from agent_runtime.runtime.agent_contract import (
    AgentContext,
    AgentTextEvent,
    RunContext,
    TaskInput,
)


class QuestionCapability:
    """处理显式 `/ask` 请求的最小 create_agent Capability。"""

    def __init__(self, agent) -> None:
        self._agent = agent

    async def initialize(self) -> None:
        """当前模板没有额外异步资源。"""

    async def health_check(self) -> HealthResult:
        """报告实例已经完成本地构造。"""

        return HealthResult(status="healthy", summary_code="QUESTION_READY")

    async def invoke(
        self,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> AgentResult:
        """先守卫范围，再把完整刷新消息交给持久化 Agent。"""

        messages = list(task_input.messages)
        latest_human = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_human is None or not isinstance(latest_human.content, str):
            raise CapabilityError(
                code="CAPABILITY_EXECUTION_FAILED",
                message="问答输入中缺少字符串 HumanMessage",
                retryable=False,
            )
        if not latest_human.content.startswith("/ask "):
            return AgentResult(
                status="rejected",
                content="",
                metadata=AgentResultMetadata(
                    control_signal="OUT_OF_SCOPE",
                    task_transition="keep_active",
                ),
            )

        content_parts: list[str] = []
        async for event in self._agent.astream(
            {"messages": messages},
            agent_context.config,
            stream_mode="messages",
        ):
            await run_context.cancellation.raise_if_requested()
            message, _metadata = event
            text = str(message.text)
            if text:
                content_parts.append(text)
                run_context.events.emit(AgentTextEvent(text=text))

        return AgentResult(
            status="completed",
            content="".join(content_parts),
        )


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> QuestionCapability:
    """用 Runtime 提供的模型和独立 Checkpointer 构造 Agent。"""

    model_factory = bootstrap_context.model_factory
    if not callable(model_factory):
        raise TypeError("model_factory 必须可调用")
    model = model_factory()
    if not isinstance(model, BaseChatModel):
        raise TypeError("model_factory 必须返回 BaseChatModel")
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt="这里填写该 Capability 的中文边界和任务提示词。",
        checkpointer=bootstrap_context.child_checkpointer,
        name=bootstrap_context.manifest.capability_id,
    )
    return QuestionCapability(agent)
```

范围判断必须发生在 `astream()` 之前。仓库中的
`src/agent_runtime/capabilities/general_chat/agent.py` 是当前可运行参考。

### 5.3 LangGraph `StateGraph`

下面是对应的完整 `entrypoint.py` 模板。把 Runtime Checkpointer 交给图编译，并始终
用 `agent_context.config` 执行。`messages` 使用 `add_messages` reducer，因此也必须传入
包含 `RemoveMessage` 的完整刷新列表：

```python
"""使用 LangGraph StateGraph 封装 Capability 的完整模板。"""

from typing import Annotated, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    CapabilityBootstrapContext,
    CapabilityError,
    HealthResult,
)
from agent_runtime.runtime.agent_contract import (
    AgentContext,
    AgentTextEvent,
    RunContext,
    TaskInput,
)


class MyPrivateState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    private_counter: int


def build_graph(
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,
):
    """构造使用 Runtime 独立 Checkpointer 的私有图。"""

    async def answer(
        state: MyPrivateState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        response = await model.ainvoke(state["messages"], config)
        return {
            "messages": [response],
            "private_counter": state.get("private_counter", 0) + 1,
        }

    builder = StateGraph(MyPrivateState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(checkpointer=checkpointer, name="graph_question")


class GraphQuestionCapability:
    """处理显式 `/graph` 请求的最小 StateGraph Capability。"""

    def __init__(self, graph) -> None:
        self._graph = graph

    async def initialize(self) -> None:
        """当前模板没有额外异步资源。"""

    async def health_check(self) -> HealthResult:
        """报告图已经完成本地编译。"""

        return HealthResult(status="healthy", summary_code="GRAPH_READY")

    async def invoke(
        self,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> AgentResult:
        """先守卫范围，再执行私有图。"""

        messages = list(task_input.messages)
        latest_human = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_human is None or not isinstance(latest_human.content, str):
            raise CapabilityError(
                code="CAPABILITY_EXECUTION_FAILED",
                message="图输入中缺少字符串 HumanMessage",
                retryable=False,
            )
        if not latest_human.content.startswith("/graph "):
            return AgentResult(
                status="rejected",
                content="",
                metadata=AgentResultMetadata(
                    control_signal="OUT_OF_SCOPE",
                    task_transition="keep_active",
                ),
            )

        content_parts: list[str] = []
        async for event in self._graph.astream(
            {"messages": messages, "private_counter": 0},
            agent_context.config,
            stream_mode="messages",
        ):
            await run_context.cancellation.raise_if_requested()
            message, _metadata = event
            text = str(message.text)
            if text:
                content_parts.append(text)
                run_context.events.emit(AgentTextEvent(text=text))

        return AgentResult(
            status="completed",
            content="".join(content_parts),
        )


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> GraphQuestionCapability:
    """用 Runtime 模型和 Checkpointer 构造私有图。"""

    model_factory = bootstrap_context.model_factory
    if not callable(model_factory):
        raise TypeError("model_factory 必须可调用")
    model = model_factory()
    if not isinstance(model, BaseChatModel):
        raise TypeError("model_factory 必须返回 BaseChatModel")
    graph = build_graph(
        model=model,
        checkpointer=bootstrap_context.child_checkpointer,
    )
    return GraphQuestionCapability(graph)
```

仓库中的 `src/agent_runtime/capabilities/en_to_zh/graph.py` 是当前可运行参考。对于
`run` 或 `session` scope，不要每次无条件把完整派生消息追加到已有私有消息列表，避免
重复；应明确设计图输入与持久 State 的合并语义。Parent 公共消息仍不能成为 Child
私有 State 的替代品。

## 6. 注册、授权和重启规则

### 6.1 Manifest 扫描

正式应用固定扫描：

```text
src/agent_runtime/capabilities/**/manifest.yaml
```

只有文件名精确为 `manifest.yaml` 的文件会被递归发现。Source 按规范化相对路径稳定
排序；单个 YAML 语法、读取或结构错误会转为该来源的受控错误记录，不中止其他文件。

Stage 3 不是通用 Python 插件系统。放在任意外部目录或仅安装到 site-packages 的
Manifest 不会自动被扫描；当前接入方式是把 Capability 包和 Manifest 纳入上述项目
包目录，并确保 `entrypoint` 可由当前 Python 环境导入。

### 6.2 Registry 启动过程

每次进程启动只执行一次：

1. 扫描全部 Manifest；
2. 严格校验并整组隔离重复 ID；
3. 忽略 `enabled=false` 来源；
4. 导入同步工厂并验证协议；
5. 为每个启用 Manifest 分配独立 Child Checkpointer；
6. 调用 `initialize()`；
7. 对所有 active Capability 执行启动健康强检；
8. 生成不可热变更的 Registry Snapshot。

因此新增、删除或修改 Manifest/代码后必须重启。当前没有文件监听、动态 mount、
unmount 或 DRAINING。

### 6.3 权限规则

权限键是 `user_id + capability_id`：

- 没有记录：拒绝；
- `allowed=false`：拒绝；
- `allowed=true`：允许。

Router 前会读取一次，用于构造候选；Invocation 前会再次实时读取，防止选择后权限被
撤销。两个仓库内标准 Capability 也不会自动授权。当前没有 RBAC、ABAC、组织、租户、
用户切换或权限管理 API。

## 7. 正确使用 Runtime 能力

### 7.1 流式输出

只允许：

```python
run_context.events.emit(AgentTextEvent(text="非空文本片段"))
```

不要发送 `AIMessage`、字典、LangGraph 原始 event、StateSnapshot、工具参数或工具原始
结果。不要为每个 token 写业务日志。推荐让所有事件文本拼接后与
`AgentResult.content` 一致；最终 Parent 历史以 `AgentResult.content` 为准。

### 7.2 OUT_OF_SCOPE

能力边界判断必须是第一项业务动作。合法拒绝必须同时满足：

- 没有调用 `events.emit()`；
- 没有登记 Operation；
- 没有推进 Child Checkpoint；
- 返回 `status="rejected"`、空 `content`、`OUT_OF_SCOPE`、`keep_active`。

如果新建 provisional Task 后合法拒绝，Runtime 会回滚它并恢复原 current Task。若
拒绝前已经产生输出、Operation 或 Checkpoint，Runtime 会把它视为执行契约违规，而
不是正常回流。

### 7.3 Task 生命周期

Router 会给出 `task_action=continue|new`，但 Task 的创建、锁定、current 切换和
持久化全部由 Runtime 完成。`continue` 找不到 current Task 时会原子降级为 `new`。

Capability 只能通过 `AgentResultMetadata.task_transition` 请求：

- `keep_active`：默认，后续 Run 可继续当前 Task；
- `completed`：任务业务目标已经完成；
- `failed`：任务业务上确定失败；
- `cancelled`：任务业务上取消。

顶层 Run 失败或取消不会自动结束 Task。不要直接操作 Task Repository。

### 7.4 State Scope 和 Checkpoint

| Scope | Runtime 使用的共享键 | 适合场景 |
| --- | --- | --- |
| `invocation` | `invocation_id` | 每次调用完全隔离；无状态或 Regenerate 能力首选 |
| `run` | `run_id` | 同一 Run 内的调用共享私有 State |
| `session` | current `task_id` | 同一 Session 中同一持续 Task 跨 Run 共享私有 State |

虽然名称是 `session`，实际线程键包含 Task ID；`task_action=new` 会创建新 Task，从而
获得新的私有线程。Runtime 生成的线程还包含 Session、Capability ID 和 Task 固化的
State Schema 版本。

升级私有 State Schema 时：

1. 修改 `state_schema_version`；
2. 只有确认新代码能读取旧 State 时，才把旧版本加入
   `compatible_state_schema_versions`；
3. Runtime 会继续用旧 Task 固化的版本 namespace；
4. 不兼容的 `continue` 在调用前返回 `CAPABILITY_STATE_VERSION_INCOMPATIBLE`；
5. Stage 3 不做自动迁移，也不修改旧 Task。

### 7.5 取消和超时

在以下位置调用 `raise_if_requested()`：

- 模型或图调用前后；
- 每次工具或外部服务调用前；
- 长循环的每轮；
- 流式事件处理循环中。

不要屏蔽取消。若底层 SDK 支持取消，应在观察到取消后协作结束；否则 Runtime 会在
Manifest 声明的宽限期后取消本地 asyncio Task。取消不会回滚已经发生的外部副作用。

### 7.6 恢复

Runtime 遵循 Checkpoint-first：已落盘最终消息或 Interrupt 时只补数据库投影，不重复
执行 Graph。没有最终 Checkpoint 时：

- `automatic + none`：从精确 Child Checkpoint 或稳定输入恢复；
- `automatic + idempotent`：复用同一 Invocation、Task、Operation 和幂等键；
- `manual + *`：不调用 Capability，形成非空 incomplete 消息并使 Run 失败；
- 最多三次恢复接管；不会创建新的 retry Run。

Capability 必须把需要恢复的业务 State 写入自己的 Checkpointer，不能依赖进程内全局
变量。Stage 3 的恢复是至少一次，不是恰好一次。

### 7.7 Operation Ledger

仅 `side_effect_policy=idempotent` 的自动恢复能力需要对每个外部副作用使用受控上下文：

```python
"""Capability 内使用 Operation Ledger 的完整辅助类示例。"""

from typing import Protocol

from agent_runtime.capabilities.contracts import CapabilityError
from agent_runtime.runtime.agent_contract import RunContext


class ReportProvider(Protocol):
    """支持供应方幂等键的报告服务最小接口。"""

    async def send_report(self, *, idempotency_key: str) -> None:
        """用稳定幂等键发送一次报告。"""


class ProviderRejected(Exception):
    """供应方已经明确判定请求不会成功。"""


class ReportSender:
    """可作为 Capability 私有成员使用的副作用适配器。"""

    def __init__(self, provider: ReportProvider) -> None:
        self._provider = provider

    async def send_once(self, run_context: RunContext) -> None:
        """在 Runtime Ledger 保护下执行一次幂等报告发送。"""

        async with run_context.operation("send-report:v1") as operation:
            if not operation.should_execute:
                return
            try:
                await self._provider.send_report(
                    idempotency_key=operation.idempotency_key,
                )
            except ProviderRejected as error:
                await operation.mark_failed()
                raise CapabilityError(
                    code="CAPABILITY_EXECUTION_FAILED",
                    message="外部服务明确拒绝发送报告",
                    retryable=False,
                ) from error
```

在 Capability 的 `invoke()` 中调用 `await self._report_sender.send_once(run_context)`。
`ProviderRejected` 只能代表供应方已经给出确定失败；网络断开、超时或未知结果不能转换
为它，也不能调用 `mark_failed()`。

规则如下：

- `operation_key` 由 Capability 定义，必须稳定、非空且不含 NUL；不要包含凭据或用户
  正文，也不要写入日志。
- `idempotency_key` 由 Runtime 根据 Run、Invocation 和 operation key 生成；必须传给
  支持幂等的外部服务。
- 正常退出自动写 `succeeded`，后续进入时 `should_execute=false`。
- 普通异常、取消或进程崩溃保留 `pending`，恢复时仍用原幂等键重试。
- 只有已确定业务失败时调用 `mark_failed()`；`failed` 默认禁止自动重试。
- Ledger 不保存外部响应 payload，也不能代替 Capability 自己的业务 State。

如果外部服务没有可靠幂等能力，不得声明 `automatic + idempotent`；应使用
`manual + unsafe`，并接受当前版本崩溃后只能安全失败、没有人工恢复 API 的限制。

### 7.8 日志

关键入口、出口、拒绝、取消和失败应使用：

```python
from agent_runtime.core.logging import log_business_event
```

可以记录可用的 `session_id`、`run_id`、`invocation_id`、`task_id`、`message_id`、
`capability_id`、状态、稳定错误码、错误类型和耗时。禁止记录：

- API Key、认证头、密码和数据库连接串；
- 用户消息和模型输出正文；
- System Prompt、完整请求体和工具原始参数/结果；
- Parent/Child 私有 State、Checkpoint 内容；
- 每个流式 token/delta。

日志默认写入 `logs/agent-runtime-YYYY-MM-DD.log`。

## 8. 测试与验收

### 8.1 建议测试层次

1. **Manifest**：严格字段、策略组合和未知字段拒绝。
2. **工厂/生命周期**：同步工厂、初始化、健康、清理。
3. **调用契约**：completed、OUT_OF_SCOPE、流式文本、取消、异常。
4. **私有 State**：真实或内存 Checkpointer 下的 scope 和 Schema 兼容。
5. **副作用**：Operation 正常、未知结果、确定失败和幂等重试。
6. **正式链路**：Manifest → Registry → 权限 → Router → Gateway → HTTP/SSE →
   Parent 历史。

测试必须使用显式权限。不要因为测试方便而在 Runtime 中自动授权。

### 8.2 相关专项命令

验证 Manifest、Source、Registry、契约和 Gateway：

```powershell
uv run --locked pytest -q `
  tests/capabilities/test_manifest.py `
  tests/capabilities/test_manifest_source.py `
  tests/capabilities/test_contracts.py `
  tests/capabilities/test_registry.py `
  tests/capabilities/test_invocation_gateway.py
```

运行全部 Capability 测试：

```powershell
uv run --locked pytest -q tests/capabilities
```

运行默认完整回归；默认使用 Fake Model，外部依赖测试按开关跳过：

```powershell
uv run --locked pytest -q
```

真实 PostgreSQL + Redis 集成需要 Docker Desktop 已启动且两个 Compose 服务健康：

```powershell
$env:RUN_POSTGRES_TESTS='1'
$env:RUN_REDIS_TESTS='1'
uv run --locked pytest -q tests/integration/test_stage_three_gate.py
uv run --locked pytest -q
```

`test_stage_three_gate.py` 使用第三个协议测试 ID `weather_lookup`，并实际经过 HTTP 202、
启动期 Registry、显式权限、动态 Router、通用 Invocation Gateway、真实
PostgreSQL/Redis、GET SSE、Task、RuntimeEvent 和 Parent 公共历史。它是新 Capability
正式产品链路测试的当前参考。

### 8.3 构建和静态门禁

```powershell
uv lock --check
uv pip check
uv run python -m compileall -q src tests
uv build
docker compose config --quiet
git diff --check
```

构建后应检查 wheel 中包含新增 Python 包和 `manifest.yaml`。不要只在源码目录验证后
就假定分发包已包含资源。

### 8.4 完成标准

新 Capability 至少应满足：

- Manifest 通过当前 `CapabilityManifest` 校验；
- Registry 启动日志显示初始化和健康检查完成，没有隔离诊断；
- 固定本地用户存在显式 allow 权限；
- 范围内请求产生一次 Run、一个最终公共消息和正确 capability_id；
- 范围外请求在零输出、零 Operation、零 Checkpoint 前返回 OUT_OF_SCOPE；
- 取消、超时和异常形成稳定结果，不泄露内部详情；
- 若有私有 State，scope 和版本兼容测试通过；
- 若有副作用，恢复窗口与 Operation Ledger 测试通过；
- 相关专项、默认回归、真实依赖集成和构建门禁通过。

## 9. 常见问题排查

### 9.1 Manifest 校验失败

现象：日志出现“Capability来源隔离”，诊断为 `MANIFEST_VALIDATION_FAILED`、
`YAML_SYNTAX_ERROR`、`YAML_ROOT_TYPE_ERROR` 或 `YAML_STRUCTURE_ERROR`。

检查：

- 文件名是否精确为 `manifest.yaml`；
- 根节点是否只有一个 YAML 映射；
- 是否遗漏任何必填字段；
- 是否出现未知字段；
- 布尔和数字是否错误写成字符串；
- `entrypoint`、SemVer、ID 和策略组合是否合法；
- YAML 是否包含循环别名、集合或过深结构。

先运行“启动前检查 Manifest”命令，再启动 Runtime。

### 9.2 重复 Capability ID

现象：相关来源状态为 `duplicate_id`，诊断码为 `DUPLICATE_CAPABILITY_ID`。

同一合法 ID 的全部来源都会被隔离，包括其中一个 Manifest 还存在其他字段错误的
情况。删除或更名冲突来源后重启；不要依赖文件排序选择实现。

### 9.3 entrypoint 或初始化失败

`entrypoint_failed` 常见原因：

- 模块不可导入或属性不存在；
- entrypoint 不是 `:create_capability`；
- 工厂是异步函数、参数名不对、带默认值或额外参数；
- 工厂返回 coroutine；
- 实例的三个异步方法签名不精确。

`initialize_failed` 表示工厂已经成功，但 `initialize()` 抛错或返回了非 `None`。检查
当天中文业务日志中的稳定诊断和错误类型；日志不会记录原始异常正文。

### 9.4 Capability 已加载，但 Router 不选择

按顺序检查：

1. `RUNTIME_DEMO_MODE` 是否为 `false`；
2. Manifest 是否 `enabled=true`；
3. 当前 `LOCAL_USER_ID` 是否有 `allowed=true`；
4. `health_check()` 是否 healthy，或 degraded 且 `allow_degraded=true`；
5. `description` 是否明确覆盖当前请求；
6. 本轮是否已因 OUT_OF_SCOPE 拒绝过该 Capability。

无授权候选会得到 `CAPABILITY_PERMISSION_DENIED`；有授权但没有可服务实例会得到
`CAPABILITY_UNAVAILABLE`；所有可选 Capability 都合法拒绝后，Parent 返回
unsupported，而不是运行失败。

### 9.5 权限写入后仍被拒绝

确认授权脚本和 Runtime 使用相同 `.env`、数据库和 `LOCAL_USER_ID`。权限没有缓存，
无需重启；如果 Router 已选择后权限被撤销，Invocation 前双检仍会拒绝，这是预期行为。

### 9.6 Health 返回 degraded 或 unhealthy

`degraded` 只有在 `allow_degraded=true` 时可服务。`unhealthy` 和检查异常始终不进入
候选。健康快照默认 TTL 为 30 秒，可由 `CAPABILITY_HEALTH_TTL_SECONDS` 配置；过期
后首个请求会触发刷新。当前没有 Capability Health 管理 API。

### 9.7 State Schema 不兼容

现象：`CAPABILITY_STATE_VERSION_INCOMPATIBLE`。

已有 Task 固化的版本既不是当前 `state_schema_version`，也不在
`compatible_state_schema_versions`。只有真正能读取旧 State 时才能声明兼容；否则
使用 `task_action=new` 开始新 Task。Stage 3 不提供自动迁移。

### 9.8 并发繁忙或执行超时

- `CAPABILITY_BUSY`：bounded 槽位在 `acquire_timeout_seconds` 内不可用；此时未创建
  新 Task。
- `CAPABILITY_TIMEOUT`：`invoke()` 超过 `timeout_seconds`；检查取消探针使用位置和
  底层 SDK 是否能协作结束。

不要通过访问 Runtime Semaphore 或延长无限超时规避治理。

### 9.9 自定义错误码没有出现在客户端

这是当前实现的安全行为。Runtime 只公开固定白名单，未知 `CapabilityError.code` 会
映射为 `CAPABILITY_EXECUTION_FAILED`，内部 message/details 不公开。当前没有插件级
公开错误 Schema 扩展机制。

### 9.10 修改文件后行为不变

Registry 和 Python 模块都在进程启动期加载。完整停止并重启 Runtime。只有权限变化和
过期健康刷新是运行期动态行为；Manifest、代码、`enabled`、并发、超时和恢复策略均
不能热更新。

## 10. 当前能力限制

Stage 3 明确不支持：

- Manifest 或实现代码热加载；
- 动态 mount/unmount、DRAINING；
- 扫描项目包目录以外的插件 Manifest；
- Remote Capability、Remote Agent；
- 同一 Capability 多版本共存；
- 多实例 Registry 同步、分布式并发、Worker Lease、分布式任务队列；
- RBAC、ABAC、组织、租户、权限管理 API 或管理平台；
- Capability 自定义配置中心；当前正式 `capability_config` 为空只读映射；
- State 自动迁移；
- manual recovery API 或 UI；
- failed Operation 自动重试、Saga 或补偿事务；
- Workflow、多 Agent 串联/并联；
- Capability Admin API、文件上传、文件解析或 RAG 产品能力；
- Capability 自定义公开 SSE 类型或公开错误 Schema；
- 恰好一次恢复承诺。

当前 Runtime 是单用户可信环境、单进程执行。PostgreSQL 是 Run、Event、Task、
Operation 和权限的事实权威源；Redis 只是短期公开实时事件旁路。

## 11. 接入检查清单

提交 Review 前逐项确认：

- [ ] Capability 包位于 `src/agent_runtime/capabilities/<capability_id>/`
- [ ] 文件名为 `manifest.yaml`，所有字段和嵌套字段均符合关闭 Schema
- [ ] `description` 清楚描述处理范围和拒绝范围
- [ ] 同步 `create_capability(bootstrap_context)` 签名精确
- [ ] 三个生命周期方法为精确异步签名
- [ ] 范围判断在任何公开输出、Checkpoint 和 Operation 之前
- [ ] 流式输出只发送 `AgentTextEvent`，最终全文放在 `AgentResult.content`
- [ ] Child Graph 使用提供的 Checkpointer 和 `agent_context.config`
- [ ] 没有直接操作 Parent、Router、SSE、Redis、Session 或 Run 终态
- [ ] 每个长操作都检查取消，`CancelledError` 原样传播
- [ ] State scope 和 Schema 版本有测试证据
- [ ] 外部副作用策略真实且与 recovery policy 匹配
- [ ] idempotent 副作用全部使用 Operation Ledger 和供应方幂等键
- [ ] 日志为中文业务事件，且不包含正文、Prompt、State、凭据或原始工具数据
- [ ] 已为目标 `LOCAL_USER_ID` 显式授权
- [ ] 修改后已重启 Runtime
- [ ] 单元、Capability 专项、默认回归、真实依赖集成和构建门禁通过

## 12. 当前代码参考

遇到疑问时优先对照以下已验收实现：

- Manifest：`src/agent_runtime/capabilities/manifest.py`
- Source：`src/agent_runtime/capabilities/source.py`
- Capability 契约：`src/agent_runtime/capabilities/contracts.py`
- 三上下文：`src/agent_runtime/runtime/agent_contract.py`
- Registry：`src/agent_runtime/capabilities/registry.py`
- 通用调用链：`src/agent_runtime/capabilities/invocation_gateway.py`
- 并发、超时和错误映射：`src/agent_runtime/capabilities/execution.py`
- Operation Ledger：`src/agent_runtime/capabilities/operation_ledger.py`
- State Scope：`src/agent_runtime/capabilities/state_scope.py`
- 普通聊天参考：`src/agent_runtime/capabilities/general_chat/`
- LangGraph 参考：`src/agent_runtime/capabilities/en_to_zh/`
- 正式第三 ID 门禁：`tests/integration/test_stage_three_gate.py`

若实际代码与本手册不一致，应停止扩展并按项目文档优先级报告冲突，不要在 Capability
内部绕过 Runtime 契约。

## 13. 远程 A2A 接入（RA-DEV-01/02）

当前实现静态发现、受信 Runtime Adapter、持久绑定、恢复和远程取消确认屏障。
远程配置不加入默认运行目录；真实联调仍需 RA-DEV-03 文本补参及后续审核门禁。
普通 SDK 的 `invoke()` 不提供远程绑定权限，正式调用由 Invocation Gateway 注入受控出口。

远程 Manifest 使用下面的 `transport`，其余必填字段沿用第 3 节；`state_scope` 只能是
`invocation`，即使 `side_effect_policy=none` 也不允许 Regenerate。旧顶层 `entrypoint`
会被拒绝；本地能力必须使用 `transport: {type: local, entrypoint: ...}`，没有双轨兼容。

```yaml
transport:
  type: remote_a2a
  agent_card_url: https://agent.example/.well-known/agent-card.json
  auth:
    type: bearer
    token_env: SEMANTIC_AGENT_TOKEN
```

Token 由 Runtime 从环境变量解析，Manifest、Card 快照、Bootstrap 和业务日志中均不能
保存其值。默认只接受 HTTPS 和受控 Bearer；`REMOTE_A2A_DEVELOPMENT_MODE=true` 才
允许 HTTP 或 `auth: {type: none}`，该安全开关与会绕过 Router 的 `RUNTIME_DEMO_MODE`
独立。`REMOTE_A2A_HTTP_TIMEOUT_SECONDS` 为所有远程 HTTP 操作的全局有限正超时，默认
10 秒。当前只支持 Card 与调用接口同源，按 scheme、规范化 host 和有效 port 比较；
拒绝 URL 凭据、片段、越界接口和全部重定向，不提供额外来源白名单。
`REMOTE_A2A_POLL_INTERVAL_SECONDS` 默认为 0.25 秒，用于 GetTask 轮询与最高 5 秒的
恢复退避。自动恢复的远程 Manifest 使用 `automatic + idempotent`；不把传输账本作为
`automatic + none` 恢复策略的隐式例外。

协议锁定 [A2A v1.0.1](https://github.com/a2aproject/A2A/tree/v1.0.1)，官方 Python SDK
锁定 `a2a-sdk==1.2.3`，仅使用 `RestTransport`，未启用可选 extra；SDK 基础依赖自身
包含的 json-rpc 包不代表 Runtime 支持该传输。不会通过 SDK Factory 回退旧协议。
Card 必须提供 `supportedInterfaces[]` 中 `protocolBinding=HTTP+JSON`、
`protocolVersion=1.0` 的接口，默认输入输出包含 `text/plain`，且认证要求可满足，
不能含未支持的 required extension。Card 不声明操作清单：三操作支持及幂等保证由
互操作测试确认。刷新 Card 只影响健康，不替换启动身份、接口或认证约束。

| 操作 | 官方 1.0 HTTP+JSON 约定 |
| --- | --- |
| Send | 接口 base URL 下 `POST /message:send`，`configuration.returnImmediately=true`，`acceptedOutputModes=["text/plain"]` |
| 输入 | `messageId`、`role=ROLE_USER`、`parts=[{"text":"当前输入"}]`；续发保持同一 `taskId/contextId` |
| Get | `GET /tasks/{id}`，返回具有稳定 `id/contextId` 的 Task |
| Cancel | `POST /tasks/{id}:cancel`，返回实际 Task 状态；不能仅凭请求成功宣称本地取消完成 |

所有请求带 `A2A-Version: 1.0`，Card 的 tenant 前缀按官方 SDK 规则进入操作路径。
Send 必须返回 Task，裸 Message 不满足本 Profile。completed Task 必须有非空文本
Artifact，input-required 必须有稳定消息 ID 及非空文本提示；原始状态使用官方
`TASK_STATE_*` 枚举。Client 不重试、不轮询、不写 Ledger、不决定本地 Run 终态。

离线协议和安全检查：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/capabilities/test_remote_a2a_client.py tests/capabilities/test_remote_agent_card.py tests/capabilities/test_registry.py tests/capabilities/test_regeneration_policy.py tests/capabilities/test_regeneration_submission.py tests/test_remote_service_bootstrap.py -q
```

共享替身位于 `tests/fake_a2a.py`，覆盖完成、working、input-required、Cancel、同
messageId 重送、冲突和受理后断连；不连接真实 Agent 或模型。

Runtime 在首次 Send 前把来源及身份 hash 写入 Invocation started；恢复不得覆盖该值。
初次消息 ID 为 Invocation 确定性 UUIDv5，绑定前未知用同 ID 重送，绑定后只 Get。
Send/Cancel 使用 `remote_send:<messageId>` / `remote_cancel:<taskId>` 账本，Get 不登记
Operation。绑定必须在 Send 正常退出标记 succeeded 前落盘。身份变化、旧事件缺少可信
身份、结果未知均保留活动 Run 与恢复证据；远程恢复次数可以超过 3，本地仍限制三次。

取消、执行超时和 Session 删除先确认远程终态。未知时 Run 保持 cancel_requested，
删除返回可重试 409。内部首次取消意图区分 explicit/timeout，重建后分别维持
stopped/cancelled 与 CAPABILITY_TIMEOUT/incomplete/failed；不向产品协议暴露 reason。
远程 input-required 当前仅保留内部未决边界，文本 Interrupt/Resume API 与页面归 RA-DEV-03。

显式范围拒绝使用 Task metadata 的 `control_signal="OUT_OF_SCOPE"`；还必须由 Get
确认同 Task/context 的 rejected 终态，且无 Artifact、公开输出、业务 State 或 Child
Checkpoint。只有满足正式架构 17.3～17.5 的 provisional Task 才能原子清理本次精确
Send 行并恢复原 current。普通 rejected、其他 Operation 与未知结果不能使用该例外；
本地 Capability 继续遵守零 Operation 拒绝规则。

隔离 PostgreSQL 与保留独立 Fake 的真实 Runtime 探针位于
`tests/integration/test_remote_runtime_postgres.py`；显式设置 `RUN_POSTGRES_TESTS=1`，
仅使用项目 Compose 中已建立的 `agent_runtime_ra02_probe` 测试库。它验证服务重建、
绑定前后崩溃、取消删除屏障和原子拒绝，不替代真实远程 Agent 四条链路验收。
