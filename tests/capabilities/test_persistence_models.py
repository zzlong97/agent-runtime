from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest


NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
TASK_ID = UUID("00000000-0000-0000-0000-000000003401")
SESSION_ID = UUID("00000000-0000-0000-0000-000000003402")
RUN_ID = UUID("00000000-0000-0000-0000-000000003403")
INVOCATION_ID = UUID("00000000-0000-0000-0000-000000003404")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000003405")


def _active_task():
    from agent_runtime.capabilities.persistence_models import CapabilityTask

    return CapabilityTask(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capability_id="general_chat",
        state_schema_version="v1",
        status="active",
        last_run_id=None,
        last_invocation_id=None,
        created_at=NOW,
        updated_at=NOW,
        ended_at=None,
    )


def test_capability_task_is_immutable_and_enforces_terminal_timestamp() -> None:
    task = _active_task()

    assert task.status == "active"
    with pytest.raises(FrozenInstanceError):
        task.status = "completed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="active Task 的 ended_at 必须为空"):
        replace(task, ended_at=NOW)
    with pytest.raises(ValueError, match="终态 Task 的 ended_at 不能为空"):
        replace(task, status="completed")
    with pytest.raises(ValueError, match="Task 状态非法"):
        replace(task, status="queued")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("capability_id", "GeneralChat", "Capability ID 格式非法"),
        ("state_schema_version", " ", "State Schema 版本不能为空"),
        ("state_schema_version", "v" * 65, "State Schema 版本最长 64 个字符"),
    ],
)
def test_capability_task_rejects_invalid_identifiers(
    field_name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_active_task(), **{field_name: value})


def test_context_operation_and_permission_models_are_closed() -> None:
    from agent_runtime.capabilities.persistence_models import (
        CapabilityOperation,
        CapabilityTaskContext,
        UserCapabilityPermission,
    )

    context = CapabilityTaskContext(
        session_id=SESSION_ID,
        capability_id="general_chat",
        current_task_id=TASK_ID,
        updated_at=NOW,
    )
    operation = CapabilityOperation(
        operation_id=OPERATION_ID,
        run_id=RUN_ID,
        invocation_id=INVOCATION_ID,
        task_id=TASK_ID,
        capability_id="general_chat",
        operation_key="send-email",
        idempotency_key="a" * 64,
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    permission = UserCapabilityPermission(
        user_id="runtime-user",
        capability_id="general_chat",
        allowed=False,
        created_at=NOW,
        updated_at=NOW,
    )

    assert context.current_task_id == TASK_ID
    assert operation.status == "pending"
    assert permission.allowed is False


def test_operation_and_permission_reject_invalid_values() -> None:
    from agent_runtime.capabilities.persistence_models import (
        CapabilityOperation,
        UserCapabilityPermission,
    )

    operation = CapabilityOperation(
        operation_id=OPERATION_ID,
        run_id=RUN_ID,
        invocation_id=INVOCATION_ID,
        task_id=TASK_ID,
        capability_id="general_chat",
        operation_key="send-email",
        idempotency_key="a" * 64,
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    cases = (
        ({"operation_key": " "}, "Operation Key 不能为空"),
        ({"idempotency_key": "A" * 64}, "小写 SHA-256"),
        ({"status": "unknown"}, "Operation 状态非法"),
    )
    for changes, message in cases:
        with pytest.raises(ValueError, match=message):
            replace(operation, **changes)

    with pytest.raises(ValueError, match="用户 ID 不能为空"):
        UserCapabilityPermission(
            user_id=" ",
            capability_id="general_chat",
            allowed=True,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(TypeError, match="allowed 必须是布尔值"):
        UserCapabilityPermission(
            user_id="runtime-user",
            capability_id="general_chat",
            allowed=1,  # type: ignore[arg-type]
            created_at=NOW,
            updated_at=NOW,
        )
