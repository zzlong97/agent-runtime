"""受信远程 A2A 文本执行、持久绑定与取消确认适配器。"""

import logging
import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid5

import httpx

from agent_runtime.capabilities.contracts import CapabilityError, HealthResult
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.remote_a2a.client import RemoteA2AClient
from agent_runtime.core.logging import log_business_event
from agent_runtime.capabilities.contracts import AgentResult, AgentResultMetadata
from agent_runtime.runtime.agent_contract import AgentTextEvent
from agent_runtime.runtime.event_models import RuntimeEventDraft
from agent_runtime.runtime.event_schemas import RemoteTaskBoundPayload, RemoteTaskTerminalPayload
from agent_runtime.capabilities.remote_a2a.errors import RemoteA2AError, protocol_error
from a2a.types import a2a_pb2 as wire
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)
_MESSAGE_NAMESPACE = UUID("95f6cf55-74e0-5ad2-a487-a92870f4c360")
_TERMINAL = {wire.TASK_STATE_COMPLETED: "completed", wire.TASK_STATE_FAILED: "failed", wire.TASK_STATE_CANCELED: "canceled", wire.TASK_STATE_REJECTED: "rejected"}


def initial_message_id(invocation_id):
    """同一 Invocation 永远重送同一消息身份，不依赖进程或恢复次数。"""
    return uuid5(_MESSAGE_NAMESPACE, str(invocation_id))


class RemoteExecutionPending(Exception):
    """远程结果尚未确认的内部控制信号，不能映射为执行失败。"""

    def __init__(self, code="CAPABILITY_REMOTE_STATE_UNKNOWN"):
        super().__init__("远程执行结果待确认")
        self.code = code


class RemoteInputRequired(RemoteExecutionPending):
    """文本补参接线前保留 open Invocation，由后续任务接入 Interrupt。"""

    def __init__(self):
        super().__init__("CAPABILITY_REMOTE_INPUT_REQUIRED")


