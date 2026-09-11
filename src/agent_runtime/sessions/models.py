"""Stage 1 Session metadata."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Session:
    """The minimal persisted Session entity for Stage 1."""

    session_id: UUID
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
