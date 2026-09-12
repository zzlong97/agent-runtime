"""Parent 与 Child 图的 Stage 1 thread 配置。"""

from uuid import UUID

from langchain_core.runnables import RunnableConfig

from agent_runtime.core.errors import ApplicationError


class ThreadConfigError(ApplicationError):
    """图调用配置中缺少合法的 Session thread_id。"""


def parent_thread_config(session_id: UUID) -> RunnableConfig:
    """使用 Session 标识作为 Parent checkpoint thread_id。"""

    return {"configurable": {"thread_id": str(session_id)}}


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
