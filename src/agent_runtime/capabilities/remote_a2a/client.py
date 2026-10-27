"""固定官方 1.0 REST Transport，隔离认证、网络与原始响应。"""

import asyncio
from collections.abc import Mapping
import logging
import os
import re
from time import perf_counter

import httpx
from a2a.client.transports.rest import RestTransport
from a2a.types import a2a_pb2 as wire
from google.protobuf.json_format import ParseDict
from pydantic import SecretStr

from agent_runtime.capabilities.manifest import RemoteA2ATransport
from agent_runtime.capabilities.remote_a2a.card import (
    AgentCardSnapshot,
    origin,
    validate_card,
    validated_url,
)
from agent_runtime.capabilities.remote_a2a.errors import (
    RemoteA2AError,
    auth_error,
    protocol_error,
    unavailable_error,
)
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)
_PATH_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.~-]*$")


class _ControlledHTTPClient(httpx.AsyncClient):
    """在官方 SDK 接收异常前，切断 HTTP 原始异常及其完整原因链。"""

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        """覆盖响应头解析、非流式正文读取和 hook；取消保持原样传播。"""
        try:
            return await super().send(request, **kwargs)
        except asyncio.CancelledError:
            raise
        except RemoteA2AError as error:
            # hook 已决定受控语义，但其 __context__ 仍可能保留原始解析异常。
            factory = {
                "CAPABILITY_REMOTE_AUTH_FAILED": auth_error,
                "CAPABILITY_REMOTE_UNAVAILABLE": unavailable_error,
            }.get(error.code, protocol_error)
            controlled = factory()
        except httpx.RequestError:
            controlled = unavailable_error()
        except Exception:
            controlled = protocol_error()
        # 必须退出 except 后再抛新对象；from None 仅隐藏原异常，不能清除异常链。
        raise controlled


