"""从 Parent 公共消息派生 Stage 1 Child 本轮消息视图。"""

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent_runtime.core.errors import ApplicationError

_FULL_HISTORY_ROUND_LIMIT = 10
_TRIMMED_HISTORY_ROUND_COUNT = 5


class ContextBuilderError(ApplicationError):
    """Parent 公共消息无法构造合法的 Child 本轮输入。"""


def _is_completed_ai_message(message: AIMessage) -> bool:
    """只把正常完成的 AIMessage 计入完整公共轮次。"""

    runtime_status = message.additional_kwargs.get("runtime_status")
    return runtime_status in (None, "completed")


def build_child_message_view(
    parent_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """保留公共 SystemMessage，并按 10/5 规则选择完整轮次和当前消息。"""

    non_system_messages = [
        message
        for message in parent_messages
        if not isinstance(message, SystemMessage)
    ]
    if not non_system_messages or not isinstance(
        non_system_messages[-1], HumanMessage
    ):
        raise ContextBuilderError(
            code="CONTEXT_CURRENT_HUMAN_MISSING",
            message="Parent 公共消息中缺少当前 HumanMessage",
        )

    current_message = non_system_messages[-1]
    completed_rounds: list[tuple[HumanMessage, AIMessage]] = []
    pending_human: HumanMessage | None = None
    for message in non_system_messages[:-1]:
        if isinstance(message, HumanMessage):
            pending_human = message
            continue
        if isinstance(message, AIMessage):
            if pending_human is not None and _is_completed_ai_message(message):
                completed_rounds.append((pending_human, message))
            pending_human = None
            continue
        pending_human = None

    selected_rounds = completed_rounds
    if len(completed_rounds) >= _FULL_HISTORY_ROUND_LIMIT:
        selected_rounds = completed_rounds[-_TRIMMED_HISTORY_ROUND_COUNT:]

    system_messages = [
        message
        for message in parent_messages
        if isinstance(message, SystemMessage)
    ]
    selected_messages = [
        message
        for human_message, ai_message in selected_rounds
        for message in (human_message, ai_message)
    ]
    return [*system_messages, *selected_messages, current_message]


def build_refreshed_child_input(
    parent_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """生成先清空旧消息、再写入 Parent 派生视图的 Child 图输入。"""

    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *build_child_message_view(parent_messages),
    ]
