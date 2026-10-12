"""S3-11 Regenerate Capability 安全门禁单元测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.persistence_models import (
    CapabilityTask,
    CapabilityTaskContext,
)


SESSION_ID = UUID("00000000-0000-0000-0000-000000003110")
TASK_ID = UUID("00000000-0000-0000-0000-000000003111")


def _manifest(
    *,
    state_scope: str = "invocation",
    side_effect_policy: str = "none",
    state_schema_version: str = "2",
    compatible_versions: tuple[str, ...] = ("1",),
) -> CapabilityManifest:
    """构造覆盖门禁矩阵的严格 Manifest。"""

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "general_chat",
            "name": "通用对话",
            "description": "用于验证重新生成安全门禁。",
            "enabled": True,
            "entrypoint": "tests.fake_capability:create_capability",
            "version": "1.0.0",
            "state_scope": state_scope,
            "state_schema_version": state_schema_version,
            "compatible_state_schema_versions": list(compatible_versions),
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
            "recovery_policy": (
                "manual" if side_effect_policy == "unsafe" else "automatic"
            ),
            "side_effect_policy": side_effect_policy,
        }
    )


@pytest.mark.parametrize("state_scope", ["invocation", "run", "session"])
@pytest.mark.parametrize("side_effect_policy", ["none", "idempotent", "unsafe"])
def test_regenerate_policy_only_allows_invocation_without_side_effects(
    state_scope: str,
    side_effect_policy: str,
) -> None:
    """九种组合中只有 invocation + none 可以通过。"""

    from agent_runtime.capabilities.regeneration import (
        CapabilityRegeneratePolicy,
        CapabilityRegenerateUnsupportedError,
    )

    policy = CapabilityRegeneratePolicy()
    manifest = _manifest(
        state_scope=state_scope,
        side_effect_policy=side_effect_policy,
    )

    if state_scope == "invocation" and side_effect_policy == "none":
        policy.require_supported(manifest)
        return

    with pytest.raises(CapabilityRegenerateUnsupportedError) as captured:
        policy.require_supported(manifest)

    assert captured.value.code == "CAPABILITY_REGENERATE_UNSUPPORTED"
    assert captured.value.status_code == 409
    assert captured.value.retryable is False


class _Registry:
    def __init__(self, manifest: CapabilityManifest, *, serviceable: bool = True) -> None:
        self.manifest = manifest
        self.serviceable = serviceable
        self.calls: list[str] = []

    def get(self, capability_id: str):
        self.calls.append(f"registry:{capability_id}")
        if capability_id != self.manifest.capability_id:
            return None
        return SimpleNamespace(manifest=self.manifest)

    async def serviceable_router_projections(self):
        self.calls.append("health")
        if not self.serviceable:
            return ()
        return (SimpleNamespace(capability_id=self.manifest.capability_id),)


class _PermissionService:
    def __init__(self, calls: list[str], *, allowed: bool = True) -> None:
        self.calls = calls
        self.allowed = allowed

    async def require_allowed(self, *, user_id: str, capability_id: str) -> None:
        self.calls.append(f"permission:{user_id}:{capability_id}")
        if not self.allowed:
            from agent_runtime.capabilities.routing import (
                CapabilityPermissionDeniedError,
            )

            raise CapabilityPermissionDeniedError(
                code="CAPABILITY_PERMISSION_DENIED",
                message="当前用户未获授权使用该能力",
                retryable=False,
            )


class _ContextRepository:
    def __init__(
        self,
        context: CapabilityTaskContext | None,
        calls: list[str],
    ) -> None:
        self.context = context
        self.calls = calls

    async def get(self, *, session_id: UUID, capability_id: str):
        self.calls.append(f"context:{session_id}:{capability_id}")
        return self.context


class _TaskRepository:
    def __init__(self, task: CapabilityTask | None, calls: list[str]) -> None:
        self.task = task
        self.calls = calls

    async def get(self, task_id: UUID):
        self.calls.append(f"task:{task_id}")
        return self.task


def _active_task(*, state_schema_version: str = "1") -> CapabilityTask:
    now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    return CapabilityTask(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capability_id="general_chat",
        state_schema_version=state_schema_version,
        status="active",
        last_run_id=None,
        last_invocation_id=None,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )


def _context() -> CapabilityTaskContext:
    return CapabilityTaskContext(
        session_id=SESSION_ID,
        capability_id="general_chat",
        current_task_id=TASK_ID,
        updated_at=datetime(2026, 10, 11, 10, 0, tzinfo=UTC),
    )


def test_submission_validator_rechecks_health_permission_and_compatible_current_task() -> None:
    """提交校验按固定顺序复查，并返回可供执行阶段继续解析的事实。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator

    calls: list[str] = []
    registry = _Registry(_manifest())
    validator = RegenerateSubmissionValidator(
        registry=registry,
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(_context(), calls),
        task_repository=_TaskRepository(_active_task(), calls),
    )

    decision = asyncio.run(
        validator.validate(
            user_id="runtime-user",
            session_id=SESSION_ID,
            capability_id="general_chat",
        )
    )

    assert decision.capability_id == "general_chat"
    assert decision.task_action == "continue"
    assert decision.current_task_id == TASK_ID
    assert registry.calls == ["registry:general_chat", "health"]
    assert calls == [
        "permission:runtime-user:general_chat",
        f"context:{SESSION_ID}:general_chat",
        f"task:{TASK_ID}",
    ]


