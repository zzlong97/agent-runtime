"""首批执行权配置与进程内令牌边界。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_runtime.core.config import Settings


def test_execution_transaction_budget_is_explicit_and_bounded():
    assert Settings(_env_file=None).run_transaction_timeout_seconds == 5.0
    for invalid in (0, -1, 5.01, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, run_transaction_timeout_seconds=invalid)


def test_execution_token_is_required_and_scoped():
    from agent_runtime.runtime.ownership import (
        RunExecutionToken,
        RunOwnershipLost,
        bind_execution_token,
        require_execution_token,
    )

    with pytest.raises(RunOwnershipLost):
        require_execution_token()
    token = RunExecutionToken(uuid4(), uuid4(), 1)
    with bind_execution_token(token):
        assert require_execution_token() is token
        with bind_execution_token(RunExecutionToken(uuid4(), uuid4(), 2)):
            assert require_execution_token() is not token
        assert require_execution_token() is token
    with pytest.raises(RunOwnershipLost):
        require_execution_token()
    for invalid in (0, -1, True):
        with pytest.raises(ValueError):
            RunExecutionToken(uuid4(), uuid4(), invalid)