class RemoteA2AClient:
    """持有单能力 HTTP 资源；只做协议操作，不拥有 Run、Ledger 或重试循环。"""

    def __init__(
        self,
        *,
        capability_id: str,
        transport: RemoteA2ATransport,
        development_mode: bool = False,
        http_timeout_seconds: float = 10.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        self._capability_id = capability_id
        self._config = transport
        self._development_mode = development_mode
        source = validated_url(
            transport.agent_card_url, development_mode=development_mode
        )
        if transport.auth.type == "none" and not development_mode:
            raise auth_error()
        self._token = None
        if transport.auth.type == "bearer":
            token = (os.environ if environ is None else environ).get(
                transport.auth.token_env
            )
            if (
                not token
                or not token.strip()
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
            ):
                raise auth_error()
            self._token = SecretStr(token)
        self._source_origin = origin(source)
        self._http = _ControlledHTTPClient(
            transport=http_transport,
            timeout=http_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            event_hooks={
                "request": [self._secure_request],
                "response": [self._check_response],
            },
        )
        self._snapshot: AgentCardSnapshot | None = None
        self._rest: RestTransport | None = None

    @property
    def snapshot(self) -> AgentCardSnapshot | None:
        """返回不可变接口身份；刷新失败不替换已建立的静态身份。"""
        return self._snapshot

    async def _secure_request(self, request: httpx.Request) -> None:
        """发送凭据之前验证每一个实际请求，且禁止自动环境代理或重定向。"""
        target = validated_url(
            str(request.url), development_mode=self._development_mode
        )
        if origin(target) != self._source_origin:
            raise protocol_error()
        request.headers["A2A-Version"] = "1.0"
        if self._token is not None:
            request.headers["Authorization"] = (
                "Bearer " + self._token.get_secret_value()
            )

    async def _check_response(self, response: httpx.Response) -> None:
        """在 SDK 解析响应前收敛 HTTP 错误，避免其错误文本包含内部地址。"""
        if response.status_code in {401, 403}:
            raise auth_error()
        if response.status_code >= 500:
            raise unavailable_error()
        if response.status_code < 200 or response.status_code >= 300:
            raise protocol_error()
        # SDK 的解析错误会被其遥测记录；提前用相同官方类型解析，把原始值截在此边界。
        path = response.request.url.path
        if path.endswith("/message:send") or "/tasks/" in path:
            try:
                await response.aread()
                model = (
                    wire.SendMessageResponse()
                    if path.endswith("/message:send")
                    else wire.Task()
                )
                ParseDict(response.json(), model)
            except httpx.RequestError:
                raise unavailable_error() from None
            except Exception:
                raise protocol_error() from None

    async def _call(self, operation: str, callback, *, message_id: str | None = None):
        """关键入口和出口仅记录关联标识、受控码与耗时。"""
        started = perf_counter()
        log_business_event(
            logger,
            "远程协议请求开始",
            capability_id=self._capability_id,
            operation=operation,
            message_id=message_id,
        )
        try:
            result = await callback()
        except asyncio.CancelledError:
            log_business_event(
                logger,
                "远程协议请求取消",
                capability_id=self._capability_id,
                operation=operation,
                message_id=message_id,
                status="cancelled",
            )
            raise
        except Exception as error:
            if isinstance(error, RemoteA2AError):
                controlled = error
            else:
                # SDK 会包装 HTTP 异常；只沿异常类型判断暂态，不复制其文案或响应。
                cause = error
                network_error = False
                while cause is not None:
                    if isinstance(cause, httpx.RequestError):
                        network_error = True
                    cause = cause.__cause__
                controlled = unavailable_error() if network_error else protocol_error()
            log_business_event(
                logger,
                "远程协议请求失败",
                level=logging.WARNING,
                capability_id=self._capability_id,
                operation=operation,
                message_id=message_id,
                error_code=controlled.code,
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started) * 1000, 2),
            )
            raise controlled from None
        log_business_event(
            logger,
            "远程协议请求完成",
            capability_id=self._capability_id,
            operation=operation,
            message_id=message_id,
            status="completed",
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )
        return result

    async def discover(self) -> AgentCardSnapshot:
        """拉取受控 Card；不通过 SDK Factory 协商或回退旧传输。"""

        async def request():
            response = await self._http.get(self._config.agent_card_url)
            card, snapshot = validate_card(
                response.json(),
                transport=self._config,
                development_mode=self._development_mode,
            )
            if self._snapshot is not None and self._snapshot != snapshot:
                raise protocol_error()
            if self._snapshot is None:
                self._snapshot = snapshot
                self._rest = RestTransport(self._http, card, snapshot.interface_url)
            return self._snapshot

        return await self._call("discover", request)

    async def send_message(
        self,
        *,
        message_id: str,
        text: str,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> wire.Task:
        """只发送当前文本；稳定消息身份由上层 Invocation/Resume 决定。"""

        async def request():
            rest, snapshot = self._require_discovered()
            if (
                not message_id.strip()
                or not text.strip()
                or bool(task_id) != bool(context_id)
            ):
                raise protocol_error()
            if task_id is not None:
                self._validate_task_id(task_id)
            message = wire.Message(
                message_id=message_id,
                role=wire.ROLE_USER,
                parts=[wire.Part(text=text)],
                task_id=task_id or "",
                context_id=context_id or "",
            )
            response = await rest.send_message(
                wire.SendMessageRequest(
                    tenant=snapshot.tenant,
                    message=message,
                    configuration=wire.SendMessageConfiguration(
                        return_immediately=True, accepted_output_modes=["text/plain"]
                    ),
                )
            )
            if not response.HasField("task"):
                raise protocol_error()
            return self._validate_task(
                response.task, expected_id=task_id, expected_context=context_id
            )

        return await self._call("send", request, message_id=message_id)

    async def get_task(self, task_id: str) -> wire.Task:
        """只读查询；不重试、不写 Ledger、不决定顶层 Run 终态。"""

        async def request():
            self._validate_task_id(task_id)
            rest, snapshot = self._require_discovered()
            return self._validate_task(
                await rest.get_task(
                    wire.GetTaskRequest(id=task_id, tenant=snapshot.tenant)
                ),
                expected_id=task_id,
            )

        return await self._call("get", request)

    async def cancel_task(self, task_id: str) -> wire.Task:
        """返回远程实际状态；取消响应不是本地取消已确认的替代品。"""

        async def request():
            self._validate_task_id(task_id)
            rest, snapshot = self._require_discovered()
            return self._validate_task(
                await rest.cancel_task(
                    wire.CancelTaskRequest(id=task_id, tenant=snapshot.tenant)
                ),
                expected_id=task_id,
            )

        return await self._call("cancel", request)

    def _require_discovered(self):
        """发现失败时不能直接使用远程 Client。"""
        if self._rest is None or self._snapshot is None:
            raise protocol_error()
        return self._rest, self._snapshot

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        """SDK 直接拼接任务路径，因此在进入 SDK 前拒绝路径和编码注入。"""
        if not _PATH_ID.fullmatch(task_id) or task_id in {".", ".."}:
            raise protocol_error()

    @staticmethod
    def _validate_task(
        task: wire.Task, *, expected_id=None, expected_context=None
    ) -> wire.Task:
        """验证可恢复身份和最小文本 Profile，不复制 Task 历史到公共数据面。"""
        RemoteA2AClient._validate_task_id(task.id)
        if (
            not task.context_id.strip()
            or (expected_id and task.id != expected_id)
            or (expected_context and task.context_id != expected_context)
        ):
            raise protocol_error()
        if (
            task.status.state not in dict(wire.TaskState.items()).values()
            or task.status.state == wire.TASK_STATE_UNSPECIFIED
        ):
            raise protocol_error()
        if task.status.state == wire.TASK_STATE_COMPLETED:
            if not any(
                part.WhichOneof("content") == "text" and part.text.strip()
                for artifact in task.artifacts
                for part in artifact.parts
            ):
                raise protocol_error()
        if task.status.state == wire.TASK_STATE_INPUT_REQUIRED:
            message = task.status.message
            if not message.message_id.strip() or not any(
                part.WhichOneof("content") == "text" and part.text.strip()
                for part in message.parts
            ):
                raise protocol_error()
        return task

    async def close(self) -> None:
        """幂等关闭本 Client 拥有的 HTTP 连接池。"""
        await self._http.aclose()
