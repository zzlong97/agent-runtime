"""英译汉测试 Capability 的标准 Stage 3 entrypoint。"""

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.capabilities.contracts import CapabilityBootstrapContext
from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> EnglishToChineseCapability:
    """从受控 Bootstrap Context 构造英译汉 Capability。"""

    if bootstrap_context.manifest.capability_id != "en_to_zh":
        raise ValueError("英译汉 entrypoint 只能绑定 en_to_zh Manifest")
    model_factory = bootstrap_context.model_factory
    if not callable(model_factory):
        raise TypeError("英译汉 model_factory 必须可调用")
    model = model_factory()
    if not isinstance(model, BaseChatModel):
        raise TypeError("英译汉 model_factory 必须返回 BaseChatModel")
    return EnglishToChineseCapability(
        scope_model=model,
        translation_model=model,
        checkpointer=bootstrap_context.child_checkpointer,
    )
