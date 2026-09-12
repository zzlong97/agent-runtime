from typing import get_type_hints

from langgraph.graph.message import add_messages


def test_parent_state_contains_only_public_and_control_fields() -> None:
    from agent_runtime.graph.state import ParentState

    fields = get_type_hints(ParentState, include_extras=True)

    assert set(fields) == {
        "messages",
        "resolved_capability_id",
        "rejected_capability_ids",
    }
    assert fields["messages"].__metadata__ == (add_messages,)
