import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import pytest


@pytest.mark.parametrize(
    ("method_name", "identifier"),
    [
        ("get", UUID("00000000-0000-0000-0000-000000002501")),
        (
            "get_by_request_id",
            UUID("00000000-0000-0000-0000-000000002502"),
        ),
    ],
)
def test_run_repository_logs_read_failure_without_database_details(
    caplog,
    method_name,
    identifier,
    monkeypatch,
) -> None:
    import agent_runtime.runtime.repository as repository_module
    from agent_runtime.core.config import Settings
    from agent_runtime.runtime.repository import (
        PostgresRunRepository,
        RunPersistenceError,
    )

    @asynccontextmanager
    async def failing_connection(settings):
        raise RuntimeError("不得进入业务日志的数据库详情")
        yield

    monkeypatch.setattr(
        repository_module,
        "open_database_connection",
        failing_connection,
    )
    repository = PostgresRunRepository(Settings(_env_file=None))

    async def exercise() -> None:
        method = getattr(repository, method_name)
        with pytest.raises(RunPersistenceError) as failure:
            await method(identifier)
        assert failure.value.code == "RUN_READ_FAILED"

    with caplog.at_level(logging.INFO, logger="agent_runtime.runtime.repository"):
        asyncio.run(exercise())

    read_failure_log = next(
        record.getMessage()
        for record in caplog.records
        if "Run持久化读取失败" in record.getMessage()
    )
    assert '"error_code": "RUN_READ_FAILED"' in read_failure_log
    assert '"error_type": "RuntimeError"' in read_failure_log
    assert '"duration_ms":' in read_failure_log
    assert "不得进入业务日志的数据库详情" not in read_failure_log
