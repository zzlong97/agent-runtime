"""S3-07 Capability 健康快照与服务准入测试。"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_runtime.capabilities.contracts import HealthResult


class FakeClock:
    """提供可精确推进的 UTC 假时钟。"""

    def __init__(self) -> None:
        self.current = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class SequencedHealthCheck:
    """按顺序返回结果或抛出异常，并记录真实调用次数。"""

    def __init__(self, *results: HealthResult | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> HealthResult:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _healthy(summary_code: str = "READY") -> HealthResult:
    return HealthResult(status="healthy", summary_code=summary_code)


def test_health_snapshot_and_decision_schemas_are_closed_and_described() -> None:
    from agent_runtime.capabilities.health import (
        HealthSnapshot,
        ServiceabilityDecision,
    )

    for schema_type in (HealthSnapshot, ServiceabilityDecision):
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        assert schema["properties"]
        assert all(
            property_schema.get("description")
            for property_schema in schema["properties"].values()
        )


def test_health_snapshot_rejects_invalid_summary_and_naive_time() -> None:
    from agent_runtime.capabilities.health import HealthSnapshot

    checked_at = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        HealthSnapshot(
            capability_id="health_cap",
            status="healthy",
            checked_at=checked_at,
            expires_at=checked_at + timedelta(seconds=30),
            summary_code="not-a-stable-code",
        )

    with pytest.raises(ValidationError, match="时区"):
        HealthSnapshot(
            capability_id="health_cap",
            status="healthy",
            checked_at=checked_at.replace(tzinfo=None),
            expires_at=(checked_at + timedelta(seconds=30)).replace(tzinfo=None),
            summary_code="READY",
        )


def test_startup_checks_each_capability_exactly_once_and_reuses_fresh_snapshot() -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    clock = FakeClock()
    first = SequencedHealthCheck(_healthy("FIRST_READY"))
    second = SequencedHealthCheck(_healthy("SECOND_READY"))

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={
                "first_cap": (False, first),
                "second_cap": (False, second),
            },
            ttl_seconds=30,
            clock=clock,
        )

        first_decision = await service.get_serviceability("first_cap")
        second_decision = await service.get_serviceability("second_cap")

        assert first_decision.serviceable is True
        assert first_decision.snapshot.summary_code == "FIRST_READY"
        assert first_decision.snapshot.checked_at == clock.current
        assert first_decision.snapshot.expires_at == clock.current + timedelta(
            seconds=30
        )
        assert second_decision.serviceable is True

    asyncio.run(scenario())

    assert first.calls == 1
    assert second.calls == 1


def test_ttl_is_fresh_before_expiry_and_refreshes_at_exact_boundary() -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    clock = FakeClock()
    check = SequencedHealthCheck(
        _healthy("STARTUP_READY"),
        HealthResult(status="unhealthy", summary_code="DEPENDENCY_DOWN"),
    )

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={"ttl_cap": (False, check)},
            ttl_seconds=10,
            clock=clock,
        )

        clock.advance(9.999)
        assert (await service.get_serviceability("ttl_cap")).serviceable is True
        assert check.calls == 1

        clock.advance(0.001)
        refreshed = await service.get_serviceability("ttl_cap")
        assert refreshed.serviceable is False
        assert refreshed.snapshot.status == "unhealthy"
        assert refreshed.snapshot.summary_code == "DEPENDENCY_DOWN"

    asyncio.run(scenario())
    assert check.calls == 2


def test_expired_concurrent_requests_share_one_singleflight_refresh() -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    clock = FakeClock()
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    calls = 0

    async def health_check() -> HealthResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _healthy("STARTUP_READY")
        refresh_started.set()
        await allow_refresh.wait()
        return _healthy("REFRESHED_READY")

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={"singleflight_cap": (False, health_check)},
            ttl_seconds=5,
            clock=clock,
        )
        clock.advance(5)
        requests = [
            asyncio.create_task(
                service.get_serviceability("singleflight_cap")
            )
            for _ in range(20)
        ]
        await refresh_started.wait()
        await asyncio.sleep(0)
        assert calls == 2
        allow_refresh.set()
        decisions = await asyncio.gather(*requests)

        assert {item.snapshot.summary_code for item in decisions} == {
            "REFRESHED_READY"
        }

    asyncio.run(scenario())
    assert calls == 2


@pytest.mark.parametrize(
    ("allow_degraded", "expected_serviceable"),
    [(True, True), (False, False)],
)
def test_degraded_serviceability_obeys_manifest_policy(
    allow_degraded: bool,
    expected_serviceable: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    check = SequencedHealthCheck(
        HealthResult(status="degraded", summary_code="PARTIAL_READY")
    )

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={"degraded_cap": (allow_degraded, check)},
            ttl_seconds=30,
            clock=FakeClock(),
        )
        decision = await service.get_serviceability("degraded_cap")
        assert decision.serviceable is expected_serviceable
        assert decision.snapshot.status == "degraded"

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    assert "Capability降级健康准入判定" in caplog.text
    assert "PARTIAL_READY" in caplog.text


def test_health_exception_isolated_and_log_does_not_expose_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    secret = "secret-token-should-not-appear"
    failed = SequencedHealthCheck(RuntimeError(secret))
    healthy = SequencedHealthCheck(_healthy())

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={
                "failed_cap": (False, failed),
                "healthy_cap": (False, healthy),
            },
            ttl_seconds=30,
            clock=FakeClock(),
        )
        failed_decision = await service.get_serviceability("failed_cap")
        healthy_decision = await service.get_serviceability("healthy_cap")

        assert failed_decision.serviceable is False
        assert failed_decision.snapshot.status == "unhealthy"
        assert failed_decision.snapshot.summary_code == "HEALTH_CHECK_FAILED"
        assert healthy_decision.serviceable is True

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    assert failed.calls == 1
    assert healthy.calls == 1
    assert "Capability健康检查失败" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_health_service_rejects_invalid_ttl_and_unknown_capability() -> None:
    from agent_runtime.capabilities.health import CapabilityHealthService

    with pytest.raises(ValueError, match="TTL"):
        asyncio.run(
            CapabilityHealthService.start(
                capabilities={},
                ttl_seconds=0,
                clock=FakeClock(),
            )
        )

    async def scenario() -> None:
        service = await CapabilityHealthService.start(
            capabilities={},
            ttl_seconds=30,
            clock=FakeClock(),
        )
        with pytest.raises(KeyError, match="unknown_cap"):
            await service.get_serviceability("unknown_cap")

    asyncio.run(scenario())
