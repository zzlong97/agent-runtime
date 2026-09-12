"""Stage 1 Parent Graph 的公共状态和控制状态。"""

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ParentState(TypedDict):
    """保存公共对话以及只属于 Parent 的调度字段。"""

    messages: Annotated[list[AnyMessage], add_messages]
    resolved_capability_id: str | None
    rejected_capability_ids: list[str]
    completion_status: NotRequired[Literal["completed", "unsupported"] | None]
