"""与默认验收隔离、仅显式启用的百炼真实模型 smoke test。"""

import os

import pytest
from langchain_core.messages import HumanMessage


@pytest.mark.bailian
@pytest.mark.skipif(
    os.getenv("RUN_BAILIAN_SMOKE") != "1",
    reason="设置 RUN_BAILIAN_SMOKE=1 后运行真实百炼 smoke test",
)
def test_bailian_model_returns_non_empty_response() -> None:
    """显式启用时验证 `.env` 配置可以完成一次真实模型调用。"""

    from agent_runtime.core.config import Settings
    from agent_runtime.core.model import build_chat_model

    response = build_chat_model(Settings()).invoke(
        [HumanMessage(content="请只回复：连接成功")]
    )

    assert str(response.text).strip()
