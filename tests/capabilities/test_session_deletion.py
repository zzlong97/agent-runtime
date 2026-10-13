"""S3-13 Capability Session 数据删除单元测试。"""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import pytest


SESSION_ID = UUID("00000000-0000-0000-0000-000000003130")


class RecordingConnection:
    """记录删除顺序，并可在指定步骤注入一次失败。"""

    def __init__(self, *, fail_step: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_step = fail_step

    async def execute(self, query: str, parameters):
        assert parameters == (SESSION_ID,)
        normalized = " ".join(query.split())
        if normalized.startswith("DELETE FROM capability_task_contexts"):
            step = "contexts"
        elif normalized.startswith("DELETE FROM capability_operations"):
            step = "operations"
        elif normalized.startswith("DELETE FROM capability_tasks"):
            step = "tasks"
        else:  # pragma: no cover - 未知 SQL 应直接暴露测试失败
            raise AssertionError(f"未预期的删除 SQL: {normalized}")
        self.events.append(step)
        if self.fail_step == step:
            self.fail_step = None
            raise RuntimeError(f"{step} 删除暂不可用")

    async def commit(self) -> None:
        self.events.append("commit")


@pytest.mark.parametrize("fail_step", ["contexts", "operations", "tasks"])
def test_capability_data_deletion_retries_whole_transaction_after_each_failure(
    monkeypatch: pytest.MonkeyPatch,
    fail_step: str,
) -> None:
    """任一 Capability 删除步骤失败时不提交，重试从 Context 开始。"""

    from agent_runtime.capabilities import session_deletion as module
    from agent_runtime.capabilities.session_deletion import (
        SessionCapabilityDataDeleter,
        SessionCapabilityDeletionError,
    )
    from agent_runtime.core.config import Settings

    connection = RecordingConnection(fail_step=fail_step)

    @asynccontextmanager
    async def fake_connection(settings):
        yield connection

    monkeypatch.setattr(module, "open_database_connection", fake_connection)
    deleter = SessionCapabilityDataDeleter(Settings(_env_file=None))

    with pytest.raises(SessionCapabilityDeletionError) as captured:
        asyncio.run(deleter.delete_by_session(session_id=SESSION_ID))

    assert captured.value.code == "SESSION_CAPABILITY_DELETE_FAILED"
    assert captured.value.retryable is True
    assert "commit" not in connection.events

    connection.events.clear()
    asyncio.run(deleter.delete_by_session(session_id=SESSION_ID))

    assert connection.events == [
        "contexts",
        "operations",
        "tasks",
        "commit",
    ]
