from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError


def test_public_runtime_event_schemas_are_closed_and_described_in_chinese() -> None:
    from agent_runtime.runtime.event_schemas import (
        PUBLIC_EVENT_SCHEMA_TYPES,
        PUBLIC_PAYLOAD_SCHEMA_TYPES,
    )

    for schema_type in (*PUBLIC_PAYLOAD_SCHEMA_TYPES, *PUBLIC_EVENT_SCHEMA_TYPES):
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        for field_name, field_schema in schema["properties"].items():
            description = field_schema.get("description")
            assert description, f"{schema_type.__name__}.{field_name} 缺少 description"
            assert any(
                "\u4e00" <= character <= "\u9fff" for character in description
            ), f"{schema_type.__name__}.{field_name} 必须使用中文 description"


@pytest.mark.parametrize(
    "sensitive_field",
    [
        "prompt",
        "system_prompt",
        "checkpoint",
        "state",
        "node_id",
        "tool_arguments",
        "tool_result",
        "request_body",
        "input_payload",
        "content",
    ],
)
def test_public_message_started_payload_rejects_sensitive_or_extra_fields(
    sensitive_field,
) -> None:
    from agent_runtime.runtime.event_schemas import MessageStartedPayload

    with pytest.raises(ValidationError):
        MessageStartedPayload.model_validate(
            {
                "response_message_id": str(uuid4()),
                "attempt": 1,
                sensitive_field: "不得公开",
            }
        )


def test_message_started_requires_positive_attempt() -> None:
    from agent_runtime.runtime.event_schemas import MessageStartedPayload

    with pytest.raises(ValidationError):
        MessageStartedPayload(
            response_message_id=uuid4(),
            attempt=0,
        )


def test_public_projection_rejects_internal_event_and_hides_internal_fields() -> None:
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        PublicEventProjectionError,
        project_public_event,
    )

    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    run_id = uuid4()
    internal = RuntimeEvent(
        event_id=uuid4(),
        run_id=run_id,
        seq=1,
        event_type="internal.execution.diagnostic",
        source="private-node-name",
        visibility="internal",
        payload={"diagnostic_code": "INTERNAL_ONLY"},
        schema_version=1,
        durability="durable",
        created_at=now,
    )

    with pytest.raises(PublicEventProjectionError):
        project_public_event(internal)

    public = RuntimeEvent(
        event_id=uuid4(),
        run_id=run_id,
        seq=2,
        event_type="message.started",
        source="private-node-name",
        visibility="public",
        payload=MessageStartedPayload(
            response_message_id=uuid4(),
            attempt=1,
        ).model_dump(mode="json"),
        schema_version=1,
        durability="durable",
        created_at=now,
    )
    projected = project_public_event(public)
    projected_data = projected.model_dump(mode="json")

    assert projected.event_type == "message.started"
    assert "source" not in projected_data
    assert "visibility" not in projected_data
    assert "durability" not in projected_data


def test_event_draft_requires_declared_public_durability_and_typed_internal_payload(
) -> None:
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import (
        MessageDeltaPayload,
        RuntimeEventSchemaError,
        serialize_event_draft_payload,
    )

    class InternalDiagnosticPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        diagnostic_code: str = Field(
            description="仅供服务端诊断使用的稳定内部代码。"
        )

    with pytest.raises(RuntimeEventSchemaError):
        serialize_event_draft_payload(
            RuntimeEventDraft(
                event_type="message.delta",
                source="executor",
                visibility="public",
                payload=MessageDeltaPayload(
                    response_message_id=uuid4(),
                    attempt=1,
                    delta="增量",
                ),
                schema_version=1,
                durability="durable",
                created_at=datetime.now(UTC),
            )
        )

    payload = serialize_event_draft_payload(
        RuntimeEventDraft(
            event_type="internal.execution.diagnostic",
            source="executor",
            visibility="internal",
            payload=InternalDiagnosticPayload(
                diagnostic_code="RECOVERY_CHECK",
            ),
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )
    )

    assert payload == {"diagnostic_code": "RECOVERY_CHECK"}


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("visibility", "private"),
        ("durability", "temporary"),
    ],
)
def test_event_draft_rejects_unknown_visibility_and_durability(
    field_name,
    field_value,
) -> None:
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import MessageStartedPayload

    arguments = {
        "event_type": "message.started",
        "source": "executor",
        "visibility": "public",
        "payload": MessageStartedPayload(
            response_message_id=uuid4(),
            attempt=1,
        ),
        "schema_version": 1,
        "durability": "durable",
        "created_at": datetime.now(UTC),
    }
    arguments[field_name] = field_value

    with pytest.raises(ValueError):
        RuntimeEventDraft(**arguments)
