import pytest
from pydantic import ValidationError


def test_settings_read_runtime_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://runtime:secret@db.example.com:5432/agent_runtime",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv(
        "LLM_BASE_URL",
        "https://workspace-id.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("LOCAL_USER_ID", "local-user-42")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    from agent_runtime.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.database_connection_string == (
        "postgresql://runtime:secret@db.example.com:5432/agent_runtime"
    )
    assert settings.dashscope_api_key is not None
    assert settings.dashscope_api_key.get_secret_value() == "test-key"
    assert str(settings.llm_base_url) == (
        "https://workspace-id.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert settings.llm_model == "qwen-plus"
    assert settings.local_user_id == "local-user-42"
    assert settings.log_level == "DEBUG"


def test_settings_do_not_require_a_real_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    from agent_runtime.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.dashscope_api_key is None


def test_settings_ignore_blank_values_in_copied_env_example(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=\n"
        "DASHSCOPE_API_KEY=\n"
        "LLM_BASE_URL=\n"
        "LLM_MODEL=\n"
        "LOCAL_USER_ID=\n"
        "LOG_LEVEL=\n",
        encoding="utf-8",
    )

    from agent_runtime.core.config import Settings

    settings = Settings(_env_file=env_file)

    assert settings.dashscope_api_key is None
    assert settings.llm_base_url is None
    assert settings.llm_model is None
    assert settings.local_user_id == "local-user"
    assert settings.log_level == "INFO"


def test_settings_reject_non_postgresql_database_urls() -> None:
    from agent_runtime.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///agent_runtime.db", _env_file=None)


def test_settings_normalize_asyncpg_url_for_psycopg_connections() -> None:
    from agent_runtime.core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://runtime:secret@localhost/agent_runtime",
        _env_file=None,
    )

    assert settings.database_url.scheme == "postgresql+asyncpg"
    assert settings.database_connection_string == (
        "postgresql://runtime:secret@localhost/agent_runtime"
    )


def test_settings_reject_unknown_log_levels() -> None:
    from agent_runtime.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(log_level="verbose", _env_file=None)
