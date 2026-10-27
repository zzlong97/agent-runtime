"""聊天组装仅替换外部存储边界，验证远程安全配置与连接池所有权。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("startup_failure", [False, True])
def test_chat_bootstrap_injects_remote_settings_and_closes_http_on_all_exits(
    monkeypatch, startup_failure
):
    from agent_runtime import chat
    from agent_runtime.capabilities.manifest import CapabilityManifest
    from agent_runtime.core.config import Settings
    from tests.capabilities.test_manifest import _valid_manifest
    from tests.capabilities.test_builtin_capability_entrypoints import (
        _BuiltinCapabilityFakeModel,
    )
    from tests.fake_a2a import remote_transport

    captured = {}

    class Resource:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

        async def setup(self):
            pass

        async def start(self, *args):
            pass

        async def close(self):
            pass

        async def aclose(self):
            pass

        async def execute_persistent_run(self, *args):
            raise AssertionError("本测试不执行 Run")

    for name in (
        "PostgresSessionRepository",
        "PostgresFeedbackStore",
        "PostgresRunRepository",
        "PostgresRuntimeEventRepository",
        "CapabilityTaskRepository",
        "CapabilityTaskContextRepository",
        "CapabilityOperationRepository",
        "UserCapabilityPermissionRepository",
        "SessionCapabilityDataDeleter",
        "DynamicChildThreadCollector",
        "PostgresRunInterruptRepository",
        "RedisStreamPublisher",
        "RedisStreamReader",
        "RuntimeEventGateway",
        "RunCoordinator",
        "PostgresParentStateStore",
        "SessionService",
        "MessageHistoryAdapter",
        "FeedbackService",
        "SessionDeletionService",
        "ChatService",
    ):
        monkeypatch.setattr(chat, name, Resource)

    @asynccontextmanager
    async def checkpointers(*args, **kwargs):
        yield SimpleNamespace(parent=None, cleanup=None, children=())

    async def registry_build(source, **kwargs):
        raw = _valid_manifest()
        raw["transport"] = remote_transport()
        capability = kwargs["remote_capability_factory"](
            CapabilityManifest.model_validate(raw)
        )
        captured["client"] = capability.client
        assert capability.client._http.timeout.read == 0.75
        return SimpleNamespace(close=capability.close)

    def graph(**kwargs):
        if startup_failure:
            raise RuntimeError("模拟 Registry 构造后的组装失败")
        return object()

    monkeypatch.setattr(chat, "open_runtime_checkpointers", checkpointers)
    monkeypatch.setattr(
        chat, "CapabilityRegistry", SimpleNamespace(build=registry_build)
    )
    monkeypatch.setattr(chat, "build_parent_graph", graph)

    async def run():
        async with chat.open_chat_service(
            Settings(
                _env_file=None,
                remote_a2a_development_mode=True,
                remote_a2a_http_timeout_seconds=0.75,
            ),
            model=_BuiltinCapabilityFakeModel(),
        ):
            assert not captured["client"]._http.is_closed

    if startup_failure:
        with pytest.raises(RuntimeError, match="组装失败"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert captured["client"]._http.is_closed
