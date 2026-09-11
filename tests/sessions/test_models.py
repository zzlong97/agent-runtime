from dataclasses import fields
from datetime import UTC, datetime
from uuid import uuid4


def test_session_contains_only_stage_one_metadata_fields() -> None:
    from agent_runtime.sessions.models import Session

    now = datetime.now(UTC)
    session = Session(
        session_id=uuid4(),
        user_id="local-user",
        title="Hello",
        created_at=now,
        updated_at=now,
    )

    assert [field.name for field in fields(Session)] == [
        "session_id",
        "user_id",
        "title",
        "created_at",
        "updated_at",
    ]
    assert session.created_at.tzinfo is UTC
    assert session.updated_at == session.created_at
