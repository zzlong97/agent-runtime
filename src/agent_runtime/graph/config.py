"""Thread configuration for Parent and Child graphs."""

from uuid import UUID

from langchain_core.runnables import RunnableConfig


def parent_thread_config(session_id: UUID) -> RunnableConfig:
    """Use the Session identifier as the Parent checkpoint thread."""

    return {"configurable": {"thread_id": str(session_id)}}


def child_thread_config(
    session_id: UUID,
    capability_id: str,
) -> RunnableConfig:
    """Isolate a Child checkpoint thread within its Session and capability."""

    return {
        "configurable": {
            "thread_id": f"{session_id}:{capability_id}",
        }
    }
