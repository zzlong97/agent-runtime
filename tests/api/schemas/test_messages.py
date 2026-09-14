"""历史消息产品 Schema 契约测试。"""

import pytest
from pydantic import ValidationError


def test_message_history_query_uses_stage_two_defaults_and_limits() -> None:
    from agent_runtime.api.schemas.messages import MessageHistoryQuery

    query = MessageHistoryQuery()

    assert query.before is None
    assert query.limit == 50
    for invalid_limit in (0, 101):
        with pytest.raises(ValidationError):
            MessageHistoryQuery(limit=invalid_limit)


def test_message_history_schemas_use_closed_values_and_reject_extra_fields() -> None:
    from agent_runtime.api.schemas.messages import ProductMessageResponse

    item = ProductMessageResponse.model_validate(
        {
            "message_id": "00000000-0000-0000-0000-000000001501",
            "role": "assistant",
            "content": "回复",
            "runtime_status": "completed",
            "capability_id": "general_chat",
            "feedback": None,
        }
    )

    assert item.role == "assistant"
    with pytest.raises(ValidationError):
        ProductMessageResponse.model_validate(
            {**item.model_dump(), "checkpoint_id": "internal"}
        )
    with pytest.raises(ValidationError):
        ProductMessageResponse.model_validate(
            {**item.model_dump(), "runtime_status": "failed"}
        )


def test_message_history_schemas_describe_every_parameter_in_chinese() -> None:
    from agent_runtime.api.schemas.messages import (
        MessageHistoryQuery,
        MessageHistoryResponse,
        ProductMessageResponse,
    )

    expected_descriptions = {
        MessageHistoryQuery: {
            "before": (
                "上一页首条消息的服务端稳定 UUID；省略或传 null 时读取当前活动 "
                "Parent 分支的最新一页，且该 UUID 必须存在于当前活动历史中。"
            ),
            "limit": (
                "本页最多返回的产品消息数量；允许 1～100，默认 50，"
                "从 before 之前向更早消息截取。"
            ),
        },
        ProductMessageResponse: {
            "message_id": "公共消息的服务端稳定 UUID，用于历史分页和后续消息操作。",
            "role": (
                "产品消息角色；user 表示 HumanMessage，assistant 表示 AIMessage，"
                "不暴露内部 System 或 Tool 消息。"
            ),
            "content": "公共消息的完整文本正文，不包含内部状态或 checkpoint 元数据。",
            "runtime_status": (
                "AIMessage 的运行终态，只允许 completed、unsupported、incomplete 或 "
                "stopped；HumanMessage 为 null，缺少 Stage 2 元数据的旧 AIMessage 按 "
                "completed 返回。"
            ),
            "capability_id": (
                "生成 AIMessage 的能力标识，只允许 general_chat、en_to_zh 或 null；"
                "HumanMessage 及缺少该元数据的旧消息为 null。"
            ),
            "feedback": (
                "固定本地用户对当前活动 AIMessage 的最终反馈，只允许 like、dislike "
                "或尚无反馈时的 null；只回显当前活动分支消息的最终反馈。"
            ),
        },
        MessageHistoryResponse: {
            "items": (
                "当前活动 Parent 分支的一页产品消息，过滤内部消息后按对话时间正序返回。"
            ),
            "next_before": (
                "更早一页应作为 before 传入的消息 UUID；没有更早产品消息时为 null。"
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
