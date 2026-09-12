from uuid import UUID, uuid4


def test_parent_thread_config_uses_session_id() -> None:
    from agent_runtime.graph.config import parent_thread_config

    session_id = uuid4()

    assert parent_thread_config(session_id) == {
        "configurable": {"thread_id": str(session_id)}
    }


def test_child_thread_config_is_scoped_to_capability() -> None:
    from agent_runtime.graph.config import child_thread_config

    session_id = uuid4()

    assert child_thread_config(session_id, "general_chat") == {
        "configurable": {"thread_id": f"{session_id}:general_chat"}
    }


def test_parent_thread_config_carries_one_stable_response_message_id() -> None:
    from agent_runtime.graph.config import (
        parent_thread_config,
        public_message_id_from_parent_config,
    )

    session_id = uuid4()
    message_id = UUID("00000000-0000-0000-0000-000000000901")

    config = parent_thread_config(session_id, message_id=message_id)

    assert config == {
        "configurable": {
            "thread_id": str(session_id),
            "message_id": str(message_id),
        }
    }
    assert public_message_id_from_parent_config(config) == message_id
    assert public_message_id_from_parent_config(parent_thread_config(session_id)) is None
