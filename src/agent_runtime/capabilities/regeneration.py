"""S3 Regenerate Capability 策略与提交前安全校验。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol
from uuid import UUID

from agent_runtime.capabilities.manifest import (
    CapabilityManifest,
    validate_capability_id,
)
from agent_runtime.capabilities.persistence_models import (
    CapabilityTask,
    CapabilityTaskAction,
    CapabilityTaskContext,
)
from agent_runtime.capabilities.routing import (
    CapabilityPermissionDeniedError,
    CapabilityPermissionService,
    CapabilityUnavailableError,
)
from agent_runtime.capabilities.state_scope import StateCompatibilityPolicy
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)


class CapabilityRegenerateUnsupportedError(ApplicationError):
    """目标 Capability 的 State 或副作用策略不允许重新生成。"""


class _RegistryEntry(Protocol):
    """提交校验只读取 Registry Entry 的严格 Manifest。"""

    manifest: CapabilityManifest


class _RouterProjection(Protocol):
    """健康复查结果只需要稳定 Capability 标识。"""

    capability_id: str


class RegenerateRegistry(Protocol):
    """Regenerate 提交校验所需的最小 Registry 只读接口。"""

    def get(self, capability_id: str) -> _RegistryEntry | None:
        """读取 active 且 enabled 的静态 Registry Entry。"""

    async def serviceable_router_projections(
        self,
    ) -> Sequence[_RouterProjection]:
        """刷新过期健康快照并返回当前可服务能力。"""


class TaskContextReader(Protocol):
    """读取 Session 与 Capability 的 current Task 指针。"""

    async def get(
        self,
        *,
        session_id: UUID,
        capability_id: str,
    ) -> CapabilityTaskContext | None:
        """无 current Task 时返回空。"""


class TaskReader(Protocol):
    """按稳定 UUID 读取 Capability Task 投影。"""

    async def get(self, task_id: UUID) -> CapabilityTask | None:
        """Task 不存在时返回空。"""


@dataclass(frozen=True, slots=True)
class RegenerateSubmissionDecision:
    """保存提交门禁确认的能力与 Task 解析输入，不创建持久化对象。"""

    capability_id: str
    task_action: CapabilityTaskAction
    current_task_id: UUID | None


class CapabilityRegeneratePolicy:
    """只允许 invocation State 且无外部副作用的 Capability 重新生成。"""

    def require_supported(self, manifest: CapabilityManifest) -> None:
        """不满足固定安全矩阵时返回提交前 HTTP 409 错误。"""

        if (
            manifest.state_scope == "invocation"
            and manifest.side_effect_policy == "none"
        ):
            return
        raise CapabilityRegenerateUnsupportedError(
            code="CAPABILITY_REGENERATE_UNSUPPORTED",
            message="该能力不支持重新生成",
            status_code=409,
            retryable=False,
        )


class RegenerateSubmissionValidator:
    """在 Run 与 Parent Fork 创建前复查 Capability 的全部静态和动态门禁。"""

    def __init__(
        self,
        *,
        registry: RegenerateRegistry,
        permission_service: CapabilityPermissionService,
        task_context_repository: TaskContextReader,
        task_repository: TaskReader,
        policy: CapabilityRegeneratePolicy | None = None,
    ) -> None:
        self._registry = registry
        self._permission_service = permission_service
        self._task_context_repository = task_context_repository
        self._task_repository = task_repository
        self._policy = policy or CapabilityRegeneratePolicy()

    async def validate(
        self,
        *,
        user_id: str,
        session_id: UUID,
        capability_id: str,
    ) -> RegenerateSubmissionDecision:
        """复查 Registry、策略、Health、权限及 current Task State Schema。"""

        started_at = perf_counter()
        normalized_id = validate_capability_id(capability_id)
        log_business_event(
            logger,
            "Capability重新生成提交校验开始",
            session_id=session_id,
            capability_id=normalized_id,
        )
        try:
            entry = self._registry.get(normalized_id)
            if entry is None:
                raise CapabilityUnavailableError(
                    code="CAPABILITY_UNAVAILABLE",
                    message="该能力当前不可用，请稍后重试",
                    status_code=503,
                    retryable=True,
                )
            manifest = entry.manifest
            self._policy.require_supported(manifest)

            serviceable = await self._registry.serviceable_router_projections()
            if not any(
                projection.capability_id == normalized_id
                for projection in serviceable
            ):
                raise CapabilityUnavailableError(
                    code="CAPABILITY_UNAVAILABLE",
                    message="该能力当前不可用，请稍后重试",
                    status_code=503,
                    retryable=True,
                )

            try:
                await self._permission_service.require_allowed(
                    user_id=user_id,
                    capability_id=normalized_id,
                )
            except CapabilityPermissionDeniedError as error:
                raise CapabilityPermissionDeniedError(
                    code=error.code,
                    message=error.message,
                    status_code=403,
                    retryable=error.retryable,
                ) from error
            context = await self._task_context_repository.get(
                session_id=session_id,
                capability_id=normalized_id,
            )
            current_task_id: UUID | None = None
            if context is not None:
                task = await self._task_repository.get(context.current_task_id)
                self._validate_current_task(
                    task=task,
                    context=context,
                    manifest=manifest,
                    session_id=session_id,
                )
                current_task_id = context.current_task_id
        except ApplicationError as error:
            log_business_event(
                logger,
                "Capability重新生成提交校验拒绝",
                level=logging.WARNING,
                session_id=session_id,
                capability_id=normalized_id,
                error_code=error.code,
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise

        decision = RegenerateSubmissionDecision(
            capability_id=normalized_id,
            task_action="continue",
            current_task_id=current_task_id,
        )
        log_business_event(
            logger,
            "Capability重新生成提交校验通过",
            session_id=session_id,
            capability_id=normalized_id,
            task_id=current_task_id,
            task_action=decision.task_action,
            status="allowed",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return decision

    @staticmethod
    def _validate_current_task(
        *,
        task: CapabilityTask | None,
        context: CapabilityTaskContext,
        manifest: CapabilityManifest,
        session_id: UUID,
    ) -> None:
        """拒绝损坏的 current 指针，并校验 Task 固化的 State Schema。"""

        if (
            task is None
            or task.task_id != context.current_task_id
            or task.session_id != session_id
            or task.capability_id != manifest.capability_id
            or task.status != "active"
        ):
            raise ApplicationError(
                code="CAPABILITY_TASK_CONTEXT_CONFLICT",
                message="Capability current Task 关联关系非法",
                status_code=409,
                retryable=False,
            )
        StateCompatibilityPolicy.from_manifest(manifest).ensure_compatible(
            task.state_schema_version
        )
