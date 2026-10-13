"""普通聊天测试 Capability 的标准 Stage 3 entrypoint。"""

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.capabilities.contracts import CapabilityBootstrapContext
from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> GeneralChatCapability:
    """从受控 Bootstrap Context 构造普通聊天 Capability。"""

    if bootstrap_context.manifest.capability_id != "general_chat":
        raise ValueError("普通聊天 entrypoint 只能绑定 general_chat Manifest")
    model_factory = bootstrap_context.model_factory
    if not callable(model_factory):
        raise TypeError("普通聊天 model_factory 必须可调用")
    model = model_factory()
    if not isinstance(model, BaseChatModel):
        raise TypeError("普通聊天 model_factory 必须返回 BaseChatModel")
    return GeneralChatCapability(
        scope_model=model,
        response_model=model,
        checkpointer=bootstrap_context.child_checkpointer,
    )
