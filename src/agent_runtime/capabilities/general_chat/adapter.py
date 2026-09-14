"""普通聊天能力与 Parent Graph 之间的固定 Stage 1 适配层。"""

from collections.abc import Callable
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
from agent_runtime.graph.child_result import CapabilityInvocation
from agent_runtime.graph.config import (
    child_thread_config,
    public_message_id_from_parent_config,
    session_id_from_parent_config,
)
from agent_runtime.graph.context import build_refreshed_child_input
from agent_runtime.graph.parent import build_public_ai_message
from agent_runtime.graph.state import ParentState


class GeneralChatAdapter:
    """刷新普通聊天 Child 快照，并分离控制结果与公共消息。"""

    def __init__(
        self,
        *,
        capability: GeneralChatCapability,
        message_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """绑定固定普通聊天能力和服务端消息 UUID 生成器。"""

        self._capability = capability
        self._message_id_factory = message_id_factory

    async def invoke(
        self,
        state: ParentState,
        config: RunnableConfig,
    ) -> CapabilityInvocation:
        """从 Parent 派生输入，执行能力并返回标准调用结果。"""

        emitted_messages: list[BaseMessage] = []
        writer = get_stream_writer()

        def emit(event: tuple[BaseMessage, dict[str, object]]) -> None:
            message, _metadata = event
            public_event = message.model_copy(
                update={
                    "additional_kwargs": {
                        **message.additional_kwargs,
                        "capability_id": "general_chat",
                    }
                }
            )
            emitted_messages.append(public_event)
            writer(public_event)

        session_id = session_id_from_parent_config(config)
        message_id = public_message_id_from_parent_config(config)
        result = await self._capability.run(
            messages=build_refreshed_child_input(state["messages"]),
            config=child_thread_config(session_id, "general_chat"),
            emit=emit,
        )
        if result.status != "completed":
            return CapabilityInvocation(result=result)
        public_message = build_public_ai_message(
            emitted_messages,
            message_id=message_id or self._message_id_factory(),
        )
        public_message = AIMessage.model_validate(
            public_message.model_copy(
                update={
                    "additional_kwargs": {
                        **public_message.additional_kwargs,
                        "runtime_status": "completed",
                        "capability_id": "general_chat",
                    }
                }
            )
        )
        return CapabilityInvocation(
            result=result,
            message=public_message,
        )