def test_submission_validator_allows_continue_to_new_degradation_without_current_task() -> None:
    """旧 Session 没有 current Task 时保留 continue，让执行事务降级为 new。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator

    calls: list[str] = []
    validator = RegenerateSubmissionValidator(
        registry=_Registry(_manifest()),
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(None, calls),
        task_repository=_TaskRepository(None, calls),
    )

    decision = asyncio.run(
        validator.validate(
            user_id="runtime-user",
            session_id=SESSION_ID,
            capability_id="general_chat",
        )
    )

    assert decision.task_action == "continue"
    assert decision.current_task_id is None
    assert not any(item.startswith("task:") for item in calls)


def test_submission_validator_rejects_unserviceable_capability_before_permission() -> None:
    """健康不可用时不继续读取权限或 Task。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator
    from agent_runtime.capabilities.routing import CapabilityUnavailableError

    calls: list[str] = []
    validator = RegenerateSubmissionValidator(
        registry=_Registry(_manifest(), serviceable=False),
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(None, calls),
        task_repository=_TaskRepository(None, calls),
    )

    with pytest.raises(CapabilityUnavailableError) as captured:
        asyncio.run(
            validator.validate(
                user_id="runtime-user",
                session_id=SESSION_ID,
                capability_id="general_chat",
            )
        )

    assert captured.value.code == "CAPABILITY_UNAVAILABLE"
    assert captured.value.retryable is True
    assert calls == []


def test_submission_validator_rejects_incompatible_current_task_without_mutation() -> None:
    """State Schema 不兼容时只拒绝，不把 continue 自动降级为 new。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator
    from agent_runtime.capabilities.state_scope import (
        CapabilityStateVersionIncompatibleError,
    )

    calls: list[str] = []
    task = _active_task(state_schema_version="legacy")
    validator = RegenerateSubmissionValidator(
        registry=_Registry(_manifest(compatible_versions=())),
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(_context(), calls),
        task_repository=_TaskRepository(task, calls),
    )

    with pytest.raises(CapabilityStateVersionIncompatibleError):
        asyncio.run(
            validator.validate(
                user_id="runtime-user",
                session_id=SESSION_ID,
                capability_id="general_chat",
            )
        )

    assert task.status == "active"
    assert task.state_schema_version == "legacy"


def test_submission_validator_rechecks_permission_on_every_new_request() -> None:
    """权限不缓存，首次允许后撤销会使下一次新提交立即拒绝。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator
    from agent_runtime.capabilities.routing import CapabilityPermissionDeniedError

    calls: list[str] = []
    permission = _PermissionService(calls)
    validator = RegenerateSubmissionValidator(
        registry=_Registry(_manifest()),
        permission_service=permission,
        task_context_repository=_ContextRepository(None, calls),
        task_repository=_TaskRepository(None, calls),
    )
    asyncio.run(
        validator.validate(
            user_id="runtime-user",
            session_id=SESSION_ID,
            capability_id="general_chat",
        )
    )
    permission.allowed = False

    with pytest.raises(CapabilityPermissionDeniedError) as captured:
        asyncio.run(
            validator.validate(
                user_id="runtime-user",
                session_id=SESSION_ID,
                capability_id="general_chat",
            )
        )

    assert captured.value.status_code == 403
    assert calls.count("permission:runtime-user:general_chat") == 2


def test_submission_validator_rechecks_health_on_every_new_request() -> None:
    """健康判定不固定为启动快照，状态变化会使下一次新提交立即拒绝。"""

    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator
    from agent_runtime.capabilities.routing import CapabilityUnavailableError

    calls: list[str] = []
    registry = _Registry(_manifest())
    validator = RegenerateSubmissionValidator(
        registry=registry,
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(None, calls),
        task_repository=_TaskRepository(None, calls),
    )
    asyncio.run(
        validator.validate(
            user_id="runtime-user",
            session_id=SESSION_ID,
            capability_id="general_chat",
        )
    )
    registry.serviceable = False

    with pytest.raises(CapabilityUnavailableError):
        asyncio.run(
            validator.validate(
                user_id="runtime-user",
                session_id=SESSION_ID,
                capability_id="general_chat",
            )
        )

    assert registry.calls.count("health") == 2
    assert calls.count("permission:runtime-user:general_chat") == 1


def test_unsupported_policy_stops_before_health_permission_and_task_reads() -> None:
    """不安全 Manifest 在最早策略门禁拒绝，不触碰动态或持久化依赖。"""

    from agent_runtime.capabilities.regeneration import (
        CapabilityRegenerateUnsupportedError,
        RegenerateSubmissionValidator,
    )

    calls: list[str] = []
    registry = _Registry(_manifest(state_scope="run"))
    validator = RegenerateSubmissionValidator(
        registry=registry,
        permission_service=_PermissionService(calls),
        task_context_repository=_ContextRepository(None, calls),
        task_repository=_TaskRepository(None, calls),
    )

    with pytest.raises(CapabilityRegenerateUnsupportedError):
        asyncio.run(
            validator.validate(
                user_id="runtime-user",
                session_id=SESSION_ID,
                capability_id="general_chat",
            )
        )

    assert registry.calls == ["registry:general_chat"]
    assert calls == []
