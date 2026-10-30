"""九张业务表注释的事务边界、失败传播与日志验证。"""

import asyncio
import json
import inspect
import logging
import re
from contextlib import asynccontextmanager

import pytest

from agent_runtime.core.config import Settings
from tests.persistence.table_comments_support import EXPECTED_COMMENTS, REPOSITORIES


@pytest.mark.parametrize("module,factory,table", REPOSITORIES)
@pytest.mark.parametrize("failure", [None, "comment", "column", "commit"])
def test_setup_comments_commit_and_failures(module, factory, table, failure, monkeypatch, caplog):
    calls = []
    sensitive = "不得进入业务日志的数据库连接凭据"

    class FakeConnection:
        async def execute(self, statement):
            calls.append(statement.strip())
            if failure == "comment" and statement.strip().startswith("COMMENT ON TABLE"):
                raise RuntimeError(sensitive)
            if failure == "column" and statement.strip().startswith("COMMENT ON COLUMN"):
                raise RuntimeError(sensitive)

        async def commit(self):
            calls.append("COMMIT")
            if failure == "commit":
                raise RuntimeError(sensitive)

    @asynccontextmanager
    async def connect(settings):
        yield FakeConnection()

    monkeypatch.setattr(module, "open_database_connection", connect)
    if table == "runs":
        async def setup_interrupt(self):
            calls.append("INTERRUPT_SETUP")
        monkeypatch.setattr(module.PostgresRunInterruptRepository, "setup", setup_interrupt)

    with caplog.at_level(logging.INFO, logger=module.__name__):
        if failure:
            with pytest.raises(Exception) as captured:
                asyncio.run(factory(Settings(_env_file=None)).setup())
            if table in {"sessions", "message_feedback"}:
                assert type(captured.value) is RuntimeError
            else:
                assert type(captured.value.__cause__) is RuntimeError
        else:
            asyncio.run(factory(Settings(_env_file=None)).setup())

    comment = f"COMMENT ON TABLE {table} IS '{EXPECTED_COMMENTS[table]}'"
    assert comment in calls
    assert calls.index(comment) > 0
    if failure in {"comment", "column"}:
        assert "COMMIT" not in calls
    else:
        assert calls.index(comment) < calls.index("COMMIT")
    logs = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records if record.name == module.__name__]
    assert sensitive not in caplog.text
    assert any(log["event"].endswith("初始化开始") for log in logs)
    assert any(log["event"].endswith("初始化完成") for log in logs) == (failure is None)
    if failure:
        failed = next(log for log in logs if log["event"].endswith("初始化失败"))
        assert failed["error_type"] == "RuntimeError"
        assert "duration_ms" in failed
        if table in {"sessions", "message_feedback"}:
            assert failed["error_code"] == {
                "sessions": "SESSION_PERSISTENCE_SETUP_FAILED",
                "message_feedback": "MESSAGE_FEEDBACK_SETUP_FAILED",
            }[table]


def test_expected_comments_match_approved_requirement():
    from pathlib import Path
    requirement = Path(".dev-team/tasks/DB-001/requirements.md").read_text(encoding="utf-8")
    for table, comment in EXPECTED_COMMENTS.items():
        assert f"| `{table}` |" in requirement
        assert f"| {comment} |" in requirement


def test_comment_ddl_is_limited_to_nine_business_tables():
    statements = []
    for module in {module for module, _, _ in REPOSITORIES}:
        statements.extend(re.findall(r"COMMENT ON TABLE (\w+)", inspect.getsource(module)))
    assert len(statements) == 9
    assert set(statements) == set(EXPECTED_COMMENTS)
