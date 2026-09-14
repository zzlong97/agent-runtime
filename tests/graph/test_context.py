from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def _completed_round(index: int) -> list[HumanMessage | AIMessage]:
    return [
        HumanMessage(content=f"用户-{index}", id=str(uuid4())),
        AIMessage(content=f"助手-{index}", id=str(uuid4())),
    ]


@pytest.mark.parametrize("completed_rounds", [0, 1, 9])
def test_context_builder_keeps_all_rounds_before_ten(
    completed_rounds: int,
) -> None:
    from agent_runtime.graph.context import build_child_message_view

    system_message = SystemMessage(content="公共系统约束", id=str(uuid4()))
    history = [system_message]
    for index in range(completed_rounds):
        history.extend(_completed_round(index))
    current_message = HumanMessage(content="当前问题", id=str(uuid4()))
    history.append(current_message)

    view = build_child_message_view(history)

    assert view == history


def test_context_builder_keeps_only_five_rounds_once_ten_exist() -> None:
    from agent_runtime.graph.context import build_child_message_view

    system_message = SystemMessage(content="公共系统约束", id=str(uuid4()))
    history = [system_message]
    rounds: list[list[HumanMessage | AIMessage]] = []
    for index in range(10):
        round_messages = _completed_round(index)
        rounds.append(round_messages)
        history.extend(round_messages)
    current_message = HumanMessage(content="第十一轮问题", id=str(uuid4()))
    history.append(current_message)
    original_history = list(history)

    view = build_child_message_view(history)

    assert view == [
        system_message,
        *(message for round_messages in rounds[-5:] for message in round_messages),
        current_message,
    ]
    assert history == original_history
    assert len(history) == 22


def test_context_builder_preserves_system_messages_without_counting_them() -> None:
    from agent_runtime.graph.context import build_child_message_view

    first_system = SystemMessage(content="系统约束一", id=str(uuid4()))
    second_system = SystemMessage(content="系统约束二", id=str(uuid4()))
    history = [first_system]
    rounds: list[list[HumanMessage | AIMessage]] = []
    for index in range(10):
        round_messages = _completed_round(index)
        rounds.append(round_messages)
        history.extend(round_messages)
        if index == 3:
            history.append(second_system)
    current_message = HumanMessage(content="当前问题", id=str(uuid4()))
    history.append(current_message)

    view = build_child_message_view(history)

    assert view[:2] == [first_system, second_system]
    assert view[2:] == [
        *(message for round_messages in rounds[-5:] for message in round_messages),
        current_message,
    ]


def test_context_builder_excludes_non_completed_rounds() -> None:
    from agent_runtime.graph.context import build_child_message_view

    completed = _completed_round(0)
    unsupported_human = HumanMessage(content="不支持的问题", id=str(uuid4()))
    unsupported_ai = AIMessage(
        content="当前能力不支持",
        id=str(uuid4()),
        additional_kwargs={"runtime_status": "unsupported"},
    )
    incomplete_human = HumanMessage(content="执行中失败的问题", id=str(uuid4()))
    incomplete_ai = AIMessage(
        content="未完成内容",
        id=str(uuid4()),
        additional_kwargs={"runtime_status": "incomplete"},
    )
    stopped_human = HumanMessage(content="用户停止的问题", id=str(uuid4()))
    stopped_ai = AIMessage(
        content="停止前内容",
        id=str(uuid4()),
        additional_kwargs={"runtime_status": "stopped"},
    )
    current_message = HumanMessage(content="新的问题", id=str(uuid4()))
    history = [
        *completed,
        unsupported_human,
        unsupported_ai,
        incomplete_human,
        incomplete_ai,
        stopped_human,
        stopped_ai,
        current_message,
    ]

    view = build_child_message_view(history)

    assert view == [*completed, current_message]


def test_unsupported_and_incomplete_rounds_do_not_trigger_trimming() -> None:
    from agent_runtime.graph.context import build_child_message_view

    completed_messages: list[HumanMessage | AIMessage] = []
    for index in range(9):
        completed_messages.extend(_completed_round(index))
    unsupported_human = HumanMessage(content="不支持的问题", id=str(uuid4()))
    unsupported_ai = AIMessage(
        content="当前能力不支持",
        id=str(uuid4()),
        additional_kwargs={"runtime_status": "unsupported"},
    )
    incomplete_human = HumanMessage(content="执行中失败的问题", id=str(uuid4()))
    incomplete_ai = AIMessage(
        content="未完成内容",
        id=str(uuid4()),
        additional_kwargs={"runtime_status": "incomplete"},
    )
    current_message = HumanMessage(content="第十个有效问题", id=str(uuid4()))
    history = [
        *completed_messages,
        unsupported_human,
        unsupported_ai,
        incomplete_human,
        incomplete_ai,
        current_message,
    ]

    view = build_child_message_view(history)

    assert view == [*completed_messages, current_message]


def test_context_builder_requires_current_human_message() -> None:
    from agent_runtime.graph.context import ContextBuilderError, build_child_message_view

    with pytest.raises(ContextBuilderError) as captured:
        build_child_message_view([AIMessage(content="孤立回复", id=str(uuid4()))])

    assert captured.value.code == "CONTEXT_CURRENT_HUMAN_MISSING"
    assert captured.value.message == "Parent 公共消息中缺少当前 HumanMessage"
