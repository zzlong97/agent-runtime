"""S3-06 真实 PostgreSQL 权限动态变更测试。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_postgres_permission_change_is_visible_to_invoke_recheck() -> None:
    from agent_runtime.capabilities.persistence import (
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.capabilities.persistence_models import (
        UserCapabilityPermission,
    )
    from agent_runtime.capabilities.routing import (
        CapabilityPermissionDeniedError,
        CapabilityPermissionService,
        RouterCandidateProvider,
    )
    from agent_runtime.capabilities.registry import RouterProjection
    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-permission-{uuid4()}",
        _env_file=None,
    )
    repository = UserCapabilityPermissionRepository(settings)
    service = CapabilityPermissionService(repository)
    capability_id = "weather_lookup"
    projection = RouterProjection(
        capability_id=capability_id,
        name="天气查询",
        description="查询公开天气信息。",
        enabled=True,
    )

    class TestRegistry:
        def active_router_projections(self):
            return (projection,)

        def router_projections(self):
            return (projection,)

        async def serviceable_router_projections(self):
            return (projection,)

    provider = RouterCandidateProvider(
        registry=TestRegistry(),
        permission_service=service,
    )

    async def exercise() -> None:
        await repository.setup()
        await repository.revoke(
            user_id=settings.local_user_id,
            capability_id=capability_id,
        )
        with pytest.raises(CapabilityPermissionDeniedError):
            await service.require_allowed(
                user_id=settings.local_user_id,
                capability_id=capability_id,
            )

        now = datetime.now(UTC)
        await repository.set_allowed(
            UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id=capability_id,
                allowed=True,
                created_at=now,
                updated_at=now,
            )
        )
        candidates = await provider.get_candidates(
            user_id=settings.local_user_id,
            rejected_capability_ids=(),
        )
        assert [item.capability_id for item in candidates] == [capability_id]

        await repository.set_allowed(
            UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id=capability_id,
                allowed=False,
                created_at=now,
                updated_at=datetime.now(UTC),
            )
        )
        with pytest.raises(CapabilityPermissionDeniedError):
            await service.require_allowed(
                user_id=settings.local_user_id,
                capability_id=capability_id,
            )
        await repository.revoke(
            user_id=settings.local_user_id,
            capability_id=capability_id,
        )

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
