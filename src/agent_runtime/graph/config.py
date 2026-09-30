"""Parent 与 Child 图的 Stage 1 thread 配置。"""

from uuid import UUID

from langchain_core.runnables import RunnableConfig

from agent_runtime.core.errors import ApplicationError


class ThreadConfigError(ApplicationError):
    """图调用配置中缺少合法的 Session thread_id。"""


def parent_thread_config(
    session_id: UUID,
    *,
    message_id: UUID | None = None,
    run_id: UUID | None = None,
    response_message_id: UUID | None = None,
) -> RunnableConfig:
    """构造 Parent thread_id，并可关联持久 Run 的 Checkpoint metadata。"""

    configurable = {"thread_id": str(session_id)}
    if message_id is not None:
        configurable["message_id"] = str(message_id)
    config: RunnableConfig = {"configurable": configurable}
    if run_id is not None and response_message_id is not None:
        config["metadata"] = {
            "run_id": str(run_id),
            "response_message_id": str(response_message_id),
        }
    return config


def child_thread_config(
    session_id: UUID,
    capability_id: str,
) -> RunnableConfig:
    """按 Session 和能力标识隔离 Child checkpoint thread_id。"""

    return {
        "configurable": {
            "thread_id": f"{session_id}:{capability_id}",
        }
    }


def session_id_from_parent_config(config: RunnableConfig) -> UUID:
    """从 Parent RunnableConfig 的 thread_id 恢复 Session UUID。"""

    try:
        configurable = config["configurable"]
        return UUID(str(configurable["thread_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ThreadConfigError(
            code="PARENT_THREAD_ID_INVALID",
            message="Parent 调用配置中缺少有效的 Session thread_id",
        ) from error


def public_message_id_from_parent_config(
    config: RunnableConfig,
) -> UUID | None:
    """读取本轮公共 AIMessage UUID；非 HTTP 调用未提供时返回 null。"""

    configurable = config.get("configurable", {})
    raw_message_id = configurable.get("message_id")
    if raw_message_id is None:
        return None
    try:
        return UUID(str(raw_message_id))
    except (TypeError, ValueError) as error:
        raise ThreadConfigError(
            code="PUBLIC_MESSAGE_ID_INVALID",
            message="Parent 调用配置中的公共 message_id 不是有效 UUID",
        ) from error
