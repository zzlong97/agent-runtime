"""消息反馈产品 Schema 契约测试。"""

import pytest
from pydantic import ValidationError


def test_feedback_request_accepts_only_stage_two_actions() -> None:
    from agent_runtime.api.schemas.feedback import FeedbackRequest

    for action in ("like", "dislike", "cancel"):
        assert FeedbackRequest(action=action).action == action

    for payload in (
        {"action": "unknown"},
        {"action": "like", "session_id": "internal"},
    ):
        with pytest.raises(ValidationError):
            FeedbackRequest.model_validate(payload)


def test_feedback_response_represents_saved_and_cancelled_results() -> None:
    from agent_runtime.api.schemas.feedback import FeedbackResponse

    saved = FeedbackResponse.model_validate(
        {
            "message_id": "00000000-0000-0000-0000-000000002701",
            "feedback": "like",
        }
    )
    cancelled = FeedbackResponse.model_validate(
        {
            "message_id": "00000000-0000-0000-0000-000000002701",
            "feedback": None,
        }
    )

    assert saved.feedback == "like"
    assert cancelled.feedback is None


def test_feedback_schemas_describe_every_parameter_in_chinese() -> None:
    from agent_runtime.api.schemas.feedback import FeedbackRequest, FeedbackResponse

    expected_descriptions = {
        FeedbackRequest: {
            "action": (
                "要执行的最终反馈动作；like 保存喜欢，dislike 保存不喜欢，cancel "
                "幂等删除当前反馈；Stage 2 不接受其他值。"
            ),
        },
        FeedbackResponse: {
            "message_id": (
                "已完成反馈操作的公共 AIMessage 稳定 UUID；目标必须仍位于固定本地"
                "用户的当前活动分支。"
            ),
            "feedback": (
                "操作后的最终反馈值；like 或 dislike 表示已保存，cancel 成功后为 null。"
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
