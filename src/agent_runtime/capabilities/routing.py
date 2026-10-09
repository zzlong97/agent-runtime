"""Stage 3 Capability 权限双检与动态 Router 候选构造。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from time import perf_counter
from typing import Protocol

from agent_runtime.capabilities.manifest import validate_capability_id
from agent_runtime.capabilities.registry import RouterProjection
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)


class PermissionRepository(Protocol):
    """权限服务所需的最小只读持久化协议。"""

    async def get_allowed(
        self,
        *,
        user_id: str,
        capability_id: str,
    ) -> bool | None:
        """返回无记录、显式拒绝或显式允许。"""


class RegistryProjectionSource(Protocol):
    """候选提供器所需的不可变 Registry Snapshot 视图。"""

    def active_router_projections(self) -> tuple[RouterProjection, ...]:
        """返回忽略健康结果的 active 最小投影。"""

    async def serviceable_router_projections(
        self,
    ) -> tuple[RouterProjection, ...]:
        """按需刷新过期健康快照并返回当前可服务的最小投影。"""


class CapabilityPermissionDeniedError(ApplicationError):
    """当前用户没有目标 Capability 的显式允许权限。"""


class CapabilityUnavailableError(ApplicationError):
    """已授权 Capability 当前没有可服务实例。"""


class CapabilityPermissionService:
    """直接读取 PostgreSQL 权限投影，不缓存任何授权结果。"""

    def __init__(self, repository: PermissionRepository) -> None:
        self._repository = repository

    async def is_allowed(self, *, user_id: str, capability_id: str) -> bool:
        """仅将数据库中的显式 true 解释为允许，无记录和 false 均拒绝。"""

        normalized_user_id = _validate_user_id(user_id)
        normalized_capability_id = validate_capability_id(capability_id)
        try:
            allowed = await self._repository.get_allowed(
                user_id=normalized_user_id,
                capability_id=normalized_capability_id,
            )
        except Exception as error:
            log_business_event(
                logger,
                "Capability权限实时读取失败",
                level=logging.ERROR,
                capability_id=normalized_capability_id,
                error_code=getattr(
                    error,
                    "code",
                    "CAPABILITY_PERMISSION_READ_FAILED",
                ),
                error_type=type(error).__name__,
            )
            raise
        return allowed is True

    async def filter_allowed(
        self,
        *,
        user_id: str,
        candidates: Iterable[RouterProjection],
    ) -> tuple[RouterProjection, ...]:
        """按 Registry 稳定顺序保留显式允许的候选，每次调用重新读取权限。"""

        allowed_candidates: list[RouterProjection] = []
        for candidate in candidates:
            if await self.is_allowed(
                user_id=user_id,
                capability_id=candidate.capability_id,
            ):
                allowed_candidates.append(candidate)
        return tuple(allowed_candidates)

    async def require_allowed(
        self,
        *,
        user_id: str,
        capability_id: str,
    ) -> None:
        """Invocation 开始前实时复查权限，已撤销时以不可重试错误拒绝。"""

        started_at = perf_counter()
        normalized_capability_id = validate_capability_id(capability_id)
        log_business_event(
            logger,
            "Capability调用前权限复查开始",
            capability_id=normalized_capability_id,
        )
        if await self.is_allowed(
            user_id=user_id,
            capability_id=normalized_capability_id,
        ):
            log_business_event(
                logger,
                "Capability调用前权限复查通过",
                capability_id=normalized_capability_id,
                status="allowed",
                duration_ms=_elapsed_ms(started_at),
            )
            return
        log_business_event(
            logger,
            "Capability调用前权限复查拒绝",
            level=logging.WARNING,
            capability_id=normalized_capability_id,
            status="denied",
            error_code="CAPABILITY_PERMISSION_DENIED",
            duration_ms=_elapsed_ms(started_at),
        )
        raise CapabilityPermissionDeniedError(
            code="CAPABILITY_PERMISSION_DENIED",
            message="当前用户未获授权使用该能力",
            retryable=False,
        )


class RouterCandidateProvider:
    """以 Registry、实时权限及本轮拒绝集合构造 Router 最小候选。"""

    def __init__(
        self,
        *,
        registry: RegistryProjectionSource,
        permission_service: CapabilityPermissionService,
    ) -> None:
        self._registry = registry
        self._permission_service = permission_service

    async def get_candidates(
        self,
        *,
        user_id: str,
        rejected_capability_ids: Iterable[str],
    ) -> tuple[RouterProjection, ...]:
        """返回动态候选，并严格区分无权限、不可服务和全部已拒绝。"""

        started_at = perf_counter()
        rejected = frozenset(
            validate_capability_id(item) for item in rejected_capability_ids
        )
        active = self._registry.active_router_projections()
        log_business_event(
            logger,
            "Router动态候选构造开始",
            active_capability_ids=[item.capability_id for item in active],
            rejected_capability_ids=sorted(rejected),
        )
        if not active:
            self._raise_unavailable(started_at=started_at)

        allowed = await self._permission_service.filter_allowed(
            user_id=user_id,
            candidates=active,
        )
        if not allowed:
            log_business_event(
                logger,
                "Router动态候选权限拒绝",
                level=logging.WARNING,
                error_code="CAPABILITY_PERMISSION_DENIED",
                active_capability_ids=[item.capability_id for item in active],
                duration_ms=_elapsed_ms(started_at),
            )
            raise CapabilityPermissionDeniedError(
                code="CAPABILITY_PERMISSION_DENIED",
                message="当前用户没有已授权的可选能力",
                retryable=False,
            )

        serviceable_ids = {
            item.capability_id
            for item in await self._registry.serviceable_router_projections()
        }
        allowed_serviceable = tuple(
            item for item in allowed if item.capability_id in serviceable_ids
        )
        if not allowed_serviceable:
            self._raise_unavailable(started_at=started_at)

        candidates = tuple(
            item
            for item in allowed_serviceable
            if item.capability_id not in rejected
        )
        log_business_event(
            logger,
            "Router动态候选构造完成",
            candidate_capability_ids=[item.capability_id for item in candidates],
            rejected_capability_ids=sorted(rejected),
            status="ready" if candidates else "all_rejected",
            duration_ms=_elapsed_ms(started_at),
        )
        return candidates

    @staticmethod
    def _raise_unavailable(*, started_at: float) -> None:
        """以统一可重试错误拒绝没有可服务实例的候选构造。"""

        log_business_event(
            logger,
            "Router动态候选不可用",
            level=logging.WARNING,
            error_code="CAPABILITY_UNAVAILABLE",
            status="unavailable",
            duration_ms=_elapsed_ms(started_at),
        )
        raise CapabilityUnavailableError(
            code="CAPABILITY_UNAVAILABLE",
            message="已授权能力当前不可用，请稍后重试",
            retryable=True,
        )


def _validate_user_id(user_id: str) -> str:
    """拒绝空白或非字符串用户标识，不在权限边界内猜测默认用户。"""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("用户 ID 必须是非空字符串")
    return user_id.strip()


def _elapsed_ms(started_at: float) -> float:
    """计算便于业务日志观察的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
