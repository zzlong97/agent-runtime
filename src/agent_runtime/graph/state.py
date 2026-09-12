"""State shared by the Stage 1 Parent Graph."""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ParentState(TypedDict):
    """Public conversation state plus Parent-only routing control fields."""

    messages: Annotated[list[AnyMessage], add_messages]
    resolved_capability_id: str | None
    rejected_capability_ids: list[str]
