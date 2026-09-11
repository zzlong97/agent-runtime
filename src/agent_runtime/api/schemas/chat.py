"""Stage 1 chat request schemas."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class ChatMessageRequest(BaseModel):
    """One user message accepted by the chat entry point."""

    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def require_non_blank_content(cls, value: str) -> str:
        """Reject empty or whitespace-only HumanMessage content."""

        if not value.strip():
            raise ValueError("message.content must be a non-empty string")
        return value


class ChatCompletionRequest(BaseModel):
    """Minimal chat request; user identity is always server controlled."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    message: ChatMessageRequest
