"""重新生成资格校验与 Parent checkpoint fork。"""

from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from agent_runtime.core.errors import ApplicationError
from agent_runtime.graph.config import parent_thread_config
from agent_runtime.history import MessageHistoryAdapter


class RegenerationError(ApplicationError):
    """重新生成目标、历史或 checkpoint fork 不符合产品契约。"""


class CheckpointForker:
    """从回答执行前的 Parent checkpoint 创建活动分支。"""

    def __init__(
        self,
        *,
        history_adapter: MessageHistoryAdapter,
        parent_graph: Any,
    ) -> None:
        self._history_adapter = history_adapter
        self._parent_graph = parent_graph

    async def create_fork(
        self,
        *,
        session_id: UUID,
        message_id: UUID,
        response_message_id: UUID,
    ) -> RunnableConfig:
        """校验最新完成回答，并返回携带新回复 UUID 的 fork 配置。"""

        messages = await self._history_adapter.get_active_messages(
            session_id=session_id
        )
        if (
            not messages
            or messages[-1].message_id != message_id
            or messages[-1].role != "assistant"
            or messages[-1].runtime_status != "completed"
        ):
            raise RegenerationError(
                code="MESSAGE_REGENERATE_NOT_ALLOWED",
                message=(
                    "仅允许重新生成当前活动分支最新的 completed AIMessage"
                ),
                status_code=409,
            )

        answer_checkpoint: RunnableConfig | None = None
        try:
            async for snapshot in self._parent_graph.aget_state_history(
                parent_thread_config(session_id),
                filter={"message_id": str(message_id)},
            ):
                if snapshot.next == ("invoke_capability",):
                    answer_checkpoint = snapshot.config
                    break
        except Exception as error:
            raise RegenerationError(
                code="MESSAGE_REGENERATE_HISTORY_FAILED",
                message="读取重新生成所需的 Parent 历史失败",
                retryable=True,
            ) from error

        if answer_checkpoint is None:
            raise RegenerationError(
                code="MESSAGE_REGENERATE_CHECKPOINT_NOT_FOUND",
                message="未找到可用于重新生成的 Parent checkpoint",
                status_code=409,
            )

        try:
            fork_config = await self._parent_graph.aupdate_state(
                answer_checkpoint,
                {},
            )
        except Exception as error:
            raise RegenerationError(
                code="MESSAGE_REGENERATE_FORK_FAILED",
                message="创建消息重新生成分支失败",
                retryable=True,
            ) from error

        configurable = fork_config.get("configurable")
        if (
            not isinstance(configurable, dict)
            or configurable.get("thread_id") != str(session_id)
            or not configurable.get("checkpoint_id")
        ):
            raise RegenerationError(
                code="MESSAGE_REGENERATE_FORK_FAILED",
                message="创建消息重新生成分支失败",
                retryable=True,
            )
        return {
            **fork_config,
            "configurable": {
                **configurable,
                "message_id": str(response_message_id),
            },
        }
