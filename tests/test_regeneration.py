"""重新生成资格校验与 Parent checkpoint fork 测试。"""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest


def _message(
    message_id: str,
    *,
    role: str = "assistant",
    runtime_status: str | None = "completed",
):
    from agent_runtime.history import ProductMessage

    return ProductMessage(
        message_id=UUID(message_id),
        role=role,
        content="消息",
        runtime_status=runtime_status,
        capability_id=("general_chat" if role == "assistant" else None),
        feedback=None,
    )


class FakeHistoryAdapter:
    def __init__(self, messages) -> None:
        self.messages = tuple(messages)
        self.calls: list[UUID] = []

    async def get_active_messages(self, *, session_id: UUID):
        self.calls.append(session_id)
        return self.messages


class FakeParentGraph:
    def __init__(self, snapshots) -> None:
        self.snapshots = tuple(snapshots)
        self.history_calls: list[tuple[dict, dict]] = []
        self.update_calls: list[tuple[dict, dict]] = []
        self.fork_config = {
            "configurable": {
                "thread_id": "00000000-0000-0000-0000-000000001700",
                "checkpoint_ns": "",
                "checkpoint_id": "fork-checkpoint",
            }
        }

    async def aget_state_history(self, config, *, filter):
        self.history_calls.append((config, filter))
        for snapshot in self.snapshots:
            yield snapshot

    async def aupdate_state(self, config, values):
        self.update_calls.append((config, values))
        return self.fork_config


def test_checkpoint_forker_forks_answer_predecessor_and_assigns_new_message_id() -> None:
    from agent_runtime.regeneration import CheckpointForker

    session_id = UUID("00000000-0000-0000-0000-000000001700")
    old_message_id = UUID("00000000-0000-0000-0000-000000001702")
    new_message_id = UUID("00000000-0000-0000-0000-000000001703")
    answer_checkpoint = {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
            "checkpoint_id": "before-answer",
        }
    }
    graph = FakeParentGraph(
        [
            SimpleNamespace(next=(), config={"configurable": {}}),
            SimpleNamespace(
                next=("invoke_capability",),
                config=answer_checkpoint,
            ),
        ]
    )
    history = FakeHistoryAdapter(
        [
            _message(
                "00000000-0000-0000-0000-000000001701",
                role="user",
                runtime_status=None,
            ),
            _message(str(old_message_id)),
        ]
    )
    forker = CheckpointForker(history_adapter=history, parent_graph=graph)

    config = asyncio.run(
        forker.create_fork(
            session_id=session_id,
            message_id=old_message_id,
            response_message_id=new_message_id,
        )
    )

    assert history.calls == [session_id]
    assert graph.history_calls == [
        (
            {"configurable": {"thread_id": str(session_id)}},
            {"message_id": str(old_message_id)},
        )
    ]
    assert graph.update_calls == [(answer_checkpoint, {})]
    assert config == {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
            "checkpoint_id": "fork-checkpoint",
            "message_id": str(new_message_id),
        }
    }
    assert "message_id" not in graph.fork_config["configurable"]


def test_checkpoint_forker_can_capture_start_before_materializing_branch() -> None:
    """Run 落库前只捕获起点，不提前改变 Parent 活动分支。"""

    from agent_runtime.regeneration import CheckpointForker

    session_id = UUID("00000000-0000-0000-0000-000000001704")
    old_message_id = UUID("00000000-0000-0000-0000-000000001705")
    new_message_id = UUID("00000000-0000-0000-0000-000000001706")
    answer_checkpoint = {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
            "checkpoint_id": "captured-before-answer",
        }
    }
    graph = FakeParentGraph(
        [
            SimpleNamespace(
                next=("invoke_capability",),
                config=answer_checkpoint,
            )
        ]
    )
    graph.fork_config = {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
            "checkpoint_id": "materialized-fork",
        }
    }
    forker = CheckpointForker(
        history_adapter=FakeHistoryAdapter([_message(str(old_message_id))]),
        parent_graph=graph,
    )

    async def exercise():
        captured = await forker.find_start_checkpoint(
            session_id=session_id,
            message_id=old_message_id,
        )
        assert captured == answer_checkpoint
        assert graph.update_calls == []
        return await forker.create_fork_from_checkpoint(
            session_id=session_id,
            start_checkpoint_id="captured-before-answer",
            response_message_id=new_message_id,
        )

    config = asyncio.run(exercise())

    assert graph.update_calls == [(answer_checkpoint, {})]
    assert config["configurable"]["message_id"] == str(new_message_id)


