"""Environment-backed application settings."""

from functools import lru_cache
from typing import Any

from pydantic import AnyHttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_DATABASE_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})


class Settings(BaseSettings):
    """Configuration loaded from process environment and the project ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: PostgresDsn = PostgresDsn(
        "postgresql://postgres:postgres@localhost:5432/agent_runtime"
    )
    dashscope_api_key: SecretStr | None = None
    llm_base_url: AnyHttpUrl | None = None
    llm_model: str | None = None
    local_user_id: str = "local-user"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def require_supported_database_scheme(cls, value: PostgresDsn) -> PostgresDsn:
        """Accept native PostgreSQL URLs and the configured asyncpg URL form."""

        if value.scheme not in _DATABASE_SCHEMES:
            raise ValueError(
                "DATABASE_URL must use postgres://, postgresql://, "
                "or postgresql+asyncpg://"
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: Any) -> str:
        """Normalize and validate standard Python log level names."""

        if not isinstance(value, str) or value.upper() not in _LOG_LEVELS:
            raise ValueError("LOG_LEVEL must be a standard Python log level")
        return value.upper()

    @property
    def database_connection_string(self) -> str:
        """Return the PostgreSQL DSN in the form expected by database clients."""

        connection_string = self.database_url.unicode_string()
        if self.database_url.scheme == "postgresql+asyncpg":
            return connection_string.replace(
                "postgresql+asyncpg://",
                "postgresql://",
                1,
            )
        return connection_string


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached view of runtime settings."""

    return Settings()
