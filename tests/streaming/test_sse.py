import json
from uuid import UUID

import pytest


@pytest.mark.parametrize(
    ("event_name", "payload_type", "payload", "expected"),
    [
        (
            "message",
            "MessageEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "message_id": UUID("00000000-0000-0000-0000-000000000002"),
                "delta": "你好",
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "message_id": "00000000-0000-0000-0000-000000000002",
                "delta": "你好",
            },
        ),
        (
            "error",
            "ErrorEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "code": "MODEL_CALL_FAILED",
                "message": "模型调用失败",
                "retryable": False,
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "code": "MODEL_CALL_FAILED",
                "message": "模型调用失败",
                "retryable": False,
            },
        ),
        (
            "done",
            "DoneEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "status": "completed",
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "status": "completed",
            },
        ),
    ],
)
def test_encode_sse_emits_only_stable_product_protocol(
    event_name: str,
    payload_type: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    from agent_runtime.api.schemas import chat
    from agent_runtime.streaming.sse import encode_sse

    schema_type = getattr(chat, payload_type)

    encoded = encode_sse(event_name, schema_type.model_validate(payload))

    lines = encoded.splitlines()
    assert lines[0] == f"event: {event_name}"
    assert json.loads(lines[1].removeprefix("data: ")) == expected
    assert lines[2:] == [""]
    assert "node" not in encoded
    assert "checkpoint" not in encoded