@pytest.mark.parametrize(
    ("messages", "target_id"),
    [
        (
            [
                _message(
                    "00000000-0000-0000-0000-000000001711",
                    role="user",
                    runtime_status=None,
                ),
                _message("00000000-0000-0000-0000-000000001712"),
                _message(
                    "00000000-0000-0000-0000-000000001713",
                    role="user",
                    runtime_status=None,
                ),
                _message("00000000-0000-0000-0000-000000001714"),
            ],
            "00000000-0000-0000-0000-000000001712",
        ),
        (
            [
                _message(
                    "00000000-0000-0000-0000-000000001721",
                    role="user",
                    runtime_status=None,
                )
            ],
            "00000000-0000-0000-0000-000000001721",
        ),
        (
            [
                _message(
                    "00000000-0000-0000-0000-000000001731",
                    runtime_status="unsupported",
                )
            ],
            "00000000-0000-0000-0000-000000001731",
        ),
        (
            [
                _message(
                    "00000000-0000-0000-0000-000000001741",
                    runtime_status="incomplete",
                )
            ],
            "00000000-0000-0000-0000-000000001741",
        ),
        (
            [
                _message(
                    "00000000-0000-0000-0000-000000001751",
                    runtime_status="stopped",
                )
            ],
            "00000000-0000-0000-0000-000000001751",
        ),
        (
            [_message("00000000-0000-0000-0000-000000001761")],
            "00000000-0000-0000-0000-000000001799",
        ),
    ],
)
def test_checkpoint_forker_rejects_non_latest_completed_ai_message(
    messages,
    target_id: str,
) -> None:
    from agent_runtime.regeneration import CheckpointForker, RegenerationError

    session_id = UUID("00000000-0000-0000-0000-000000001710")
    graph = FakeParentGraph([])
    forker = CheckpointForker(
        history_adapter=FakeHistoryAdapter(messages),
        parent_graph=graph,
    )

    with pytest.raises(RegenerationError) as captured:
        asyncio.run(
            forker.create_fork(
                session_id=session_id,
                message_id=UUID(target_id),
                response_message_id=UUID(
                    "00000000-0000-0000-0000-000000001798"
                ),
            )
        )

    assert captured.value.code == "MESSAGE_REGENERATE_NOT_ALLOWED"
    assert captured.value.message == (
        "仅允许重新生成当前活动分支最新的 completed AIMessage"
    )
    assert captured.value.status_code == 409
    assert graph.history_calls == []
    assert graph.update_calls == []


def test_checkpoint_forker_rejects_missing_answer_checkpoint() -> None:
    from agent_runtime.regeneration import CheckpointForker, RegenerationError

    session_id = UUID("00000000-0000-0000-0000-000000001770")
    target_id = UUID("00000000-0000-0000-0000-000000001771")
    graph = FakeParentGraph(
        [SimpleNamespace(next=("route",), config={"configurable": {}})]
    )
    forker = CheckpointForker(
        history_adapter=FakeHistoryAdapter([_message(str(target_id))]),
        parent_graph=graph,
    )

    with pytest.raises(RegenerationError) as captured:
        asyncio.run(
            forker.create_fork(
                session_id=session_id,
                message_id=target_id,
                response_message_id=UUID(
                    "00000000-0000-0000-0000-000000001772"
                ),
            )
        )

    assert captured.value.code == "MESSAGE_REGENERATE_CHECKPOINT_NOT_FOUND"
    assert captured.value.message == "未找到可用于重新生成的 Parent checkpoint"
    assert captured.value.status_code == 409
    assert graph.update_calls == []


@pytest.mark.parametrize("failure_stage", ["history", "fork"])
def test_checkpoint_forker_converts_checkpoint_failures_to_stable_error(
    failure_stage: str,
) -> None:
    from agent_runtime.regeneration import CheckpointForker, RegenerationError

    session_id = UUID("00000000-0000-0000-0000-000000001780")
    target_id = UUID("00000000-0000-0000-0000-000000001781")
    answer_checkpoint = {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
            "checkpoint_id": "before-answer",
        }
    }

    class FailingGraph(FakeParentGraph):
        async def aget_state_history(self, config, *, filter):
            if failure_stage == "history":
                raise RuntimeError("数据库历史读取失败")
            yield SimpleNamespace(
                next=("invoke_capability",),
                config=answer_checkpoint,
            )

        async def aupdate_state(self, config, values):
            if failure_stage == "fork":
                raise RuntimeError("数据库 fork 写入失败")
            return await super().aupdate_state(config, values)

    forker = CheckpointForker(
        history_adapter=FakeHistoryAdapter([_message(str(target_id))]),
        parent_graph=FailingGraph([]),
    )

    with pytest.raises(RegenerationError) as captured:
        asyncio.run(
            forker.create_fork(
                session_id=session_id,
                message_id=target_id,
                response_message_id=UUID(
                    "00000000-0000-0000-0000-000000001782"
                ),
            )
        )

    expected = {
        "history": (
            "MESSAGE_REGENERATE_HISTORY_FAILED",
            "读取重新生成所需的 Parent 历史失败",
        ),
        "fork": (
            "MESSAGE_REGENERATE_FORK_FAILED",
            "创建消息重新生成分支失败",
        ),
    }[failure_stage]
    assert (captured.value.code, captured.value.message) == expected
    assert captured.value.status_code == 500
    assert captured.value.retryable is True
