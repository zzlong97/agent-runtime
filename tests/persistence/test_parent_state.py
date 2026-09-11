import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage


def test_parent_state_store_setup_initializes_langgraph_tables(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence import parent_state

    class FakeCheckpointer:
        setup_called = False

        async def setup(self) -> None:
            self.setup_called = True

    checkpointer = FakeCheckpointer()

    @asynccontextmanager
    async def fake_open_checkpointer(settings):
        yield checkpointer

    monkeypatch.setattr(
        parent_state,
        "_open_checkpointer",
        fake_open_checkpointer,
    )
    store = parent_state.PostgresParentStateStore(Settings(_env_file=None))

    asyncio.run(store.setup())

    assert checkpointer.setup_called is True


def test_parent_state_store_persists_initial_human_message_as_messages_channel(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence import parent_state

    captured: dict[str, object] = {}

    class FakeCheckpointer:
        def get_next_version(self, current, channel):
            assert current is None
            assert channel is None
            return "0001.test"

        async def aput(self, config, checkpoint, metadata, new_versions):
            captured["config"] = config
            captured["checkpoint"] = checkpoint
            captured["metadata"] = metadata
            captured["new_versions"] = new_versions

    @asynccontextmanager
    async def fake_open_checkpointer(settings):
        yield FakeCheckpointer()

    monkeypatch.setattr(
        parent_state,
        "_open_checkpointer",
        fake_open_checkpointer,
    )
    store = parent_state.PostgresParentStateStore(Settings(_env_file=None))
    session_id = uuid4()
    message = HumanMessage(content="First message", id=str(uuid4()))

    asyncio.run(store.store_initial_human_message(session_id, message))

    assert captured["config"] == {
        "configurable": {
            "thread_id": str(session_id),
            "checkpoint_ns": "",
        }
    }
    checkpoint = captured["checkpoint"]
    assert checkpoint["channel_values"]["messages"] == [message]
    assert checkpoint["channel_versions"]["messages"] == "0001.test"
    assert checkpoint["updated_channels"] == ["messages"]
    assert captured["new_versions"] == {"messages": "0001.test"}
    assert captured["metadata"] == {
        "source": "input",
        "step": -1,
        "parents": {},
    }


def test_parent_state_store_restores_public_messages(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence import parent_state

    session_id = uuid4()
    message = HumanMessage(content="First message", id=str(uuid4()))

    class FakeCheckpointer:
        async def aget_tuple(self, config):
            assert config == {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_ns": "",
                }
            }
            return SimpleNamespace(
                checkpoint={"channel_values": {"messages": [message]}}
            )

    @asynccontextmanager
    async def fake_open_checkpointer(settings):
        yield FakeCheckpointer()

    monkeypatch.setattr(
        parent_state,
        "_open_checkpointer",
        fake_open_checkpointer,
    )
    store = parent_state.PostgresParentStateStore(Settings(_env_file=None))

    restored = asyncio.run(store.get_messages(session_id))

    assert restored == [message]
