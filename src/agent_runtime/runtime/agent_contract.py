"""Stage 2.5 现有 Child Agent 的最小统一执行契约。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.child_result import ChildResult

type AgentCapabilityId = Literal["general_chat", "en_to_zh"]
type CancellationProbe = Callable[[], Awaitable[bool]]
type AgentEventHandler = Callable[["AgentTextEvent"], None]

logger = logging.getLogger(__name__)


class AgentTextEvent(BaseModel):
    """Agent 可经类型化出口发送的唯一用户可见文本事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(
        min_length=1,
        description=(
            "Agent 本次生成的非空用户可见文本片段；不得包含 Prompt、私有 State、"
            "工具原始参数、工具原始结果或基础设施对象。"
        ),
    )


class AgentEventOutlet:
    """只把已定义 Agent 事件交给 Runtime 提供的受控处理器。"""

    def __init__(self, handler: AgentEventHandler) -> None:
        self._handler = handler

    def emit(self, event: AgentTextEvent) -> None:
        """发送一个类型化文本事件，拒绝原始消息或任意对象。"""

        if not isinstance(event, AgentTextEvent):
            raise TypeError("Agent 事件出口只接受已定义的类型化事件")
        self._handler(event)


class AgentCancellation:
    """向 Agent 暴露只读取消探针，不暴露 Coordinator 或 Run Repository。"""

    def __init__(self, probe: CancellationProbe) -> None:
        self._probe = probe

    async def is_requested(self) -> bool:
        """返回 Runtime 是否已请求取消当前 Run。"""

        return bool(await self._probe())

    async def raise_if_requested(self) -> None:
        """已请求取消时传播 asyncio 标准取消异常。"""

        if await self.is_requested():
            raise asyncio.CancelledError


class RunOperationGateway(Protocol):
    """RunContext 可调用但不能访问底层 Repository 的 Operation 入口。"""

    run_id: UUID
    invocation_id: UUID

    def idempotency_key(self, operation_key: str) -> str:
        """生成绑定当前调用的稳定副作用幂等键。"""

        ...

    def operation(
        self,
        operation_key: str,
    ) -> AbstractAsyncContextManager[Any]:
        """返回 Runtime 管理的异步 Operation Context。"""

        ...


@dataclass(frozen=True, slots=True, init=False)
class RunContext:
    """提供稳定调用标识、取消、事件出口与受控 Operation Ledger。"""

    run_id: UUID | None
    request_id: UUID | None
    input_message_id: UUID | None
    response_message_id: UUID
    cancellation: AgentCancellation
    events: AgentEventOutlet
    invocation_id: UUID | None
    _operation_gateway: RunOperationGateway | None = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        *,
        run_id: UUID | None,
        request_id: UUID | None,
        input_message_id: UUID | None,
        response_message_id: UUID,
        cancellation: AgentCancellation,
        events: AgentEventOutlet,
        invocation_id: UUID | None = None,
        operation_gateway: RunOperationGateway | None = None,
    ) -> None:
        """构造受控上下文并校验 Operation 入口绑定的调用身份。"""

        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "input_message_id", input_message_id)
        object.__setattr__(self, "response_message_id", response_message_id)
        object.__setattr__(self, "cancellation", cancellation)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "invocation_id", invocation_id)
        object.__setattr__(self, "_operation_gateway", operation_gateway)
        if operation_gateway is None:
            return
        if run_id is None or operation_gateway.run_id != run_id:
            raise ValueError("Operation Gateway 与 RunContext 的 Run 不一致")
        if invocation_id is None or operation_gateway.invocation_id != invocation_id:
            raise ValueError("Operation Gateway 与 RunContext 的 Invocation 不一致")

    def idempotency_key(self, operation_key: str) -> str:
        """生成当前 Invocation 下指定业务操作的稳定幂等键。"""

        gateway = self._require_operation_gateway()
        return gateway.idempotency_key(operation_key)

    def operation(
        self,
        operation_key: str,
    ) -> AbstractAsyncContextManager[Any]:
        """返回由 Runtime 管理的异步 Operation Context。"""

        gateway = self._require_operation_gateway()
        return gateway.operation(operation_key)

    def _require_operation_gateway(self) -> RunOperationGateway:
        """缺少 S3 Operation Ledger 绑定时明确拒绝副作用入口。"""

        if self._operation_gateway is None:
            raise RuntimeError("当前 RunContext 未配置 Operation Ledger")
        return self._operation_gateway


