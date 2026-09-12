"""Stage 1 普通聊天 Child Agent。"""

from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.core.errors import ApplicationError
from agent_runtime.graph.child_result import ChildResult

GENERAL_CHAT_SCOPE_PROMPT = """你是 general_chat 的能力边界判断器，只判断当前用户消息是否要求翻译。
凡是要求把内容从一种语言转换为另一种语言，无论源语言、目标语言或内容长短，都属于翻译请求。
解释词义、介绍语言知识但不要求转换原文，不属于翻译请求。
你只返回符合 Schema 的判断结果，不回答用户问题。"""

GENERAL_CHAT_SYSTEM_PROMPT = """你是 AgentRuntime 的普通聊天助手。
你只处理普通聊天、知识问答和简单咨询，不得执行任何翻译。
回答应简洁、直接、准确；除非用户明确要求展开，否则避免不必要的铺陈。"""

GeneralChatMessageEvent = tuple[BaseMessage, dict[str, Any]]


class GeneralChatError(ApplicationError):
    """普通聊天能力输入、边界判断或执行不符合运行约束。"""


class GeneralChatScopeDecision(BaseModel):
    """普通聊天能力的翻译范围判断结果。"""

    model_config = ConfigDict(extra="forbid")

    is_translation_request: bool = Field(
        description=(
            "判断当前 HumanMessage 是否要求执行任何语言方向的翻译；true 表示 "
            "general_chat 必须返回 OUT_OF_SCOPE，false 表示可进入普通聊天生成。"
        )
    )


class GeneralChatCapability:
    """先执行能力边界判断，再调用可持久化的普通聊天 Agent。"""

    def __init__(
        self,
        *,
        scope_model: BaseChatModel,
        response_model: BaseChatModel,
        checkpointer: BaseCheckpointSaver,
    ) -> None:
        """分别绑定范围判断模型和用户可见回复模型。"""

        self._scope_model = scope_model.with_structured_output(
            GeneralChatScopeDecision
        )
        self._agent = create_agent(
            model=response_model,
            tools=[],
            system_prompt=GENERAL_CHAT_SYSTEM_PROMPT,
            checkpointer=checkpointer,
            name="general_chat",
        )

    async def run(
        self,
        *,
        messages: list[BaseMessage],
        config: RunnableConfig,
        emit: Callable[[GeneralChatMessageEvent], None],
    ) -> ChildResult:
        """执行范围守卫；仅对范围内请求生成并转发消息事件。"""

        latest_message = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_message is None:
            raise GeneralChatError(
                code="GENERAL_CHAT_INVALID_INPUT",
                message="普通聊天输入中缺少 HumanMessage",
            )

        scope_request = HumanMessage(
            content=f"待判断的当前用户消息：\n{latest_message.content}"
        )
        try:
            output = await self._scope_model.ainvoke(
                [SystemMessage(content=GENERAL_CHAT_SCOPE_PROMPT), scope_request],
                config,
            )
            decision = GeneralChatScopeDecision.model_validate(output)
        except ValidationError as error:
            raise GeneralChatError(
                code="GENERAL_CHAT_SCOPE_INVALID_OUTPUT",
                message="普通聊天范围判断结果不符合 Schema",
            ) from error
        except Exception as error:
            raise GeneralChatError(
                code="GENERAL_CHAT_SCOPE_CALL_FAILED",
                message="普通聊天范围判断模型调用失败",
            ) from error

        if decision.is_translation_request:
            return ChildResult(
                status="rejected",
                control_signal="OUT_OF_SCOPE",
            )

        try:
            async for event in self._agent.astream(
                {"messages": messages},
                config,
                stream_mode="messages",
            ):
                emit(event)
        except Exception as error:
            raise GeneralChatError(
                code="GENERAL_CHAT_CALL_FAILED",
                message="普通聊天模型调用失败",
            ) from error
        return ChildResult(status="completed", control_signal=None)
