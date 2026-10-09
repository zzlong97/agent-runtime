"""历史适配器动态 capability_id 回归测试。"""

from uuid import uuid4

import pytest


def test_history_preserves_valid_manifest_capability_id() -> None:
    from langchain_core.messages import AIMessage

    from agent_runtime.history import MessageHistoryAdapter

    adapter = MessageHistoryAdapter()
    product_message = adapter._to_product_message(
        AIMessage(
            content="天气查询结果",
            id=str(uuid4()),
            additional_kwargs={
                "runtime_status": "completed",
                "capability_id": "weather_lookup",
            },
        )
    )

    assert product_message is not None
    assert product_message.capability_id == "weather_lookup"


def test_history_rejects_capability_id_outside_manifest_rules() -> None:
    from langchain_core.messages import AIMessage

    from agent_runtime.history import MessageHistoryAdapter, MessageHistoryError

    adapter = MessageHistoryAdapter()
    with pytest.raises(MessageHistoryError) as captured:
        adapter._to_product_message(
            AIMessage(
                content="非法能力结果",
                id=str(uuid4()),
                additional_kwargs={
                    "runtime_status": "completed",
                    "capability_id": "invalid-id",
                },
            )
        )

    assert captured.value.code == "MESSAGE_HISTORY_INVALID"
