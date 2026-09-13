"""Session 列表不透明游标测试。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest


def test_session_cursor_round_trip_preserves_composite_sort_key() -> None:
    from agent_runtime.sessions.models import SessionCursor
    from agent_runtime.sessions.pagination import decode_cursor, encode_cursor

    cursor = SessionCursor(
        updated_at=datetime(2026, 9, 13, 8, 30, 45, 123456, tzinfo=UTC),
        session_id=UUID("00000000-0000-0000-0000-000000000321"),
    )

    encoded = encode_cursor(cursor)

    assert "+" not in encoded
    assert "/" not in encoded
    assert "=" not in encoded
    assert decode_cursor(encoded) == cursor


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "不是-base64",
        "e30",
        "eyJ2IjoyLCJ1cGRhdGVkX2F0IjoiMjAyNi0wOS0xM1QwODowMDowMCswMDowMCIs"
        "InNlc3Npb25faWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEifQ",
    ],
)
def test_decode_cursor_rejects_invalid_or_unsupported_payload(cursor: str) -> None:
    from agent_runtime.sessions.pagination import (
        InvalidSessionCursorError,
        decode_cursor,
    )

    with pytest.raises(InvalidSessionCursorError) as captured:
        decode_cursor(cursor)

    assert captured.value.code == "SESSION_CURSOR_INVALID"
    assert captured.value.message == "Session 列表游标无效"
    assert captured.value.status_code == 400
    assert captured.value.retryable is False


def test_decode_cursor_rejects_timestamp_without_timezone() -> None:
    from base64 import urlsafe_b64encode
    import json

    from agent_runtime.sessions.pagination import (
        InvalidSessionCursorError,
        decode_cursor,
    )

    payload = {
        "v": 1,
        "updated_at": "2026-09-13T08:00:00",
        "session_id": "00000000-0000-0000-0000-000000000001",
    }
    encoded = urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")

    with pytest.raises(InvalidSessionCursorError):
        decode_cursor(encoded)
