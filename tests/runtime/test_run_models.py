from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest


def _submission():
    from agent_runtime.runtime.models import RunSubmission

    session_id = uuid4()
    return RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "需要恢复的输入"}},
        request_fingerprint="a" * 64,
        created_at=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )


def test_run_submission_only_accepts_confirmed_run_types() -> None:
    from agent_runtime.runtime.models import RunType

    submission = _submission()

    assert submission.run_type == "normal"
    assert replace(submission, run_type="regenerate").run_type == "regenerate"
    with pytest.raises(ValueError, match="Run 类型"):
        replace(submission, run_type=cast(RunType, "retry"))


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("queued", "running"),
        ("queued", "cancelled"),
        ("queued", "failed"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "interrupted"),
        ("running", "cancel_requested"),
        ("running", "recovering"),
        ("recovering", "running"),
        ("recovering", "failed"),
        ("recovering", "interrupted"),
        ("recovering", "cancel_requested"),
        ("interrupted", "running"),
        ("interrupted", "cancelled"),
        ("cancel_requested", "cancelled"),
    ],
)
def test_run_state_machine_accepts_only_confirmed_transitions(
    current_status,
    target_status,
) -> None:
    from agent_runtime.runtime.models import can_transition_run

    assert can_transition_run(current_status, target_status) is True


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("queued", "completed"),
        ("queued", "interrupted"),
        ("recovering", "completed"),
        ("interrupted", "completed"),
        ("cancel_requested", "running"),
        ("completed", "failed"),
        ("failed", "running"),
        ("cancelled", "queued"),
    ],
)
def test_run_state_machine_rejects_unconfirmed_or_terminal_transitions(
    current_status,
    target_status,
) -> None:
    from agent_runtime.runtime.models import can_transition_run

    assert can_transition_run(current_status, target_status) is False


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "recovering",
        "interrupted",
        "cancel_requested",
        "completed",
        "failed",
        "cancelled",
    ],
)
def test_repeating_same_run_status_is_idempotent(status) -> None:
    from agent_runtime.runtime.models import can_transition_run

    assert can_transition_run(status, status) is True
