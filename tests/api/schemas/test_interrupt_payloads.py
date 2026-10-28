"""补参关闭 Schema 与旧确认提示的安全投影。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_runtime.api.schemas.runs import PendingInterruptResponse, ResumeRunRequest


@pytest.mark.parametrize("payload", [
    {}, {"answer": ""}, {"answer": " \n\t"}, {"answer": 1}, {"answer": True},
    {"approved": "true"}, {"approved": 1}, {"approved": False, "answer": "参数"},
    {"answer": "参数", "extra": 1}, {"kind": "text", "answer": "参数"},
])
def test_resume_payload_is_closed_and_strict(payload):
    with pytest.raises(ValidationError):
        ResumeRunRequest(interrupt_id=uuid4(), request_id=uuid4(), resume_payload=payload)


@pytest.mark.parametrize("payload", [
    {"kind": "other", "prompt": "提示"}, {"kind": "text", "prompt": ""},
    {"kind": "text", "prompt": " "}, {"kind": "text", "prompt": "字" * 4001},
    {"kind": "text", "prompt": "提示", "remote": {"task_id": "secret"}},
])
def test_public_interrupt_payload_is_closed(payload):
    with pytest.raises(ValidationError):
        PendingInterruptResponse(interrupt_id=uuid4(), interrupt_payload=payload, created_at=datetime.now(UTC))
