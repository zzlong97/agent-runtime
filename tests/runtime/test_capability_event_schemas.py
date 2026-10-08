from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_capability_internal_payload_schemas_are_closed_and_described() -> None:
    from agent_runtime.runtime.event_schemas import (
        INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_TYPES,
    )

    for schema_type in INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_TYPES:
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        for field_name, field_schema in schema["properties"].items():
            description = field_schema.get("description")
            assert description, f"{schema_type.__name__}.{field_name} 缺少说明"
            assert any(
                "\u4e00" <= character <= "\u9fff" for character in description
            )


def test_capability_invocation_payloads_reject_extra_or_invalid_combinations() -> None:
    from agent_runtime.runtime.event_schemas import (
        InvocationCancelledPayload,
        InvocationCompletedPayload,
        InvocationFailedPayload,
        InvocationStartedPayload,
    )

    common_fields = {
        "invocation_id",
        "capability_id",
        "task_id",
        "requested_task_action",
        "effective_task_action",
        "state_scope",
        "state_schema_version",
    }
    assert set(InvocationStartedPayload.model_fields) == common_fields
    assert set(InvocationCompletedPayload.model_fields) == common_fields | {
        "outcome"
    }
    assert set(InvocationFailedPayload.model_fields) == common_fields | {
        "error_code"
    }
    assert set(InvocationCancelledPayload.model_fields) == common_fields | {
        "outcome"
    }

    with pytest.raises(ValidationError):
        InvocationStartedPayload.model_validate(
            {
                "invocation_id": str(uuid4()),
                "capability_id": "general_chat",
                "task_id": None,
                "requested_task_action": "new",
                "effective_task_action": None,
                "state_scope": "invocation",
                "state_schema_version": None,
                "private_state": "不得进入事件",
            }
        )

    with pytest.raises(ValidationError):
        InvocationStartedPayload.model_validate(
            {
                "invocation_id": str(uuid4()),
                "capability_id": "general_chat",
                "task_id": None,
                "requested_task_action": "new",
                "effective_task_action": None,
                "state_scope": "invocation",
            }
        )

    with pytest.raises(ValidationError):
        InvocationCompletedPayload(
            invocation_id=uuid4(),
            capability_id="general_chat",
            task_id=None,
            requested_task_action="continue",
            effective_task_action=None,
            state_scope="session",
            state_schema_version=None,
            outcome="completed",
        )


def test_capability_internal_event_requires_fixed_payload_type() -> None:
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import (
        InvocationFailedPayload,
        InvocationStartedPayload,
        RuntimeEventSchemaError,
        serialize_event_draft_payload,
    )

    invocation_id = uuid4()
    with pytest.raises(RuntimeEventSchemaError):
        serialize_event_draft_payload(
            RuntimeEventDraft(
                event_type="internal.capability.invocation.started",
                source="runtime.capability",
                visibility="internal",
                payload=InvocationFailedPayload(
                    invocation_id=invocation_id,
                    capability_id="general_chat",
                    task_id=None,
                    requested_task_action="continue",
                    effective_task_action=None,
                    state_scope="session",
                    state_schema_version=None,
                    error_code="CAPABILITY_EXECUTION_FAILED",
                ),
                schema_version=1,
                durability="durable",
                created_at=datetime.now(UTC),
            )
        )

    payload = serialize_event_draft_payload(
        RuntimeEventDraft(
            event_type="internal.capability.invocation.started",
            source="runtime.capability",
            visibility="internal",
            payload=InvocationStartedPayload(
                invocation_id=invocation_id,
                capability_id="general_chat",
                task_id=None,
                requested_task_action="continue",
                effective_task_action=None,
                state_scope="session",
                state_schema_version=None,
            ),
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )
    )
    assert payload["invocation_id"] == str(invocation_id)
    assert payload["requested_task_action"] == "continue"
    assert payload["effective_task_action"] is None
    assert payload["state_scope"] == "session"


def test_invocation_terminal_payload_records_continue_degraded_to_new() -> None:
    from agent_runtime.runtime.event_schemas import InvocationCompletedPayload

    task_id = uuid4()
    payload = InvocationCompletedPayload(
        invocation_id=uuid4(),
        capability_id="general_chat",
        task_id=task_id,
        requested_task_action="continue",
        effective_task_action="new",
        state_scope="session",
        state_schema_version="v1",
        outcome="rejected",
    )

    assert payload.model_dump(mode="json") == {
        "invocation_id": str(payload.invocation_id),
        "capability_id": "general_chat",
        "task_id": str(task_id),
        "requested_task_action": "continue",
        "effective_task_action": "new",
        "state_scope": "session",
        "state_schema_version": "v1",
        "outcome": "rejected",
    }
