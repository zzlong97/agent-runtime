"""Registry 使用的受信远程发现适配器；执行闭环由后续任务实现。"""

import logging

import httpx

from agent_runtime.capabilities.contracts import CapabilityError, HealthResult
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.remote_a2a.client import RemoteA2AClient
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)


class RemoteA2ACapability:
    """不向本地 Bootstrap 暴露 Client，也不在本批绕过 Run 执行门禁。"""

    def __init__(
        self,
        *,
        manifest: CapabilityManifest,
        development_mode: bool = False,
        http_timeout_seconds: float = 10.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        if manifest.transport.type != "remote_a2a":
            raise TypeError("远程适配器只能接收 remote_a2a Manifest")
        self._capability_id = manifest.capability_id
        self.client = RemoteA2AClient(
            capability_id=manifest.capability_id,
            transport=manifest.transport,
            development_mode=development_mode,
            http_timeout_seconds=http_timeout_seconds,
            http_transport=http_transport,
        )

    async def initialize(self) -> None:
        """启动时先完成 Card/Profile 校验，失败仅隔离该来源。"""
        await self.client.discover()

    async def health_check(self) -> HealthResult:
        """TTL 刷新重新验证 Card，不能重置已固化身份或接口。"""
        await self.client.discover()
        return HealthResult(status="healthy", summary_code="REMOTE_A2A_READY")

    async def invoke(self, run_context, agent_context, task_input):
        """持久绑定闭环交付前安全拒绝，不发送远程业务请求。"""
        log_business_event(
            logger,
            "远程能力执行尚未开放",
            capability_id=self._capability_id,
            status="rejected",
            error_code="CAPABILITY_REMOTE_UNAVAILABLE",
        )
        raise CapabilityError(
            code="CAPABILITY_REMOTE_UNAVAILABLE",
            message="远程能力执行链路尚未开放",
            retryable=False,
        )

    async def close(self) -> None:
        """把连接池所有权交回 Registry 清理组。"""
        await self.client.close()
