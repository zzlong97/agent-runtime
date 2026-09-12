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


def test_chat_and_sse_schemas_describe_every_field() -> None:
    from agent_runtime.api.schemas.chat import (
        ChatCompletionRequest,
        ChatMessageRequest,
        DoneEventData,
        ErrorEventData,
        MessageEventData,
    )

    expected_descriptions = {
        ChatMessageRequest: {
            "content": (
                "当前用户提交的消息正文；Stage 1 必须是去除首尾空白后仍非空的"
                "字符串，并作为本轮 HumanMessage 内容。"
            ),
        },
        ChatCompletionRequest: {
            "session_id": (
                "要继续对话的 Session UUID；省略或传 null 时由服务端创建新 "
                "Session，Stage 1 不接受客户端指定 user_id。"
            ),
            "message": "本轮唯一的用户消息；Stage 1 只接受文本 content。",
        },
        MessageEventData: {
            "session_id": "本轮对话所属的稳定 Session UUID。",
            "message_id": "本轮 AIMessage 的服务端稳定 UUID；同一回复的所有 delta 相同。",
            "delta": "本次 SSE message 事件携带的增量文本，不包含内部图事件。",
        },
        ErrorEventData: {
            "session_id": "发生流式运行错误的 Session UUID。",
            "code": "稳定的应用错误码；不得包含内部节点或 checkpoint 信息。",
            "message": "面向客户端的中文错误说明，不暴露敏感异常详情。",
            "retryable": "客户端是否可以安全重试本轮请求的布尔标记。",
        },
        DoneEventData: {
            "session_id": "本轮对话所属的稳定 Session UUID。",
            "status": (
                "本轮最终状态；Stage 1 只允许 completed、unsupported 或 failed。"
            ),
        },
    }

    for schema_type, descriptions in expected_descriptions.items():
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        assert {
            name: definition["description"]
            for name, definition in schema["properties"].items()
        } == descriptions


def test_done_event_rejects_status_outside_stage_one_protocol() -> None:
    from agent_runtime.api.schemas.chat import DoneEventData

    with pytest.raises(ValidationError):
        DoneEventData.model_validate(
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "status": "interrupted",
            }
        )
