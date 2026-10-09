"""S3-06 权限双检与动态 Router 候选测试。"""

import asyncio

import pytest


def _projection(capability_id: str):
    from agent_runtime.capabilities.registry import RouterProjection

    return RouterProjection(
        capability_id=capability_id,
        name=f"{capability_id} 名称",
        description=f"{capability_id} 公开说明",
        enabled=True,
    )


class FakePermissionRepository:
    def __init__(self, values: dict[str, bool | None]) -> None:
        self.values = values
        self.calls: list[tuple[str, str]] = []

    async def get_allowed(self, *, user_id: str, capability_id: str):
        self.calls.append((user_id, capability_id))
        return self.values.get(capability_id)


class FakeRegistry:
    def __init__(self, *, active: tuple, serviceable: tuple) -> None:
        self._active = active
        self._serviceable = serviceable

    def active_router_projections(self):
        return self._active

    def router_projections(self):
        return self._serviceable

    async def serviceable_router_projections(self):
        return self._serviceable


def test_permission_candidates_require_explicit_true_and_never_auto_authorize() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionService,
        RouterCandidateProvider,
    )

    active = tuple(
        _projection(item)
        for item in ("general_chat", "en_to_zh", "weather_lookup")
    )
    repository = FakePermissionRepository(
        {
            "general_chat": None,
            "en_to_zh": False,
            "weather_lookup": True,
        }
    )
    provider = RouterCandidateProvider(
        registry=FakeRegistry(active=active, serviceable=active),
        permission_service=CapabilityPermissionService(repository),
    )

    candidates = asyncio.run(
        provider.get_candidates(user_id="runtime-user", rejected_capability_ids=())
    )

    assert [item.capability_id for item in candidates] == ["weather_lookup"]
    assert repository.calls == [
        ("runtime-user", "general_chat"),
        ("runtime-user", "en_to_zh"),
        ("runtime-user", "weather_lookup"),
    ]


def test_no_authorized_candidate_maps_to_non_retryable_permission_denied() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionDeniedError,
        CapabilityPermissionService,
        RouterCandidateProvider,
    )

    projection = _projection("general_chat")
    provider = RouterCandidateProvider(
        registry=FakeRegistry(
            active=(projection,),
            serviceable=(projection,),
        ),
        permission_service=CapabilityPermissionService(
            FakePermissionRepository({"general_chat": None})
        ),
    )

    with pytest.raises(CapabilityPermissionDeniedError) as captured:
        asyncio.run(
            provider.get_candidates(
                user_id="runtime-user",
                rejected_capability_ids=(),
            )
        )

    assert captured.value.code == "CAPABILITY_PERMISSION_DENIED"
    assert captured.value.retryable is False


def test_authorized_but_unserviceable_maps_to_retryable_unavailable() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionService,
        CapabilityUnavailableError,
        RouterCandidateProvider,
    )

    projection = _projection("weather_lookup")
    provider = RouterCandidateProvider(
        registry=FakeRegistry(active=(projection,), serviceable=()),
        permission_service=CapabilityPermissionService(
            FakePermissionRepository({"weather_lookup": True})
        ),
    )

    with pytest.raises(CapabilityUnavailableError) as captured:
        asyncio.run(
            provider.get_candidates(
                user_id="runtime-user",
                rejected_capability_ids=(),
            )
        )

    assert captured.value.code == "CAPABILITY_UNAVAILABLE"
    assert captured.value.retryable is True


def test_router_candidates_use_async_dynamic_health_projection() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionService,
        CapabilityUnavailableError,
        RouterCandidateProvider,
    )

    projection = _projection("weather_lookup")

    class DynamicRegistry:
        def active_router_projections(self):
            return (projection,)

        async def serviceable_router_projections(self):
            return ()

        def router_projections(self):
            raise AssertionError("S3-07 Router 不得继续读取启动期同步健康视图")

    provider = RouterCandidateProvider(
        registry=DynamicRegistry(),
        permission_service=CapabilityPermissionService(
            FakePermissionRepository({"weather_lookup": True})
        ),
    )

    with pytest.raises(CapabilityUnavailableError):
        asyncio.run(
            provider.get_candidates(
                user_id="runtime-user",
                rejected_capability_ids=(),
            )
        )


def test_all_authorized_serviceable_candidates_rejected_returns_empty_for_unsupported() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionService,
        RouterCandidateProvider,
    )

    projection = _projection("weather_lookup")
    provider = RouterCandidateProvider(
        registry=FakeRegistry(
            active=(projection,),
            serviceable=(projection,),
        ),
        permission_service=CapabilityPermissionService(
            FakePermissionRepository({"weather_lookup": True})
        ),
    )

    candidates = asyncio.run(
        provider.get_candidates(
            user_id="runtime-user",
            rejected_capability_ids=("weather_lookup",),
        )
    )

    assert candidates == ()


def test_permission_is_rechecked_without_cache_immediately_before_invoke() -> None:
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionDeniedError,
        CapabilityPermissionService,
    )

    repository = FakePermissionRepository({"weather_lookup": True})
    service = CapabilityPermissionService(repository)

    asyncio.run(
        service.require_allowed(
            user_id="runtime-user",
            capability_id="weather_lookup",
        )
    )
    repository.values["weather_lookup"] = False

    with pytest.raises(CapabilityPermissionDeniedError):
        asyncio.run(
            service.require_allowed(
                user_id="runtime-user",
                capability_id="weather_lookup",
            )
        )

    assert repository.calls == [
        ("runtime-user", "weather_lookup"),
        ("runtime-user", "weather_lookup"),
    ]
