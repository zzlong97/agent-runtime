"""Stage 3 Capability 健康快照、TTL 刷新与服务准入。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from agent_runtime.capabilities.contracts import HealthResult
from agent_runtime.capabilities.manifest import (
    ManifestCapabilityId,
    validate_capability_id,
)
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)

_SUMMARY_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,127}$"

type HealthCheck = Callable[[], Awaitable[HealthResult]]
type HealthClock = Callable[[], datetime]
type HealthCapabilityBindings = Mapping[
    str,
    tuple[bool, HealthCheck],
]


class HealthSnapshot(BaseModel):
    """保存单个 Capability 最近一次健康检查的进程内只读快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: ManifestCapabilityId = Field(
        description=(
            "本健康快照所属的稳定 Capability 标识；只允许符合 Manifest ID 规则的"
            "字符串，不得包含来源路径或健康详情。"
        ),
    )
    status: Literal["healthy", "degraded", "unhealthy"] = Field(
        description=(
            "最近一次检查结果；healthy 表示正常，degraded 表示降级，unhealthy 表示"
            "不可用或检查异常。"
        ),
    )
    checked_at: datetime = Field(
        description=(
            "本次健康检查完成时的带时区 UTC 时间，仅用于进程内 TTL 判定和内部诊断。"
        ),
    )
    expires_at: datetime = Field(
        description=(
            "本快照失效的带时区 UTC 时间；当前时间达到该值时必须按需刷新。"
        ),
    )
    summary_code: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=_SUMMARY_CODE_PATTERN,
        description=(
            "Capability 返回或 Runtime 生成的稳定健康摘要码；只供内部日志和诊断，"
            "不得进入 Router 输入或公开 SSE。"
        ),
    )

    @model_validator(mode="after")
    def validate_time_window(self) -> HealthSnapshot:
        """确保快照使用可比较的带时区时间和严格正 TTL。"""

        if self.checked_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("健康快照时间必须包含时区")
        if self.expires_at <= self.checked_at:
            raise ValueError("健康快照 expires_at 必须晚于 checked_at")
        return self


