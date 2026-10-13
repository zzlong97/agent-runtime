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

    database_url: PostgresDsn = Field(
        default=PostgresDsn(
            "postgresql://postgres:postgres@localhost:5432/agent_runtime"
        ),
        description=(
            "Runtime、Session、Run、事件和 Checkpoint 共用的 PostgreSQL 连接地址；"
            "只允许 postgres、postgresql 或 postgresql+asyncpg 协议，不得写入业务日志。"
        ),
    )
    dashscope_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "阿里云百炼模型调用使用的可选密钥；自动化测试默认使用 Fake Model，"
            "真实联调时才配置，任何日志和公开协议均不得输出该值。"
        ),
    )
    llm_base_url: AnyHttpUrl | None = Field(
        default=None,
        description=(
            "阿里云百炼 OpenAI 兼容接口的可选基础 URL；默认测试不依赖该地址，"
            "真实模型联调时必须与所选模型服务匹配。"
        ),
    )
    llm_model: str | None = Field(
        default=None,
        description=(
            "真实模型联调使用的百炼模型标识；默认测试为 null 并注入 Fake Model，"
            "不得把具体模型设为自动化测试硬依赖。"
        ),
    )
    local_user_id: str = Field(
        default="local-user",
        description=(
            "单用户可信环境中的固定本地用户标识；Stage 3 不接受客户端覆盖，"
            "并用它约束 Session 所有权和 Capability 权限。"
        ),
    )
    log_level: str = Field(
        default="INFO",
        description=(
            "Python 与中文业务日志的最低输出级别；只允许 CRITICAL、ERROR、"
            "WARNING、INFO 或 DEBUG，读取后统一规范为大写。"
        ),
    )
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
    run_coordinator_scan_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        description=(
            "单进程 Run Coordinator 扫描 queued Run 的补偿间隔秒数；"
            "Stage 2.5 默认 1 秒，只补偿进程内唤醒丢失，不提供多实例 Lease。"
        ),
    )
    run_cancel_grace_seconds: float = Field(
        default=2.0,
        gt=0,
        description=(
            "持久 Run 接受显式取消后等待执行器协作退出的最长秒数；超过该宽限期"
            "仅强制取消当前进程内对应任务，不承诺回滚已经发生的外部副作用。"
        ),
    )
    capability_health_ttl_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Capability 健康快照的全局有效秒数；Stage 3 默认 30 秒，达到过期边界后"
            "由首个需要服务的请求触发单飞刷新，不用于后台持续探测。"
        ),
    )
    runtime_demo_mode: bool = Field(
        default=False,
        description=(
            "是否启用仅供开发环境使用的确定性 Runtime 演示图；默认关闭，启用后"
            "请求绕过正式 Capability Router，但仍经过真实 Run、Event、Redis、SSE、"
            "Interrupt 与 Resume 链路。"
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
