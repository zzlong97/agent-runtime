"""Registry 驱动的通用 Capability Invocation Gateway。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from agent_runtime.capabilities.contracts import AgentResult
from agent_runtime.capabilities.execution import (
    CapabilityErrorMapper,
    CapabilityExecutionController,
)
from agent_runtime.capabilities.invocation_service import (
    CapabilityInvocationService,
)
from agent_runtime.capabilities.manifest import validate_capability_id
from agent_runtime.capabilities.operation_ledger import (
    CapabilityOperationGateway,
)
from agent_runtime.capabilities.persistence import (
    CapabilityOperationRepository,
    CapabilityTaskRepository,
)
from agent_runtime.capabilities.registry import CapabilityRegistry
from agent_runtime.capabilities.remote_a2a.capability import RemoteA2ACapability, RemoteExecutionPending
from agent_runtime.capabilities.routing import (
    CapabilityPermissionService,
    CapabilityUnavailableError,
)
from agent_runtime.capabilities.state_scope import ChildThreadIdFactory
from agent_runtime.capabilities.task_service import (
    CapabilityTaskService,
    CheckpointerThreadStore,
)
from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.child_result import (
    CapabilityInvocation as ParentCapabilityInvocation,
    ChildResult,
)
from agent_runtime.graph.config import (
    public_message_id_from_parent_config,
    session_id_from_parent_config,
)
from agent_runtime.graph.context import build_refreshed_child_input
from agent_runtime.graph.state import ParentState
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    AgentTextEvent,
    RunContext,
    TaskInput,
)
from agent_runtime.runtime.sequencer import RunSequencer

logger = logging.getLogger(__name__)

type CancellationProbe = Callable[[], Awaitable[bool]]
type StreamWriterFactory = Callable[[], Callable[[object], None]]
type SequencerFactory = Callable[[UUID, UUID], RunSequencer]
type InvocationServiceFactory = Callable[
    [RunSequencer], CapabilityInvocationService
]
type TaskServiceFactory = Callable[
    [RunSequencer, CapabilityInvocationService], CapabilityTaskService
]
type ChildCheckpointerProvider = Callable[[str], object]
type NowFactory = Callable[[], datetime]


class _OperationReader(Protocol):
    """Gateway 校验拒绝零副作用所需的最小 Operation 查询接口。"""

    async def list_for_invocation(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
    ) -> tuple[object, ...]:
        """返回当前 Invocation 已登记的 Operation。"""


class CapabilityInvocationGateway:
    """串联动态准入、Invocation、Task、State Scope 与标准 Capability。"""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        permission_service: CapabilityPermissionService,
        task_repository: CapabilityTaskRepository,
        operation_repository: CapabilityOperationRepository,
        child_checkpointer_provider: ChildCheckpointerProvider,
        sequencer_factory: SequencerFactory,
        invocation_service_factory: InvocationServiceFactory | None = None,
        task_service_factory: TaskServiceFactory | None = None,
        thread_id_factory: ChildThreadIdFactory | None = None,
        error_mapper: CapabilityErrorMapper | None = None,
        stream_writer_factory: StreamWriterFactory = get_stream_writer,
        now_factory: NowFactory | None = None,
    ) -> None:
        self._registry = registry
        self._permission_service = permission_service
        self._task_repository = task_repository
        self._operation_repository = operation_repository
        self._child_checkpointer_provider = child_checkpointer_provider
        self._sequencer_factory = sequencer_factory
        self._invocation_service_factory = (
            invocation_service_factory
            or (lambda sequencer: CapabilityInvocationService(sequencer=sequencer))
        )
        self._task_service_factory = task_service_factory or (
            lambda sequencer, invocation_service: CapabilityTaskService(
                repository=task_repository,
                sequencer=sequencer,
                invocation_service=invocation_service,
            )
        )
        self._thread_id_factory = thread_id_factory or ChildThreadIdFactory()
        self._error_mapper = error_mapper or CapabilityErrorMapper()
        self._stream_writer_factory = stream_writer_factory
        self._now_factory = now_factory or (lambda: datetime.now(UTC))

    async def invoke(
        self,
        state: ParentState,
        config: RunnableConfig,
        *,
        user_id: str,
        cancellation_probe: CancellationProbe,
    ) -> ParentCapabilityInvocation:
        """只按 Registry Entry 执行一次调用并返回 Parent 控制结果。"""

        started_at = perf_counter()
        capability_id = validate_capability_id(
            state.get("resolved_capability_id")
        )
        metadata_capability_id = config.get("metadata", {}).get(
            "capability_id"
        )
        if (
            metadata_capability_id is not None
            and validate_capability_id(metadata_capability_id) != capability_id
        ):
            raise ValueError(
                "Regenerate Run 与 Parent Fork 的 Capability 标识不一致"
            )
        task_action = self._task_action(state=state, config=config)
        session_id = session_id_from_parent_config(config)
        run_id = self._required_metadata_uuid(config, "run_id")
        response_message_id = (
            public_message_id_from_parent_config(config)
            or self._required_metadata_uuid(config, "response_message_id")
        )
        request_id = self._optional_metadata_uuid(config, "request_id")
        input_message_id = self._optional_metadata_uuid(
            config,
            "input_message_id",
        )
        log_business_event(
            logger,
            "Capability通用调用入口",
            run_id=run_id,
            request_id=request_id,
            session_id=session_id,
            message_id=response_message_id,
            capability_id=capability_id,
            task_action=task_action,
        )

        entry = self._registry.get(capability_id)
        if (
            entry is None
            or entry.manifest is None
            or entry.capability is None
            or entry.concurrency_controller is None
        ):
            raise self._unavailable(capability_id)
        serviceable = {
            projection.capability_id
            for projection in await self._registry.serviceable_router_projections()
        }
        if capability_id not in serviceable:
            raise self._unavailable(capability_id)
        await self._permission_service.require_allowed(
            user_id=user_id,
            capability_id=capability_id,
        )
        child_checkpointer = self._child_checkpointer_provider(capability_id)

        sequencer = self._sequencer_factory(run_id, response_message_id)
        invocation_service = self._invocation_service_factory(sequencer)
        task_service = self._task_service_factory(
            sequencer,
            invocation_service,
        )
        if isinstance(entry.capability, RemoteA2ACapability) and await sequencer.get_remote_rejected_result(capability_id=capability_id):
            return ParentCapabilityInvocation(result=ChildResult(status="rejected", control_signal="OUT_OF_SCOPE"))
        invocation = await invocation_service.start(
            capability_id=capability_id,
            task_action=task_action,
            state_scope=entry.manifest.state_scope,
            created_at=self._now_factory(),
            **({"remote_agent_identity_hash": entry.capability.client.snapshot.fingerprint} if isinstance(entry.capability, RemoteA2ACapability) and entry.capability.client.snapshot is not None else {}),
        )
        persisted_started = getattr(invocation, "started_event", None)
        if persisted_started is not None and not isinstance(entry.capability, RemoteA2ACapability):
            remote_origin = persisted_started.payload.get("remote_agent_identity_hash") or await sequencer.get_remote_agent_event(
                invocation_id=invocation.invocation_id, event_type="internal.remote_agent.task_bound")
            if remote_origin:
                raise RemoteExecutionPending("CAPABILITY_REMOTE_IDENTITY_CHANGED")
        resolution = None
        remote = isinstance(entry.capability, RemoteA2ACapability)
        invocation_closed = False
        try:
            async with entry.concurrency_controller.acquire():
                resolution = await task_service.resolve(
                    manifest=entry.manifest,
                    session_id=session_id,
                    run_id=run_id,
                    invocation_id=invocation.invocation_id,
                    task_action=task_action,
                    updated_at=self._now_factory(),
                )
                thread_id = self._thread_id_factory.build_for_manifest(
                    manifest=entry.manifest,
                    task=resolution.task,
                    run_id=run_id,
                    invocation_id=invocation.invocation_id,
                )
                checkpoint_before = await self._checkpoint_marker(
                    child_checkpointer,
                    thread_id,
                )
                streamed_text: list[str] = []
                writer = self._stream_writer_factory()

                def emit(event: AgentTextEvent) -> None:
                    streamed_text.append(event.text)
                    writer(
                        AIMessageChunk(
                            content=event.text,
                            additional_kwargs={
                                "capability_id": capability_id,
                            },
                        )
                    )

                upstream_cancellation = AgentCancellation(cancellation_probe)
                operation_gateway = CapabilityOperationGateway(
                    run_id=run_id,
                    invocation_id=invocation.invocation_id,
                    task_id=resolution.task.task_id,
                    capability_id=capability_id,
                    repository=self._operation_repository,
                    now_factory=self._now_factory,
                )

                async def execute_capability(
                    cancellation: AgentCancellation,
                ) -> AgentResult:
                    invoke_options = {"sequencer": sequencer} if remote else {}
                    invoke = entry.capability.invoke_bound if remote else entry.capability.invoke
                    result = await invoke(
                        RunContext(
                            run_id=run_id,
                            request_id=request_id,
                            input_message_id=input_message_id,
                            response_message_id=response_message_id,
                            invocation_id=invocation.invocation_id,
                            cancellation=cancellation,
                            events=AgentEventOutlet(emit),
                            operation_gateway=operation_gateway,
                        ),
                        AgentContext(
                            session_id=session_id,
                            capability_id=capability_id,
                            thread_id=thread_id,
                            config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": "",
                                }
                            },
                            task_id=resolution.task.task_id,
                            state_scope=entry.manifest.state_scope,
                            state_schema_version=(
                                resolution.task.state_schema_version
                            ),
                        ),
                        TaskInput(
                            messages=tuple(
                                build_refreshed_child_input(state["messages"])
                            )
                        ),
                        **invoke_options,
                    )
                    return AgentResult.model_validate(result)

                result = await CapabilityExecutionController(
                    capability_id=capability_id,
                    policy=entry.manifest.execution,
                ).execute(
                    execute_capability,
                    upstream_cancellation=upstream_cancellation,
                )

                if result.status == "rejected":
                    checkpoint_after = await self._checkpoint_marker(
                        child_checkpointer,
                        thread_id,
                    )
                    operations = await self._operation_repository.list_for_invocation(
                        run_id=run_id,
                        invocation_id=invocation.invocation_id,
                    )
                    if (
                        streamed_text
                        or (operations and not (remote and resolution.provisional))
                        or checkpoint_after != checkpoint_before
                        or (remote and checkpoint_after is not None)
                    ):
                        raise RuntimeError(
                            "Capability 在 OUT_OF_SCOPE 前产生了公开输出或业务状态"
                        )
                    if resolution.provisional:
                        await task_service.rollback_rejected_new(
                            resolution=resolution,
                            run_id=run_id,
                            invocation_id=invocation.invocation_id,
                            thread_id=thread_id,
                            public_output_emitted=False,
                            checkpoint_store=CheckpointerThreadStore(
                                child_checkpointer
                            ),
                            updated_at=self._now_factory(),
                            **({"remote_binding": (await sequencer.get_remote_agent_event(invocation_id=invocation.invocation_id, event_type="internal.remote_agent.task_bound")).payload} if remote else {}),
                        )
                    else:
                        await invocation_service.complete(
                            invocation_id=invocation.invocation_id,
                            capability_id=capability_id,
                            task_id=resolution.task.task_id,
                            requested_task_action=resolution.requested_action,
                            effective_task_action=resolution.resolved_action,
                            state_scope=entry.manifest.state_scope,
                            state_schema_version=(
                                resolution.task.state_schema_version
                            ),
                            outcome="rejected",
                            created_at=self._now_factory(),
                        )
                    invocation_closed = True
                    return ParentCapabilityInvocation(
                        result=ChildResult(
                            status="rejected",
                            control_signal="OUT_OF_SCOPE",
                        )
                    )

                await task_service.apply_transition(
                    task=resolution.task,
                    task_transition=result.metadata.task_transition,
                    run_id=run_id,
                    invocation_id=invocation.invocation_id,
                    updated_at=self._now_factory(),
                )
                message = AIMessage(
                    content=result.content,
                    id=str(response_message_id),
                    additional_kwargs={
                        "runtime_status": "completed",
                        "capability_id": capability_id,
                    },
                )
                log_business_event(
                    logger,
                    "Capability通用调用出口",
                    run_id=run_id,
                    request_id=request_id,
                    session_id=session_id,
                    message_id=response_message_id,
                    invocation_id=invocation.invocation_id,
                    task_id=resolution.task.task_id,
                    capability_id=capability_id,
                    status="completed",
                    duration_ms=round((perf_counter() - started_at) * 1000, 2),
                )
                return ParentCapabilityInvocation(
                    result=ChildResult(
                        status="completed",
                        control_signal=None,
                    ),
                    message=message,
                )
        except RemoteExecutionPending:
            # open Invocation 和 pending Ledger 是后续恢复的唯一身份，不能关闭。
            raise
        except asyncio.CancelledError:
            if remote:
                raise
            if not invocation_closed:
                await invocation_service.cancel(
                    invocation_id=invocation.invocation_id,
                    capability_id=capability_id,
                    task_id=(
                        None if resolution is None else resolution.task.task_id
                    ),
                    requested_task_action=task_action,
                    effective_task_action=(
                        None if resolution is None else resolution.resolved_action
                    ),
                    state_scope=entry.manifest.state_scope,
                    state_schema_version=(
                        None
                        if resolution is None
                        else resolution.task.state_schema_version
                    ),
                    created_at=self._now_factory(),
                )
            raise
        except Exception as error:
            if remote:
                log_business_event(logger, "远程调用收尾检查", run_id=run_id, invocation_id=invocation.invocation_id,
                    capability_id=capability_id, error_code=getattr(error, "code", None), error_type=type(error).__name__)
                if getattr(error, "code", None) == "CAPABILITY_TIMEOUT":
                    # 先由 Chat 在状态事务中保存 timeout 意图，再发 Cancel；崩溃不得丢失原因。
                    raise RemoteExecutionPending("CAPABILITY_REMOTE_TIMEOUT_UNCONFIRMED") from None
                terminal = await sequencer.get_remote_agent_event(invocation_id=invocation.invocation_id, event_type="internal.remote_agent.task_terminal")
                if terminal is None:
                    operations = await self._operation_repository.list_for_invocation(run_id=run_id, invocation_id=invocation.invocation_id)
                    if operations and not all(operation.status == "failed" for operation in operations):
                        raise RemoteExecutionPending() from None
            public_error = self._error_mapper.map(error)
            if not invocation_closed:
                await invocation_service.fail(
                    invocation_id=invocation.invocation_id,
                    capability_id=capability_id,
                    task_id=(
                        None if resolution is None else resolution.task.task_id
                    ),
                    requested_task_action=task_action,
                    effective_task_action=(
                        None if resolution is None else resolution.resolved_action
                    ),
                    state_scope=entry.manifest.state_scope,
                    state_schema_version=(
                        None
                        if resolution is None
                        else resolution.task.state_schema_version
                    ),
                    error_code=public_error.code,
                    created_at=self._now_factory(),
                )
            log_business_event(
                logger,
                "Capability通用调用失败",
                level=logging.ERROR,
                run_id=run_id,
                request_id=request_id,
                session_id=session_id,
                message_id=response_message_id,
                invocation_id=invocation.invocation_id,
                task_id=None if resolution is None else resolution.task.task_id,
                capability_id=capability_id,
                error_code=public_error.code,
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise public_error from error

    async def is_remote_open(self, *, run_id, response_message_id):
        """识别已受理但尚未绑定的调用；恢复次数上限不能先于该判断。"""
        sequencer = self._sequencer_factory(run_id, response_message_id)
        opened = await sequencer.get_open_capability_invocation()
        if opened is None:
            return False
        if opened.payload.get("remote_agent_identity_hash"):
            return True
        if await sequencer.get_remote_agent_event(invocation_id=UUID(opened.payload["invocation_id"]), event_type="internal.remote_agent.task_bound"):
            return True
        entry = self._registry.get(opened.payload["capability_id"])
        return bool(entry and entry.manifest and entry.manifest.transport.type == "remote_a2a")

    async def confirm_remote_cancellation_for_run(self, *, run_id, response_message_id, task_input):
        """超时、显式取消和删除共用一次有界的远程终态屏障。"""
        sequencer = self._sequencer_factory(run_id, response_message_id)
        opened = await sequencer.get_open_capability_invocation()
        if opened is None:
            return
        capability_id = opened.payload["capability_id"]
        entry = self._registry.get(capability_id)
        remote_evidence = opened.payload.get("remote_agent_identity_hash") or await sequencer.get_remote_agent_event(
            invocation_id=UUID(opened.payload["invocation_id"]), event_type="internal.remote_agent.task_bound")
        if not remote_evidence and (not entry or not entry.manifest or entry.manifest.transport.type != "remote_a2a"):
            return
        if entry is None or not isinstance(entry.capability, RemoteA2ACapability):
            raise RemoteExecutionPending("CAPABILITY_REMOTE_CANCEL_UNCONFIRMED")
        invocation_id = UUID(opened.payload["invocation_id"])
        task = await self._task_repository.get_by_invocation(run_id=run_id, invocation_id=invocation_id)
        if task is None:
            # 尚未创建本地 Task 就不可能发出受信远程请求。
            return
        context = RunContext(run_id=run_id, request_id=None, input_message_id=None,
            response_message_id=response_message_id, invocation_id=invocation_id,
            cancellation=AgentCancellation(_never_cancel), events=AgentEventOutlet(lambda event: None),
            operation_gateway=CapabilityOperationGateway(run_id=run_id, invocation_id=invocation_id,
                task_id=task.task_id, capability_id=capability_id, repository=self._operation_repository))
        await entry.capability.cancel_remote(context, task_input, sequencer=sequencer)

    @staticmethod
    async def _checkpoint_marker(
        child_checkpointer: object,
        thread_id: str,
    ) -> str | None:
        """只读取 Checkpoint 稳定标识，用于拒绝零 State 推进校验。"""

        get_tuple = getattr(child_checkpointer, "aget_tuple", None)
        if not callable(get_tuple):
            raise TypeError("Child Checkpointer 必须实现 aget_tuple")
        checkpoint = await get_tuple(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                }
            }
        )
        if checkpoint is None:
            return None
        configurable = checkpoint.config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id")
        return None if checkpoint_id is None else str(checkpoint_id)

    @staticmethod
    def _task_action(
        *,
        state: ParentState,
        config: RunnableConfig,
    ) -> Literal["continue", "new"]:
        """优先使用受控 Run metadata；连续调用缺省为 continue。"""

        raw_action = config.get("metadata", {}).get("task_action")
        if raw_action is None:
            raw_action = state.get("task_action") or "continue"
        if raw_action not in {"continue", "new"}:
            raise ValueError("Capability task_action 只允许 continue 或 new")
        return cast(Literal["continue", "new"], raw_action)

    @staticmethod
    def _required_metadata_uuid(
        config: RunnableConfig,
        field_name: str,
    ) -> UUID:
        """读取正式 Run 必须携带的稳定 UUID metadata。"""

        value = CapabilityInvocationGateway._optional_metadata_uuid(
            config,
            field_name,
        )
        if value is None:
            raise ValueError(f"Capability 调用缺少 {field_name}")
        return value

    @staticmethod
    def _optional_metadata_uuid(
        config: RunnableConfig,
        field_name: str,
    ) -> UUID | None:
        """读取可选 UUID metadata，并拒绝损坏值。"""

        raw = config.get("metadata", {}).get(field_name)
        if raw is None:
            return None
        try:
            return UUID(str(raw))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Capability 调用的 {field_name} 不是有效 UUID"
            ) from error

    @staticmethod
    def _unavailable(capability_id: str) -> CapabilityUnavailableError:
        """构造不暴露 Registry 内部状态的统一不可用错误。"""

        return CapabilityUnavailableError(
            code="CAPABILITY_UNAVAILABLE",
            message="该能力当前不可用，请稍后重试",
            status_code=503,
            retryable=True,
        )


async def _never_cancel():
    """远程确认不继承已经触发的本地取消标记。"""
    return False
