"""英译汉能力与 Parent Graph 之间的固定 Stage 1 适配层。"""

from collections.abc import Awaitable, Callable
import logging
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.child_result import CapabilityInvocation
from agent_runtime.graph.config import (
    child_thread_config,
    public_message_id_from_parent_config,
    session_id_from_parent_config,
)
from agent_runtime.graph.context import build_refreshed_child_input
from agent_runtime.graph.parent import build_public_ai_message
from agent_runtime.graph.state import ParentState
from agent_runtime.runtime.agent_contract import (
    Agent,
    AgentContext,
    AgentTextEvent,
    TaskInput,
    execute_agent,
    run_context_from_parent_config,
)

logger = logging.getLogger(__name__)


class EnglishToChineseAdapter:
    """刷新英译汉 Child 快照，并分离控制结果与公共消息。"""

    def __init__(
        self,
        *,
        capability: Agent,
        message_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """绑定固定英译汉能力和服务端消息 UUID 生成器。"""

        self._capability = capability
        self._message_id_factory = message_id_factory

    async def invoke(
        self,
        state: ParentState,
        config: RunnableConfig,
        *,
        cancellation_probe: Callable[[], Awaitable[bool]] | None = None,
    ) -> CapabilityInvocation:
        """从 Parent 派生输入，执行能力并返回标准调用结果。"""

        emitted_messages: list[BaseMessage] = []
        writer = get_stream_writer()

        def emit(event: AgentTextEvent) -> None:
            public_event = AIMessageChunk(
                content=event.text,
                additional_kwargs={"capability_id": "en_to_zh"},
            )
            emitted_messages.append(public_event)
            writer(public_event)

        session_id = session_id_from_parent_config(config)
        message_id = public_message_id_from_parent_config(config) or (
            self._message_id_factory()
        )

        async def never_cancelled() -> bool:
            return False

        child_config = child_thread_config(session_id, "en_to_zh")
        run_context = run_context_from_parent_config(
            config,
            response_message_id=message_id,
            cancellation_probe=cancellation_probe or never_cancelled,
            event_handler=emit,
        )
        agent_context = AgentContext(
            session_id=session_id,
            capability_id="en_to_zh",
            thread_id=str(child_config["configurable"]["thread_id"]),
            config=child_config,
        )
        task_input = TaskInput(
            messages=tuple(build_refreshed_child_input(state["messages"]))
        )
        log_business_event(
            logger,
            "英译汉能力入口",
            session_id=session_id,
            message_id=message_id,
            capability_id="en_to_zh",
        )
        result = await execute_agent(
            self._capability,
            run_context=run_context,
            agent_context=agent_context,
            task_input=task_input,
        )
        if result.status != "completed":
            log_business_event(
                logger,
                "英译汉能力出口",
                session_id=session_id,
                message_id=message_id,
                capability_id="en_to_zh",
                status=result.status,
                control_signal=result.control_signal,
            )
            return CapabilityInvocation(result=result)
        public_message = build_public_ai_message(
            emitted_messages,
            message_id=message_id,
        )
        public_message = AIMessage.model_validate(
            public_message.model_copy(
                update={
                    "additional_kwargs": {
                        **public_message.additional_kwargs,
                        "runtime_status": "completed",
                        "capability_id": "en_to_zh",
                    }
                }
            )
        )
        log_business_event(
            logger,
            "英译汉能力出口",
            session_id=session_id,
            message_id=public_message.id,
            capability_id="en_to_zh",
            status=result.status,
        )
        return CapabilityInvocation(
            result=result,
            message=public_message,
        )
