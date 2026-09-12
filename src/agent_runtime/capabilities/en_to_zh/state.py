"""英文到中文翻译 Child Graph 的私有状态。"""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class EnglishToChineseState(TypedDict):
    """保存翻译图的派生消息快照和最近一次译文。"""

    messages: Annotated[list[AnyMessage], add_messages]
    draft_translation: str
