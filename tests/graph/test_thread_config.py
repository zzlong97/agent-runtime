from uuid import uuid4


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
