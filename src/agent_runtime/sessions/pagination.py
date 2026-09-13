"""Session 列表版本化不透明游标的编解码。"""

import base64
import binascii
from datetime import UTC, datetime
import json
from typing import Any
from uuid import UUID

from agent_runtime.core.errors import ApplicationError
from agent_runtime.sessions.models import SessionCursor

_CURSOR_VERSION = 1
_CURSOR_KEYS = {"v", "updated_at", "session_id"}


class InvalidSessionCursorError(ApplicationError):
    """客户端提交的 Session 列表游标无法安全解析。"""


def encode_cursor(cursor: SessionCursor) -> str:
    """把复合排序边界编码为客户端无需理解的 URL-safe 字符串。"""

    if cursor.updated_at.tzinfo is None or cursor.updated_at.utcoffset() is None:
        raise ValueError("Session 游标时间必须包含时区")
    payload = {
        "v": _CURSOR_VERSION,
        "updated_at": cursor.updated_at.astimezone(UTC).isoformat(),
        "session_id": str(cursor.session_id),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> SessionCursor:
    """严格校验版本、字段和类型后恢复复合排序边界。"""

    try:
        if not value:
            raise ValueError("游标不能为空")
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload: Any = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
            raise ValueError("游标字段不完整")
        if type(payload["v"]) is not int or payload["v"] != _CURSOR_VERSION:
            raise ValueError("游标版本不受支持")
        if not isinstance(payload["updated_at"], str):
            raise ValueError("游标时间格式无效")
        if not isinstance(payload["session_id"], str):
            raise ValueError("游标 Session ID 格式无效")
        updated_at = datetime.fromisoformat(payload["updated_at"])
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("游标时间必须包含时区")
        session_id = UUID(payload["session_id"])
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise InvalidSessionCursorError(
            code="SESSION_CURSOR_INVALID",
            message="Session 列表游标无效",
            status_code=400,
        ) from error

    return SessionCursor(
        updated_at=updated_at.astimezone(UTC),
        session_id=session_id,
    )