class RemoteA2ACapability:
    """Runtime 专用适配器，Client 和绑定出口不进入普通 Capability SDK。"""

    def __init__(
        self,
        *,
        manifest: CapabilityManifest,
        development_mode: bool = False,
        http_timeout_seconds: float = 10.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_seconds: float = 0.25,
    ):
        if manifest.transport.type != "remote_a2a":
            raise TypeError("远程适配器只能接收 remote_a2a Manifest")
        self._capability_id = manifest.capability_id
        self._poll_interval_seconds = poll_interval_seconds
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

    async def acquire_task(self, run_context, task_input, *, sequencer):
        """绑定已存在时只 Get；受理结果未知时以原消息 ID 幂等取回。"""
        invocation_id = run_context.invocation_id
        binding = await sequencer.get_remote_agent_event(invocation_id=invocation_id, event_type="internal.remote_agent.task_bound")
        opened = await sequencer.get_open_capability_invocation()
        original_hash = None if opened is None else opened.payload.get("remote_agent_identity_hash")
        snapshot = self.client.snapshot
        if (snapshot is None or (not original_hash and binding is None)
                or (original_hash and original_hash != snapshot.fingerprint)
                or (binding and binding.payload["remote_agent_identity_hash"] != snapshot.fingerprint)):
            raise RemoteExecutionPending("CAPABILITY_REMOTE_IDENTITY_CHANGED")
        if binding:
            task = await self.client.get_task(binding.payload["remote_task_id"])
            if task.context_id != binding.payload["remote_context_id"]:
                raise RemoteExecutionPending("CAPABILITY_REMOTE_IDENTITY_CHANGED")
            # 崩溃可能发生在绑定提交后、Operation 正常退出前；确认取回即可补齐。
            if not self.is_scope_rejection(task):
                async with run_context.operation(f"remote_send:{binding.payload['initial_message_id']}"):
                    pass
            return task
        current = next((message for message in reversed(task_input.messages) if isinstance(message, HumanMessage)), None)
        if current is None or not isinstance(current.content, str) or not current.content.strip():
            raise protocol_error()
        message_id = initial_message_id(invocation_id)
        async with run_context.operation(f"remote_send:{message_id}") as operation:
            # succeeded 却无绑定是损坏事实，不能重发或把已成功行降回 pending。
            if not operation.should_execute:
                raise RemoteExecutionPending()
            try:
                task = await self.client.send_message(message_id=str(message_id), text=current.content)
            except RemoteA2AError as error:
                if error.code == "CAPABILITY_REMOTE_AUTH_FAILED":
                    if operation.created:
                        await operation.mark_failed()
                    else:
                        raise RemoteExecutionPending(error.code) from None
                raise
            await sequencer.record_remote_agent_event(self._draft("internal.remote_agent.task_bound", RemoteTaskBoundPayload(
                invocation_id=invocation_id, capability_id=self._capability_id,
                remote_task_id=task.id, remote_context_id=task.context_id,
                initial_message_id=message_id, remote_agent_identity_hash=snapshot.fingerprint, protocol_version="1.0",
            )))
        return task

    async def confirm_task(self, run_context, task, *, sequencer):
        """通过只读 Get 确认同一 Task/context；只有确定终态才登记证据。"""
        confirmed = await self.client.get_task(task.id)
        if confirmed.context_id != task.context_id:
            raise RemoteExecutionPending("CAPABILITY_REMOTE_IDENTITY_CHANGED")
        if confirmed.status.state in _TERMINAL:
            await sequencer.record_remote_agent_event(self._draft("internal.remote_agent.task_terminal", RemoteTaskTerminalPayload(
                invocation_id=run_context.invocation_id, capability_id=self._capability_id,
                remote_task_id=confirmed.id, remote_status=_TERMINAL[confirmed.status.state],
            )))
        return confirmed

    async def cancel_remote(self, run_context, task_input, *, sequencer):
        """一次有界 Cancel/Get 确认；未知结果保持原账本和 open Invocation。"""
        try:
            task = await self.acquire_task(run_context, task_input, sequencer=sequencer)
            if task.status.state not in _TERMINAL:
                async with run_context.operation(f"remote_cancel:{task.id}") as operation:
                    if operation.should_execute:
                        await self.client.cancel_task(task.id)
            task = await self.confirm_task(run_context, task, sequencer=sequencer)
            if task.status.state not in _TERMINAL:
                raise RemoteExecutionPending("CAPABILITY_REMOTE_CANCEL_UNCONFIRMED")
            return task
        except RemoteA2AError:
            raise RemoteExecutionPending("CAPABILITY_REMOTE_CANCEL_UNCONFIRMED") from None

    async def invoke(self, run_context, agent_context, task_input):
        """普通 SDK 不持有绑定写入权限，远程仅允许受信 Gateway 调用。"""
        raise protocol_error()

    async def invoke_bound(self, run_context, agent_context, task_input, *, sequencer):
        """受信 Gateway 注入绑定出口，远程实现只接收当前文本。"""
        if sequencer is None:
            raise protocol_error()
        try:
            task = await self.acquire_task(run_context, task_input, sequencer=sequencer)
            while True:
                await run_context.cancellation.raise_if_requested()
                task = await self.confirm_task(run_context, task, sequencer=sequencer)
                state = task.status.state
                if state == wire.TASK_STATE_COMPLETED:
                    content = "".join(part.text for artifact in task.artifacts for part in artifact.parts if part.WhichOneof("content") == "text")
                    run_context.events.emit(AgentTextEvent(text=content))
                    return AgentResult(status="completed", content=content)
                if state == wire.TASK_STATE_REJECTED and self.is_scope_rejection(task):
                    return AgentResult(status="rejected", content="", metadata=AgentResultMetadata(control_signal="OUT_OF_SCOPE"))
                if state in _TERMINAL or state == wire.TASK_STATE_AUTH_REQUIRED:
                    raise CapabilityError(code="CAPABILITY_REMOTE_TASK_FAILED", message="远程任务执行失败", retryable=False)
                if state == wire.TASK_STATE_INPUT_REQUIRED:
                    raise RemoteInputRequired()
                await asyncio.sleep(self._poll_interval_seconds)
        except RemoteA2AError as error:
            binding = await sequencer.get_remote_agent_event(invocation_id=run_context.invocation_id, event_type="internal.remote_agent.task_bound")
            if binding is None and error.code == "CAPABILITY_REMOTE_AUTH_FAILED":
                raise
            # HTTP 一旦发送后无法用网络异常证明远程未受理。
            raise RemoteExecutionPending(error.code) from None

    @staticmethod
    def is_scope_rejection(task):
        """只信任显式 Profile 控制码；任何可见 Artifact 都拒绝例外。"""
        return (task.status.state == wire.TASK_STATE_REJECTED and not task.artifacts
                and task.metadata.fields["control_signal"].string_value == "OUT_OF_SCOPE")

    @staticmethod
    def _draft(event_type, payload):
        """内部事件只有固定身份字段，不进入公开 SSE 或 Redis。"""
        return RuntimeEventDraft(event_type=event_type, source="runtime.remote_agent", visibility="internal", payload=payload, schema_version=1, durability="durable", created_at=datetime.now(UTC))

    async def close(self) -> None:
        """把连接池所有权交回 Registry 清理组。"""
        await self.client.close()
