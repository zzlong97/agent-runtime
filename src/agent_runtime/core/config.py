"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_DATABASE_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})


class Settings(BaseSettings):
    """从进程环境变量和项目 ``.env`` 文件加载运行配置。"""

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
    log_dir: Path = Field(
        default=Path("logs"),
        description=(
            "按日期保存 AgentRuntime 中文业务日志的目录；默认使用项目运行目录下的 "
            "logs，日志文件名为 agent-runtime-YYYY-MM-DD.log。"
        ),
    )
    redis_url: RedisDsn = Field(
        default=RedisDsn("redis://localhost:6379/0"),
        description=(
            "短期公开 RuntimeEvent 使用的 Redis 连接地址；Stage 2.5 仅允许作为实时"
            "旁路，不是 Run 或事件事实权威源。"
        ),
    )
    redis_stream_ttl_seconds: int = Field(
        default=1800,
        ge=1,
        description=(
            "Redis Run 事件 Stream 每次成功写入后刷新的存活秒数；Stage 2.5 默认"
            "为 1800 秒，即 30 分钟。"
        ),
    )
    redis_socket_timeout_seconds: float = Field(
        default=0.5,
        gt=0,
        description=(
            "Redis 建连和命令等待的最长秒数；用于在 Redis 不可用时快速降级，"
            "不得阻塞 PostgreSQL Run 主流程。"
        ),
    )

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

    @property
    def redis_connection_string(self) -> str:
        """返回 Redis 客户端可直接使用的完整连接地址。"""

        return self.redis_url.unicode_string()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached view of runtime settings."""

    return Settings()