class ServiceabilityDecision(BaseModel):
    """表示 Runtime 根据健康快照和 Manifest 策略得出的内部准入结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: ManifestCapabilityId = Field(
        description=(
            "本次服务准入判定对应的稳定 Capability 标识；仅供 Runtime 内部关联。"
        ),
    )
    serviceable: StrictBool = Field(
        description=(
            "当前是否允许进入 Router 或 Invocation；healthy 为 true，degraded 由"
            "allow_degraded 决定，unhealthy 始终为 false。"
        ),
    )
    snapshot: HealthSnapshot = Field(
        description=(
            "支持本次判定的最新进程内健康快照；仅供 Runtime 内部使用，不得传给"
            "Router、公开事件或客户端。"
        ),
    )


class CapabilityHealthService:
    """执行启动强检、TTL 复用和每能力单飞刷新。"""

    def __init__(
        self,
        *,
        capabilities: HealthCapabilityBindings,
        ttl_seconds: float,
        clock: HealthClock,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or ttl_seconds <= 0
        ):
            raise ValueError("Capability Health TTL 必须是有限正数")
        if not callable(clock):
            raise TypeError("Capability Health clock 必须可调用")

        bindings: dict[str, tuple[bool, HealthCheck]] = {}
        for raw_capability_id, binding in capabilities.items():
            capability_id = validate_capability_id(raw_capability_id)
            if (
                not isinstance(binding, tuple)
                or len(binding) != 2
                or type(binding[0]) is not bool
                or not callable(binding[1])
            ):
                raise TypeError(
                    "Capability Health 绑定必须是 (allow_degraded, health_check)"
                )
            bindings[capability_id] = binding

        self._capabilities = bindings
        self._ttl = timedelta(seconds=float(ttl_seconds))
        self._clock = clock
        self._snapshots: dict[str, HealthSnapshot] = {}
        self._refresh_locks = {
            capability_id: asyncio.Lock() for capability_id in bindings
        }

    @classmethod
    async def start(
        cls,
        *,
        capabilities: HealthCapabilityBindings,
        ttl_seconds: float,
        clock: HealthClock | None = None,
    ) -> CapabilityHealthService:
        """顺序强检每个已实例化 Capability，并隔离单能力检查失败。"""

        service = cls(
            capabilities=capabilities,
            ttl_seconds=ttl_seconds,
            clock=_utc_now if clock is None else clock,
        )
        log_business_event(
            logger,
            "Capability启动健康强检开始",
            capability_count=len(service._capabilities),
        )
        for capability_id in service._capabilities:
            await service._refresh(capability_id, reason="startup")
        log_business_event(
            logger,
            "Capability启动健康强检完成",
            capability_count=len(service._capabilities),
            serviceable_count=sum(
                service._make_decision(capability_id).serviceable
                for capability_id in service._capabilities
            ),
        )
        return service

    async def get_serviceability(
        self,
        capability_id: str,
    ) -> ServiceabilityDecision:
        """返回新鲜准入判定，过期时在每能力锁内只刷新一次。"""

        normalized_id = validate_capability_id(capability_id)
        if normalized_id not in self._capabilities:
            raise KeyError(normalized_id)

        now = self._read_clock()
        snapshot = self._snapshots[normalized_id]
        if now < snapshot.expires_at:
            return self._make_decision(normalized_id)

        async with self._refresh_locks[normalized_id]:
            now = self._read_clock()
            snapshot = self._snapshots[normalized_id]
            if now >= snapshot.expires_at:
                await self._refresh(normalized_id, reason="ttl_expired")
            return self._make_decision(normalized_id)

    async def serviceable_capability_ids(self) -> tuple[str, ...]:
        """按注册稳定顺序返回当前健康可服务 ID，不暴露健康详情。"""

        serviceable: list[str] = []
        for capability_id in self._capabilities:
            decision = await self.get_serviceability(capability_id)
            if decision.serviceable:
                serviceable.append(capability_id)
        return tuple(serviceable)

    def startup_snapshot(self, capability_id: str) -> HealthSnapshot:
        """返回启动强检生成的初始只读快照，供 Registry 构建静态诊断记录。"""

        normalized_id = validate_capability_id(capability_id)
        try:
            return self._snapshots[normalized_id]
        except KeyError as error:
            raise KeyError(normalized_id) from error

    async def _refresh(self, capability_id: str, *, reason: str) -> None:
        """执行一次受控检查，把异常收敛为不含敏感详情的 unhealthy 快照。"""

        started_at = perf_counter()
        _, health_check = self._capabilities[capability_id]
        log_business_event(
            logger,
            "Capability健康检查开始",
            capability_id=capability_id,
            check_reason=reason,
        )
        try:
            result = await health_check()
            if not isinstance(result, HealthResult):
                raise TypeError("Capability health_check 必须返回 HealthResult")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_business_event(
                logger,
                "Capability健康检查失败",
                level=logging.WARNING,
                capability_id=capability_id,
                check_reason=reason,
                status="unhealthy",
                error_code="CAPABILITY_HEALTH_CHECK_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            result = HealthResult(
                status="unhealthy",
                summary_code="HEALTH_CHECK_FAILED",
            )

        checked_at = self._read_clock()
        self._snapshots[capability_id] = HealthSnapshot(
            capability_id=capability_id,
            status=result.status,
            checked_at=checked_at,
            expires_at=checked_at + self._ttl,
            summary_code=result.summary_code,
        )
        log_business_event(
            logger,
            "Capability健康检查完成",
            capability_id=capability_id,
            check_reason=reason,
            health_status=result.status,
            health_summary_code=result.summary_code,
            duration_ms=_elapsed_ms(started_at),
        )

    def _make_decision(self, capability_id: str) -> ServiceabilityDecision:
        """应用 healthy/degraded/unhealthy 与 allow_degraded 的固定准入矩阵。"""

        allow_degraded, _ = self._capabilities[capability_id]
        snapshot = self._snapshots[capability_id]
        serviceable = snapshot.status == "healthy" or (
            snapshot.status == "degraded" and allow_degraded
        )
        if snapshot.status == "degraded":
            log_business_event(
                logger,
                "Capability降级健康准入判定",
                level=logging.WARNING,
                capability_id=capability_id,
                health_status="degraded",
                health_summary_code=snapshot.summary_code,
                allow_degraded=allow_degraded,
                status="serviceable" if serviceable else "unavailable",
            )
        return ServiceabilityDecision(
            capability_id=capability_id,
            serviceable=serviceable,
            snapshot=snapshot,
        )

    def _read_clock(self) -> datetime:
        """读取并规范化带时区 UTC 时间，拒绝含糊的本地时间。"""

        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("Capability Health clock 必须返回带时区 datetime")
        return value.astimezone(UTC)


def _utc_now() -> datetime:
    """返回当前带时区 UTC 时间。"""

    return datetime.now(UTC)


def _elapsed_ms(started_at: float) -> float:
    """返回适合业务日志的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
