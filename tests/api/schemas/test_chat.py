import pytest
from pydantic import ValidationError


def test_chat_request_rejects_client_supplied_user_id() -> None:
    from agent_runtime.api.schemas.chat import ChatCompletionRequest

    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "session_id": None,
                "message": {"content": "Hello"},
                "user_id": "client-selected-user",
            }
        )


def test_chat_request_rejects_blank_message_content() -> None:
    from agent_runtime.api.schemas.chat import ChatCompletionRequest

    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "session_id": None,
                "message": {"content": "   "},
            }
        )
