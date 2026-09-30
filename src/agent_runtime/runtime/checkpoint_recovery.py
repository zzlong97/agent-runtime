"""持久 Run 的精确 Parent Checkpoint 查询与恢复配置构造。"""

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import StateSnapshot

from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.config import parent_thread_config
from agent_runtime.runtime.models import Run

logger = logging.getLogger(__name__)

_AUTOMATIC_RECOVERY_CAPABILITIES = frozenset({"general_chat", "en_to_zh"})
RecoveredRuntimeStatus = Literal[
    "completed", "unsupported", "incomplete", "stopped"
]
RecoveredCapabilityId = Literal["general_chat", "en_to_zh"]


class RunCheckpointError(ApplicationError):
    """Run Checkpoint 缺少稳定关联或无法安全用于恢复。"""


@dataclass(frozen=True, slots=True)
class RecoveredFinalMessage:
    """Checkpoint 中已持久化、可直接补投影的稳定公共终态消息。"""

    runtime_status: RecoveredRuntimeStatus
    capability_id: RecoveredCapabilityId | None


class RunCheckpointInspector:
    """只按 Run metadata 定位精确快照，禁止使用模糊的 Session 最新状态。"""

    def __init__(self, *, parent_graph: Any) -> None:
        self._parent_graph = parent_graph

    async def latest_for_run(self, run: Run) -> StateSnapshot | None:
        """返回目标 Run 自己的最新 Checkpoint；尚未生成时返回空。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Run精确Checkpoint查询开始",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
        )
        async for snapshot in self._parent_graph.aget_state_history(
            parent_thread_config(run.session_id),
            filter={"run_id": str(run.run_id)},
            limit=1,
        ):
            metadata = snapshot.metadata or {}
            if metadata.get("response_message_id") != str(
                run.response_message_id
            ):
                raise RunCheckpointError(
                    code="RUN_CHECKPOINT_METADATA_INVALID",
                    message="Run Checkpoint 的稳定回复标识不匹配",
                    status_code=409,
                )
            log_business_event(
                logger,
                "Run精确Checkpoint查询完成",
                run_id=run.run_id,
                session_id=run.session_id,
                message_id=run.response_message_id,
                checkpoint_found=True,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            return snapshot
        log_business_event(
            logger,
            "Run精确Checkpoint查询完成",
            run_id=run.run_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            checkpoint_found=False,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return None

    @staticmethod
    def recovery_config(
        run: Run,
        snapshot: StateSnapshot,
    ) -> RunnableConfig:
        """从精确快照构造继续执行配置，并保持稳定 Run metadata。"""

        configurable = snapshot.config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is None:
            raise RunCheckpointError(
                code="RUN_CHECKPOINT_METADATA_INVALID",
                message="Run Checkpoint 缺少 checkpoint_id",
                status_code=409,
            )
        config = parent_thread_config(
            run.session_id,
            message_id=run.response_message_id,
            run_id=run.run_id,
            response_message_id=run.response_message_id,
        )
        config["configurable"] = {
            **config["configurable"],
            "checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
            "checkpoint_id": str(checkpoint_id),
        }
        return config


def recovered_final_message(
    run: Run,
    snapshot: StateSnapshot | Any,
) -> RecoveredFinalMessage | None:
    """识别目标 Run 已落盘的非空稳定终态消息，未完成时返回空。"""

    final_message = next(
        (
            message
            for message in reversed(snapshot.values.get("messages", []))
            if isinstance(message, AIMessage)
            and str(message.id) == str(run.response_message_id)
        ),
        None,
    )
    if final_message is None or not str(final_message.text).strip():
        return None
    raw_status = final_message.additional_kwargs.get("runtime_status")
    if raw_status not in {
        "completed",
        "unsupported",
        "incomplete",
        "stopped",
    }:
        return None
    raw_capability = final_message.additional_kwargs.get("capability_id")
    if raw_capability not in {None, "general_chat", "en_to_zh"}:
        raise RunCheckpointError(
            code="RUN_CHECKPOINT_MESSAGE_INVALID",
            message="Run Checkpoint 的公共消息能力标识不合法",
            status_code=409,
        )
    return RecoveredFinalMessage(
        runtime_status=cast(RecoveredRuntimeStatus, raw_status),
        capability_id=cast(RecoveredCapabilityId | None, raw_capability),
    )


def ensure_automatic_recovery_safe(snapshot: StateSnapshot | Any) -> None:
    """拒绝自动恢复当前阶段未声明幂等保障的副作用能力。"""

    capability_id = snapshot.values.get("resolved_capability_id")
    if capability_id is None or capability_id in _AUTOMATIC_RECOVERY_CAPABILITIES:
        return
    raise RunCheckpointError(
        code="RUN_RECOVERY_UNSAFE_SIDE_EFFECT",
        message="当前能力缺少副作用幂等保障，不能自动恢复",
        status_code=409,
    )