@dataclass(frozen=True, slots=True)
class AgentContext:
    """只提供固定能力标识及其隔离的 Child 执行配置。"""

    session_id: UUID
    capability_id: AgentCapabilityId
    thread_id: str
    config: RunnableConfig


@dataclass(frozen=True, slots=True)
class TaskInput:
    """保存与 Parent 消息对象完全隔离的本轮派生输入视图。"""

    messages: tuple[BaseMessage, ...]

    def __post_init__(self) -> None:
        """深拷贝消息及嵌套字段，阻止 Agent 修改 Parent 权威历史。"""

        object.__setattr__(
            self,
            "messages",
            tuple(message.model_copy(deep=True) for message in self.messages),
        )


class Agent(Protocol):
    """现有 Capability 与测试 Fake Agent 必须实现的统一调用接口。"""

    async def run(
        self,
        *,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> ChildResult:
        """执行一次受控任务并仅返回极薄 ChildResult。"""

        ...


def run_context_from_parent_config(
    config: RunnableConfig,
    *,
    response_message_id: UUID,
    cancellation_probe: CancellationProbe,
    event_handler: AgentEventHandler,
) -> RunContext:
    """从受控 Parent metadata 构造 Agent 可见的最小 RunContext。"""

    metadata = config.get("metadata", {})
    return RunContext(
        run_id=_optional_uuid(metadata.get("run_id"), field_name="run_id"),
        request_id=_optional_uuid(
            metadata.get("request_id"),
            field_name="request_id",
        ),
        input_message_id=_optional_uuid(
            metadata.get("input_message_id"),
            field_name="input_message_id",
        ),
        response_message_id=response_message_id,
        cancellation=AgentCancellation(cancellation_probe),
        events=AgentEventOutlet(event_handler),
        invocation_id=_optional_uuid(
            metadata.get("invocation_id"),
            field_name="invocation_id",
        ),
    )


async def execute_agent(
    agent: Agent,
    *,
    run_context: RunContext,
    agent_context: AgentContext,
    task_input: TaskInput,
) -> ChildResult:
    """通过固定三上下文入口执行 Agent，并记录不含正文的业务日志。"""

    log_business_event(
        logger,
        "Agent统一执行入口",
        run_id=run_context.run_id,
        request_id=run_context.request_id,
        session_id=agent_context.session_id,
        message_id=run_context.response_message_id,
        capability_id=agent_context.capability_id,
    )
    try:
        result = await agent.run(
            run_context=run_context,
            agent_context=agent_context,
            task_input=task_input,
        )
    except asyncio.CancelledError:
        log_business_event(
            logger,
            "Agent统一执行取消",
            run_id=run_context.run_id,
            request_id=run_context.request_id,
            session_id=agent_context.session_id,
            message_id=run_context.response_message_id,
            capability_id=agent_context.capability_id,
            status="cancelled",
        )
        raise
    except Exception as error:
        log_business_event(
            logger,
            "Agent统一执行失败",
            level=logging.ERROR,
            run_id=run_context.run_id,
            request_id=run_context.request_id,
            session_id=agent_context.session_id,
            message_id=run_context.response_message_id,
            capability_id=agent_context.capability_id,
            error_type=type(error).__name__,
            error_code=getattr(error, "code", None),
        )
        raise
    validated = ChildResult.model_validate(result)
    log_business_event(
        logger,
        "Agent统一执行出口",
        run_id=run_context.run_id,
        request_id=run_context.request_id,
        session_id=agent_context.session_id,
        message_id=run_context.response_message_id,
        capability_id=agent_context.capability_id,
        status=validated.status,
        control_signal=validated.control_signal,
    )
    return validated


def _optional_uuid(value: object, *, field_name: str) -> UUID | None:
    """把可选 metadata 字段转换为 UUID，并拒绝损坏的稳定标识。"""

    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"Agent RunContext 的 {field_name} 不是有效 UUID") from error
