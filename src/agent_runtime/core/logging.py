"""进程日志配置与结构化中文业务事件。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_MANAGED_HANDLER_ATTRIBUTE = "_agent_runtime_daily_handler"
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "authorization",
        "client_secret",
        "connection_string",
        "connection_uri",
        "connection_url",
        "content",
        "cookie",
        "credentials",
        "database_url",
        "dsn",
        "message",
        "password",
        "private_key",
        "prompt",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "set_cookie",
        "system_prompt",
        "token",
        "user_message",
    }
)


class _DailyFileHandler(logging.Handler):
    """按日志记录的本地日期写入独立 UTF-8 文件。"""

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._current_date: str | None = None
        self._file_handler: logging.FileHandler | None = None
        setattr(self, _MANAGED_HANDLER_ATTRIBUTE, True)

    def emit(self, record: logging.LogRecord) -> None:
        """在跨日时切换文件，再交由标准 FileHandler 写入。"""

        try:
            record_date = datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d"
            )
            if record_date != self._current_date:
                self._switch_file(record_date)
            if self._file_handler is not None:
                self._file_handler.emit(record)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """刷新当前日期文件。"""

        if self._file_handler is not None:
            self._file_handler.flush()

    def close(self) -> None:
        """关闭当前日期文件并释放句柄。"""

        self.acquire()
        try:
            if self._file_handler is not None:
                self._file_handler.close()
                self._file_handler = None
        finally:
            try:
                super().close()
            finally:
                self.release()

    def _switch_file(self, record_date: str) -> None:
        """切换到 ``agent-runtime-YYYY-MM-DD.log``。"""

        if self._file_handler is not None:
            self._file_handler.close()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            self._log_dir / f"agent-runtime-{record_date}.log",
            encoding="utf-8",
        )
        file_handler.setLevel(self.level)
        file_handler.setFormatter(self.formatter)
        self._file_handler = file_handler
        self._current_date = record_date


def configure_logging(
    log_level: str,
    *,
    log_dir: str | Path = "logs",
) -> None:
    """在进程启动或测试重建应用时配置控制台与按日业务日志文件。"""

    normalized_level = log_level.upper()
    numeric_level = getattr(logging, normalized_level, None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"不支持的日志级别：{log_level}")

    logging.basicConfig(level=numeric_level, format=_LOG_FORMAT)
    package_logger = logging.getLogger("agent_runtime")
    package_logger.setLevel(numeric_level)
    package_logger.propagate = True

    for handler in tuple(package_logger.handlers):
        if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
            package_logger.removeHandler(handler)
            handler.close()

    daily_handler = _DailyFileHandler(Path(log_dir))
    daily_handler.setLevel(numeric_level)
    daily_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    package_logger.addHandler(daily_handler)
    log_business_event(
        package_logger,
        "日志系统初始化完成",
        log_level=normalized_level,
        log_dir=str(Path(log_dir)),
    )


def log_business_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """以稳定 JSON 字段记录中文业务事件，并统一脱敏敏感值。"""

    payload = {
        "event": event,
        **{
            field_name: _sanitize_value(field_name, field_value)
            for field_name, field_value in fields.items()
        },
    }
    logger.log(
        level,
        "业务日志 %s",
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def _sanitize_value(field_name: str, value: Any) -> Any:
    """递归转换可序列化字段，并按字段名屏蔽敏感内容。"""

    normalized_name = field_name.strip().lower().replace("-", "_")
    if _is_sensitive_field(normalized_name):
        return "<已脱敏>"
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(str(key), nested_value)
            for key, nested_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_sanitize_value(field_name, item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_sensitive_field(field_name: str) -> bool:
    """判断业务日志字段是否必须脱敏。"""

    return (
        field_name in _SENSITIVE_FIELD_NAMES
        or field_name.endswith("_api_key")
        or field_name.endswith("_content")
        or field_name.endswith("_password")
        or field_name.endswith("_prompt")
        or field_name.endswith("_secret")
        or field_name.endswith("_token")
        or field_name.endswith("_dsn")
        or field_name.endswith("_private_key")
        or field_name.endswith("_credentials")
        or field_name.endswith("_connection_string")
        or field_name.endswith("_connection_uri")
        or field_name.endswith("_connection_url")
        or field_name.endswith("_authorization")
        or field_name.endswith("_cookie")
    )
