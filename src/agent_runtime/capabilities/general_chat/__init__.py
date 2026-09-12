"""普通聊天能力。"""

from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
from agent_runtime.capabilities.general_chat.agent import (
    GENERAL_CHAT_SCOPE_PROMPT,
    GENERAL_CHAT_SYSTEM_PROMPT,
    GeneralChatCapability,
    GeneralChatError,
    GeneralChatScopeDecision,
)

__all__ = [
    "GENERAL_CHAT_SCOPE_PROMPT",
    "GENERAL_CHAT_SYSTEM_PROMPT",
    "GeneralChatAdapter",
    "GeneralChatCapability",
    "GeneralChatError",
    "GeneralChatScopeDecision",
]
