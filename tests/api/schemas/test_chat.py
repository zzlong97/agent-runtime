from uuid import UUID

import pytest
from pydantic import ValidationError


REQUEST_ID = UUID("00000000-0000-0000-0000-000000000010")


def test_chat_request_rejects_client_supplied_user_id() -> None:
    from agent_runtime.api.schemas.chat import ChatCompletionRequest

    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "request_id": str(REQUEST_ID),
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
                "request_id": str(REQUEST_ID),
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
                "当前用户提交的消息正文；Stage 2.5 必须是去除首尾空白后仍非空的"
                "字符串，并作为本轮 HumanMessage 内容。"
            ),
        },
        ChatCompletionRequest: {
            "request_id": (
                "客户端为本次普通消息生成的全局唯一 UUID；相同请求"
                "重试必须复用该值，同一值对应不同请求时返回 409。"
            ),
            "session_id": (
                "要继续对话的 Session UUID；省略或传 null 时由服务端创建新 "
                "Session，Stage 2.5 不接受客户端指定 user_id。"
            ),
            "message": (
                "本 Run 唯一的用户消息；Stage 2.5 只接受文本 "
                "content，正文只写入最小恢复输入与 Parent 公共消息。"
            ),
        },
        MessageEventData: {
            "session_id": "本轮对话所属的稳定 Session UUID。",
            "message_id": "本轮 AIMessage 的服务端稳定 UUID；同一回复的所有 delta 相同。",
            "capability_id": (
                "生成本次增量的能力标识；Stage 2 只允许 general_chat、"
                "en_to_zh 或尚未确定能力时的 null。"
            ),
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
            "message_id": (
                "本轮 AIMessage 的服务端稳定 UUID；与同一回复的 message 事件一致。"
            ),
            "capability_id": (
                "本轮最终采用的能力标识；Stage 2 只允许 general_chat、"
                "en_to_zh 或未采用能力时的 null。"
            ),
            "status": (
                "本轮最终状态；Stage 2 只允许 completed、unsupported、stopped 或 failed。"
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


def test_done_event_accepts_stopped_and_rejects_status_outside_stage_two_protocol() -> None:
    from agent_runtime.api.schemas.chat import DoneEventData

    stopped = DoneEventData.model_validate(
        {
            "session_id": "00000000-0000-0000-0000-000000000001",
            "message_id": "00000000-0000-0000-0000-000000000002",
            "capability_id": None,
            "status": "stopped",
        }
    )

    assert stopped.status == "stopped"

    with pytest.raises(ValidationError):
        DoneEventData.model_validate(
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "message_id": "00000000-0000-0000-0000-000000000002",
                "capability_id": None,
                "status": "interrupted",
            }
        )
