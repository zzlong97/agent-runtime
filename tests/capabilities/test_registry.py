"""Stage 3 静态 Capability Registry 测试。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agent_runtime.capabilities.contracts import (
    AgentResult,
    CapabilityBootstrapContext,
    HealthResult,
)
from agent_runtime.capabilities.registry import CapabilityRegistry
from agent_runtime.capabilities.source import (
    CapabilityManifestDocument,
    CapabilitySource,
    ManifestDocumentError,
)

type Cleanup = Callable[[], Awaitable[None]]


class FakeSource(CapabilitySource):
    def __init__(self, documents: list[CapabilityManifestDocument]) -> None:
        self._documents = documents

    def load(self) -> list[CapabilityManifestDocument]:
        return list(self._documents)


class FakeCapability:
    def __init__(
        self,
        *,
        health: HealthResult | Exception | object | None = None,
        initialize_error: Exception | None = None,
    ) -> None:
        self.health = health or HealthResult(
            status="healthy",
            summary_code="READY",
        )
        self.initialize_error = initialize_error
        self.initialize_calls = 0
        self.health_calls = 0

    async def initialize(self) -> None:
        self.initialize_calls += 1
        if self.initialize_error is not None:
            raise self.initialize_error

    async def health_check(self) -> HealthResult:
        self.health_calls += 1
        if isinstance(self.health, Exception):
            raise self.health
        return self.health  # type: ignore[return-value]

    async def invoke(self, run_context, agent_context, task_input) -> AgentResult:
        raise AssertionError("S3-03 不应执行 Capability")


def _raw_manifest(
    capability_id: str,
    *,
    entrypoint: str,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "manifest_schema_version": 1,
        "capability_id": capability_id,
        "name": f"{capability_id} 名称",
        "description": f"{capability_id} 的测试说明。",
        "enabled": enabled,
        "entrypoint": entrypoint,
        "version": "1.0.0",
        "state_scope": "invocation",
        "state_schema_version": "1",
        "compatible_state_schema_versions": [],
        "allow_degraded": False,
        "concurrency": {
            "mode": "unlimited",
            "max_concurrency": None,
            "acquire_timeout_seconds": 1.0,
        },
        "execution": {
            "timeout_seconds": 30.0,
            "cancel_grace_seconds": 1.0,
        },
        "recovery_policy": "automatic",
        "side_effect_policy": "none",
    }


def _document(
    source: str,
    raw_document: Mapping[Any, Any] | None,
    *,
    error: ManifestDocumentError | None = None,
) -> CapabilityManifestDocument:
    return CapabilityManifestDocument(
        source=Path(source),
        raw_document=raw_document,
        error=error,
    )


def _install_factory_module(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    factory: object,
) -> str:
    module = ModuleType(name)
    module.create_capability = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)
    return f"{name}:create_capability"


async def _build_registry(
    documents: list[CapabilityManifestDocument],
    *,
    capability_configs: Mapping[str, Mapping[str, object]] | None = None,
) -> CapabilityRegistry:
    return await CapabilityRegistry.build(
        FakeSource(documents),
        child_checkpointer_factory=lambda manifest: object(),
        model_factory=object(),
        capability_configs=capability_configs,
    )


def test_registry_builds_active_entry_and_minimal_router_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[CapabilityBootstrapContext] = []

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        captured.append(bootstrap_context)
        return FakeCapability()

    entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_registry_active",
        create_capability,
    )
    registry = asyncio.run(
        _build_registry(
            [_document("b/manifest.yaml", _raw_manifest("active_cap", entrypoint=entrypoint))],
            capability_configs={"active_cap": {"region": "test"}},
        )
    )

    entry = registry.get("active_cap")
    assert entry is not None
    assert entry.status == "active"
    assert entry.health == HealthResult(status="healthy", summary_code="READY")
    assert entry.capability is not None
    assert captured[0].manifest.capability_id == "active_cap"
    assert captured[0].capability_config["region"] == "test"
    assert not hasattr(captured[0], "redis")
    assert not hasattr(captured[0], "session_repository")
    assert not hasattr(captured[0], "run_repository")

    [projection] = registry.router_projections()
    assert projection.model_dump() == {
        "capability_id": "active_cap",
        "name": "active_cap 名称",
        "description": "active_cap 的测试说明。",
        "enabled": True,
    }
    asyncio.run(registry.close())


def test_registry_isolates_invalid_duplicates_disabled_and_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls: list[str] = []

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        factory_calls.append(bootstrap_context.manifest.capability_id)
        return FakeCapability()

    valid_entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_registry_isolation",
        create_capability,
    )
    documents = [
        _document(
            "z/manifest.yaml",
            _raw_manifest("valid_cap", entrypoint=valid_entrypoint),
        ),
        _document(
            "invalid_source/manifest.yaml",
            None,
            error=ManifestDocumentError(
                code="YAML_SYNTAX_ERROR",
                message="语法错误",
            ),
        ),
        _document("duplicate_b/manifest.yaml", _raw_manifest("duplicate", entrypoint=valid_entrypoint)),
        _document("duplicate_a/manifest.yaml", _raw_manifest("duplicate", entrypoint=valid_entrypoint)),
        _document("disabled/manifest.yaml", _raw_manifest("disabled", entrypoint="missing.disabled:create_capability", enabled=False)),
        _document("broken/manifest.yaml", _raw_manifest("broken", entrypoint="missing.module:create_capability")),
        _document("invalid_schema/manifest.yaml", {"capability_id": "bad"}),
    ]

    registry = asyncio.run(_build_registry(documents))

    assert [(entry.source.as_posix(), entry.status) for entry in registry.entries] == [
        ("broken/manifest.yaml", "entrypoint_failed"),
        ("disabled/manifest.yaml", "disabled"),
        ("duplicate_a/manifest.yaml", "duplicate_id"),
        ("duplicate_b/manifest.yaml", "duplicate_id"),
        ("invalid_schema/manifest.yaml", "invalid_manifest"),
        ("invalid_source/manifest.yaml", "invalid_manifest"),
        ("z/manifest.yaml", "active"),
    ]
    assert factory_calls == ["valid_cap"]
    assert registry.get("duplicate") is None
    assert registry.get("disabled") is None
    assert [item.capability_id for item in registry.router_projections()] == [
        "valid_cap"
    ]
    asyncio.run(registry.close())


def test_invalid_manifest_with_same_declared_id_isolates_entire_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls: list[str] = []

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        factory_calls.append(bootstrap_context.manifest.capability_id)
        return FakeCapability()

    entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_registry_invalid_duplicate",
        create_capability,
    )
    valid = _raw_manifest("same_id", entrypoint=entrypoint)
    invalid = {
        **_raw_manifest("same_id", entrypoint=entrypoint),
        "unknown_field": "必须导致严格 Schema 校验失败",
    }

    registry = asyncio.run(
        _build_registry(
            [
                _document("a_valid/manifest.yaml", valid),
                _document("b_invalid/manifest.yaml", invalid),
            ]
        )
    )

    assert [entry.status for entry in registry.entries] == [
        "duplicate_id",
        "duplicate_id",
    ]
    assert factory_calls == []
    assert registry.get("same_id") is None
    assert registry.router_projections() == ()
    asyncio.run(registry.close())


def test_invalid_manifest_normalizes_declared_id_before_duplicate_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls: list[str] = []

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        factory_calls.append(bootstrap_context.manifest.capability_id)
        return FakeCapability()

    entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_registry_normalized_duplicate",
        create_capability,
    )
    valid = _raw_manifest("same_id", entrypoint=entrypoint)
    invalid = {
        **_raw_manifest("same_id", entrypoint=entrypoint),
        "capability_id": "  same_id  ",
        "unknown_field": "归一化 ID 后仍必须参与重复冲突收集",
    }

    registry = asyncio.run(
        _build_registry(
            [
                _document("a_valid/manifest.yaml", valid),
                _document("b_invalid/manifest.yaml", invalid),
            ]
        )
    )

    assert [entry.status for entry in registry.entries] == [
        "duplicate_id",
        "duplicate_id",
    ]
    assert factory_calls == []
    assert registry.get("same_id") is None
    assert registry.router_projections() == ()
    asyncio.run(registry.close())


def test_invalid_manifest_id_rejected_by_schema_does_not_create_false_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls: list[str] = []

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        factory_calls.append(bootstrap_context.manifest.capability_id)
        return FakeCapability()

    entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_registry_control_separator_id",
        create_capability,
    )
    valid = _raw_manifest("same_id", entrypoint=entrypoint)
    invalid = {
        **_raw_manifest("same_id", entrypoint=entrypoint),
        "capability_id": "\u001csame_id\u001c",
        "unknown_field": "正式 Schema 不把该控制分隔符归一化为空白",
    }

    registry = asyncio.run(
        _build_registry(
            [
                _document("a_valid/manifest.yaml", valid),
                _document("b_invalid/manifest.yaml", invalid),
            ]
        )
    )

    assert [entry.status for entry in registry.entries] == [
        "active",
        "invalid_manifest",
    ]
    assert factory_calls == ["same_id"]
    assert registry.get("same_id") is not None
    assert [item.capability_id for item in registry.router_projections()] == [
        "same_id"
    ]
    asyncio.run(registry.close())


def test_registry_isolates_factory_protocol_initialize_and_health_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_signature() -> FakeCapability:
        return FakeCapability()

    def wrong_protocol(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> object:
        return object()

    def initialize_failed(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        return FakeCapability(initialize_error=RuntimeError("初始化失败"))

    def health_failed(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        return FakeCapability(health=RuntimeError("健康检查失败"))

    def healthy(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        return FakeCapability()

    factories = {
        "a_bad_signature": wrong_signature,
        "b_bad_protocol": wrong_protocol,
        "c_init_failed": initialize_failed,
        "d_health_failed": health_failed,
        "e_healthy": healthy,
    }
    documents = []
    for capability_id, factory in factories.items():
        entrypoint = _install_factory_module(
            monkeypatch,
            f"tests.fake_registry_{capability_id}",
            factory,
        )
        documents.append(
            _document(
                f"{capability_id}/manifest.yaml",
                _raw_manifest(capability_id, entrypoint=entrypoint),
            )
        )

    registry = asyncio.run(_build_registry(documents))

    assert [entry.status for entry in registry.entries] == [
        "entrypoint_failed",
        "entrypoint_failed",
        "initialize_failed",
        "active",
        "active",
    ]
    assert registry.entries[3].health == HealthResult(
        status="unhealthy",
        summary_code="HEALTH_CHECK_FAILED",
    )
    assert [item.capability_id for item in registry.router_projections()] == [
        "e_healthy"
    ]
    asyncio.run(registry.close())


def test_registry_rejects_capability_with_wrong_async_method_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongInvokeSignatureCapability:
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> HealthResult:
            return HealthResult(status="healthy", summary_code="READY")

        async def invoke(self) -> AgentResult:
            raise AssertionError("非法签名不应被调用")

    def create_capability(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> WrongInvokeSignatureCapability:
        return WrongInvokeSignatureCapability()

    entrypoint = _install_factory_module(
        monkeypatch,
        "tests.fake_wrong_invoke_signature",
        create_capability,
    )

    registry = asyncio.run(
        _build_registry(
            [
                _document(
                    "wrong/manifest.yaml",
                    _raw_manifest("wrong_signature", entrypoint=entrypoint),
                )
            ]
        )
    )

    assert registry.entries[0].status == "entrypoint_failed"
    assert registry.get("wrong_signature") is None
    assert registry.router_projections() == ()
    asyncio.run(registry.close())


def test_cleanup_is_reverse_order_failure_isolated_and_close_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_events: list[str] = []

    def factory_for(
        capability_id: str,
        *,
        initialize_error: Exception | None = None,
    ):
        def create_capability(
            bootstrap_context: CapabilityBootstrapContext,
        ) -> FakeCapability:
            async def cleanup_first() -> None:
                cleanup_events.append(f"{capability_id}:first")

            async def cleanup_second() -> None:
                cleanup_events.append(f"{capability_id}:second")
                if capability_id == "b_active":
                    raise RuntimeError("清理失败")

            bootstrap_context.register_cleanup(cleanup_first)
            bootstrap_context.register_cleanup(cleanup_second)
            return FakeCapability(initialize_error=initialize_error)

        return create_capability

    documents = []
    for capability_id, initialize_error in (
        ("a_active", None),
        ("b_active", None),
        ("c_failed", RuntimeError("初始化失败")),
    ):
        entrypoint = _install_factory_module(
            monkeypatch,
            f"tests.fake_cleanup_{capability_id}",
            factory_for(capability_id, initialize_error=initialize_error),
        )
        documents.append(
            _document(
                f"{capability_id}/manifest.yaml",
                _raw_manifest(capability_id, entrypoint=entrypoint),
            )
        )

    registry = asyncio.run(_build_registry(documents))
    assert cleanup_events == ["c_failed:second", "c_failed:first"]

    asyncio.run(registry.close())
    asyncio.run(registry.close())

    assert cleanup_events == [
        "c_failed:second",
        "c_failed:first",
        "b_active:second",
        "b_active:first",
        "a_active:second",
        "a_active:first",
    ]


def test_build_cancellation_cleans_current_and_previous_capabilities_in_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_events: list[str] = []

    def factory_for(capability_id: str, *, cancel_initialize: bool):
        def create_capability(
            bootstrap_context: CapabilityBootstrapContext,
        ) -> FakeCapability:
            async def cleanup() -> None:
                cleanup_events.append(capability_id)

            bootstrap_context.register_cleanup(cleanup)
            error = asyncio.CancelledError() if cancel_initialize else None
            return FakeCapability(initialize_error=error)

        return create_capability

    documents = []
    for capability_id, cancel_initialize in (
        ("a_active", False),
        ("b_cancelled", True),
    ):
        entrypoint = _install_factory_module(
            monkeypatch,
            f"tests.fake_build_cancel_{capability_id}",
            factory_for(capability_id, cancel_initialize=cancel_initialize),
        )
        documents.append(
            _document(
                f"{capability_id}/manifest.yaml",
                _raw_manifest(capability_id, entrypoint=entrypoint),
            )
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_build_registry(documents))

    assert cleanup_events == ["b_cancelled", "a_active"]


def test_build_cancellation_retries_current_cancelled_cleanup_before_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_events: list[str] = []
    current_cleanup_attempts = 0

    def active_factory(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        async def cleanup() -> None:
            cleanup_events.append("a_active")

        bootstrap_context.register_cleanup(cleanup)
        return FakeCapability()

    def cancelled_factory(
        bootstrap_context: CapabilityBootstrapContext,
    ) -> FakeCapability:
        async def cleanup() -> None:
            nonlocal current_cleanup_attempts
            current_cleanup_attempts += 1
            cleanup_events.append(f"b_cancelled:{current_cleanup_attempts}")
            if current_cleanup_attempts == 1:
                raise asyncio.CancelledError

        bootstrap_context.register_cleanup(cleanup)
        return FakeCapability(initialize_error=asyncio.CancelledError())

    documents = []
    for capability_id, factory in (
        ("a_active", active_factory),
        ("b_cancelled", cancelled_factory),
    ):
        entrypoint = _install_factory_module(
            monkeypatch,
            f"tests.fake_nested_build_cancel_{capability_id}",
            factory,
        )
        documents.append(
            _document(
                f"{capability_id}/manifest.yaml",
                _raw_manifest(capability_id, entrypoint=entrypoint),
            )
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_build_registry(documents))

    assert cleanup_events == [
        "b_cancelled:1",
        "b_cancelled:2",
        "a_active",
    ]


def test_close_cancellation_finishes_other_cleanup_and_can_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_events: list[str] = []
    cancel_attempts = 0

    def factory_for(capability_id: str):
        def create_capability(
            bootstrap_context: CapabilityBootstrapContext,
        ) -> FakeCapability:
            async def cleanup_first() -> None:
                cleanup_events.append(f"{capability_id}:first")

            bootstrap_context.register_cleanup(cleanup_first)
            if capability_id == "b_active":

                async def cleanup_cancel_once() -> None:
                    nonlocal cancel_attempts
                    cancel_attempts += 1
                    cleanup_events.append(
                        f"{capability_id}:cancel:{cancel_attempts}"
                    )
                    if cancel_attempts == 1:
                        raise asyncio.CancelledError

                bootstrap_context.register_cleanup(cleanup_cancel_once)
            return FakeCapability()

        return create_capability

    documents = []
    for capability_id in ("a_active", "b_active"):
        entrypoint = _install_factory_module(
            monkeypatch,
            f"tests.fake_close_cancel_{capability_id}",
            factory_for(capability_id),
        )
        documents.append(
            _document(
                f"{capability_id}/manifest.yaml",
                _raw_manifest(capability_id, entrypoint=entrypoint),
            )
        )
    registry = asyncio.run(_build_registry(documents))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(registry.close())
    assert cleanup_events == [
        "b_active:cancel:1",
        "b_active:first",
        "a_active:first",
    ]

    asyncio.run(registry.close())
    asyncio.run(registry.close())
    assert cleanup_events == [
        "b_active:cancel:1",
        "b_active:first",
        "a_active:first",
        "b_active:cancel:2",
    ]
