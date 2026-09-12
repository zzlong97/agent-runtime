import pytest


def test_stage_one_model_requires_complete_bailian_configuration() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.core.model import ModelConfigurationError, build_chat_model

    with pytest.raises(ModelConfigurationError) as captured:
        build_chat_model(Settings(_env_file=None))

    assert captured.value.code == "LLM_CONFIGURATION_INCOMPLETE"
    assert captured.value.message == (
        "聊天模型配置不完整，请设置 DASHSCOPE_API_KEY、LLM_BASE_URL 和 LLM_MODEL"
    )


def test_stage_one_model_uses_configured_openai_compatible_endpoint() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.core.model import build_chat_model

    model = build_chat_model(
        Settings(
            _env_file=None,
            dashscope_api_key="test-secret",
            llm_base_url="https://example.com/v1",
            llm_model="qwen-test",
        )
    )

    assert model.model_name == "qwen-test"
    assert str(model.openai_api_base).rstrip("/") == "https://example.com/v1"
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "test-secret"
    assert model.streaming is True
