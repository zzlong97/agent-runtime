"""阿里云百炼 OpenAI 兼容聊天模型装配。"""

from langchain_openai import ChatOpenAI

from agent_runtime.core.config import Settings
from agent_runtime.core.errors import ApplicationError


class ModelConfigurationError(ApplicationError):
    """Stage 1 聊天模型配置缺失或不完整。"""


def build_chat_model(settings: Settings) -> ChatOpenAI:
    """使用服务端配置创建支持流式输出的百炼兼容 ChatOpenAI。"""

    if (
        settings.dashscope_api_key is None
        or settings.llm_base_url is None
        or settings.llm_model is None
    ):
        raise ModelConfigurationError(
            code="LLM_CONFIGURATION_INCOMPLETE",
            message=(
                "聊天模型配置不完整，请设置 DASHSCOPE_API_KEY、LLM_BASE_URL 和 "
                "LLM_MODEL"
            ),
        )
    return ChatOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=str(settings.llm_base_url),
        model=settings.llm_model,
        streaming=True,
    )
