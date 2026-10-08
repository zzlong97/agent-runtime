"""公开 RuntimeEvent 白名单 Schema、校验与投影。"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Literal, TypeAlias, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    UUID4,
    ValidationError,
    model_validator,
)

from agent_runtime.core.errors import ApplicationError
from agent_runtime.runtime.event_models import RuntimeEvent, RuntimeEventDraft
from agent_runtime.runtime.models import JsonValue

_INTERNAL_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

PublicEventType: TypeAlias = Literal[
    "run.started",
    "message.started",
    "message.delta",
    "message.finalized",
    "interrupt.required",
    "interrupt.resumed",
    "run.completed",
    "run.cancelled",
    "run.failed",
]

PUBLIC_EVENT_DURABILITY: dict[str, str] = {
    "run.started": "durable",
    "message.started": "durable",
    "message.delta": "transient",
    "message.finalized": "durable",
    "interrupt.required": "durable",
    "interrupt.resumed": "durable",
    "run.completed": "durable",
    "run.cancelled": "durable",
    "run.failed": "durable",
}

STATEFUL_PUBLIC_EVENT_TYPES = frozenset(
    {
        "run.started",
        "interrupt.required",
        "interrupt.resumed",
        "run.completed",
        "run.cancelled",
        "run.failed",
    }
)

STATEFUL_INTERNAL_EVENT_TYPES = frozenset(
    {
        "internal.run.cancel_requested",
        "internal.run.recovery_claimed",
        "internal.run.recovery_activated",
    }
)


class RuntimeEventSchemaError(ApplicationError):
    """事件草稿不符合公开白名单或内部事件边界。"""


class PublicEventProjectionError(ApplicationError):
    """事件不可安全投影为公开 SSE 数据。"""


class _ClosedPayload(BaseModel):
    """拒绝未声明字段的公开事件 payload 基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStartedPayload(_ClosedPayload):
    """Run 首次进入执行态的公开数据。"""

    status: Literal["running"] = Field(
        description="Run 已进入执行态；Stage 2.5 该字段固定为 running。"
    )


class RunCancelRequestedPayload(_ClosedPayload):
    """Run 已持久化显式取消意图的内部数据。"""

    status: Literal["cancel_requested"] = Field(
        description=(
            "Run 已进入等待执行器完成取消投影的活动状态；该内部字段在 Stage 2.5 "
            "固定为 cancel_requested，不允许投影到公开 SSE。"
        )
    )


class RunRecoveryClaimedPayload(_ClosedPayload):
    """重启恢复器已原子接管 Run 的内部数据。"""

    status: Literal["recovering"] = Field(
        description=(
            "Run 已进入 Checkpoint 对账恢复态；Stage 2.5 该字段固定为 recovering，"
            "且不得投影到公开 SSE。"
        )
    )
    recovery_attempt: int = Field(
        ge=1,
        le=3,
        description="本次重启接管的恢复次数，只允许 1 到 3，并与 Run 计数原子递增。",
    )


class RunRecoveryActivatedPayload(_ClosedPayload):
    """恢复器完成 Checkpoint 对账并准备继续执行的内部数据。"""

    status: Literal["running"] = Field(
        description=(
            "Run 已完成恢复安全检查并重新进入执行态；Stage 2.5 固定为 running，"
            "且不得投影到公开 SSE。"
        )
    )
    recovery_attempt: int = Field(
        ge=1,
        le=3,
        description="当前 Run 已持久化的恢复次数，用于关联本次重新执行尝试。",
    )


class _InvocationPayloadBase(_ClosedPayload):
    """所有 Invocation durable event 共享的最小关联事实。"""

    invocation_id: UUID4 = Field(
        description="Runtime 为本次调用分配并在完整生命周期内保持稳定的 UUIDv4。"
    )
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="本次 Invocation 调用的严格 Manifest Capability ID。",
    )
    task_id: UUID | None = Field(
        description=(
            "Task 解析后关联的 Capability Task UUID；Task 解析前的 started、失败或"
            "取消事件允许为 null。"
        )
    )
    requested_task_action: Literal["continue", "new"] = Field(
        description="Router 原始请求的 Task 动作，只允许 continue 或 new。"
    )
    effective_task_action: Literal["continue", "new"] | None = Field(
        description=(
            "Task 事务实际采用的动作，只允许 continue 或 new；Task 解析完成前为 null，"
            "并可明确记录 continue 降级为 new。"
        )
    )
    state_scope: Literal["invocation", "run", "session"] = Field(
        description=(
            "本次调用采用的私有状态共享范围，只允许 invocation、run 或 session；"
            "该值来自启动期 Manifest。"
        )
    )
    state_schema_version: str | None = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description=(
            "Task 解析后实际固化并用于 Child namespace 的 State Schema 版本；"
            "Task 解析完成前为 null。"
        ),
    )


class InvocationStartedPayload(_InvocationPayloadBase):
    """Capability Invocation 已进入唯一 open 生命周期的内部数据。"""

    @model_validator(mode="after")
    def validate_before_task_resolution(self) -> InvocationStartedPayload:
        """started 必须准确表达 Task 尚未解析的时间点。"""

        if any(
            value is not None
            for value in (
                self.task_id,
                self.effective_task_action,
                self.state_schema_version,
            )
        ):
            raise ValueError("Invocation started 事件不得包含 Task 解析结果")
        return self


class InvocationCompletedPayload(_InvocationPayloadBase):
    """Capability Invocation 已正常完成或合法拒绝的内部数据。"""

    outcome: Literal["completed", "rejected"] = Field(
        description="调用结果，只允许正常完成 completed 或合法范围拒绝 rejected。"
    )

    @model_validator(mode="after")
    def validate_resolved_task_context(self) -> InvocationCompletedPayload:
        """正常完成和合法拒绝都必须带完整 Task 解析事实。"""

        if any(
            value is None
            for value in (
                self.task_id,
                self.effective_task_action,
                self.state_schema_version,
            )
        ):
            raise ValueError("Invocation completed 事件必须包含完整 Task 解析结果")
        return self


class InvocationFailedPayload(_InvocationPayloadBase):
    """Capability Invocation 因受控执行错误结束的内部数据。"""

    error_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=_INTERNAL_ERROR_CODE_PATTERN,
        description="不含异常详情或业务正文的稳定内部失败码。",
    )

    @model_validator(mode="after")
    def validate_optional_task_context(self) -> InvocationFailedPayload:
        """失败事件的 Task 解析字段只能全部为空或全部存在。"""

        _validate_invocation_task_context(
            task_id=self.task_id,
            effective_task_action=self.effective_task_action,
            state_schema_version=self.state_schema_version,
        )
        return self


class InvocationCancelledPayload(_InvocationPayloadBase):
    """Capability Invocation 因显式 Run Cancel 结束的内部数据。"""

    outcome: Literal["cancelled"] = Field(
        description="调用终态结果；显式取消事件在 Stage 3 固定为 cancelled。"
    )

    @model_validator(mode="after")
    def validate_optional_task_context(self) -> InvocationCancelledPayload:
        """取消事件的 Task 解析字段只能全部为空或全部存在。"""

        _validate_invocation_task_context(
            task_id=self.task_id,
            effective_task_action=self.effective_task_action,
            state_schema_version=self.state_schema_version,
        )
        return self


def _validate_invocation_task_context(
    *,
    task_id: UUID | None,
    effective_task_action: Literal["continue", "new"] | None,
    state_schema_version: str | None,
) -> None:
    """拒绝只写入部分 Task 解析事实的 Invocation 终态事件。"""

    present = (
        task_id is not None,
        effective_task_action is not None,
        state_schema_version is not None,
    )
    if any(present) and not all(present):
        raise ValueError("Invocation 终态的 Task 解析字段必须同时为空或同时存在")


class CapabilityTaskCreatedPayload(_ClosedPayload):
    """Capability Task 已创建的内部数据。"""

    task_id: UUID4 = Field(description="本次创建的 Capability Task UUIDv4。")
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="新 Task 所属的严格 Manifest Capability ID。",
    )
    state_schema_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="创建 Task 时固化的 Capability 私有 State Schema 版本。",
    )


class CapabilityTaskCurrentChangedPayload(_ClosedPayload):
    """Capability current Task 指针已切换的内部数据。"""

    task_id: UUID4 = Field(description="切换后的 current Capability Task UUIDv4。")
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="发生 current Task 切换的严格 Manifest Capability ID。",
    )
    previous_task_id: UUID | None = Field(
        description=(
            "切换前的 current Task UUID；原本没有 current Task 时为 null，用于"
            "崩溃恢复后安全还原 provisional Task 之前的指针。"
        ),
    )


class CapabilityTaskTerminalPayload(_ClosedPayload):
    """Capability Task 已进入唯一终态的内部数据。"""

    task_id: UUID4 = Field(description="进入终态的 Capability Task UUIDv4。")
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="终态 Task 所属的严格 Manifest Capability ID。",
    )
    status: Literal["completed", "failed", "cancelled"] = Field(
        description="Task 唯一终态，只允许 completed、failed 或 cancelled。"
    )


class CapabilityTaskContinueDegradedPayload(_ClosedPayload):
    """continue 因缺少 current Task 原子降级为 new 的内部诊断。"""

    task_id: UUID4 = Field(description="降级后新建的 Capability Task UUIDv4。")
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="发生 continue 降级的严格 Manifest Capability ID。",
    )


class CapabilityTaskRejectedRolledBackPayload(_ClosedPayload):
    """未被接受的 provisional Task 已完成无副作用回滚的内部诊断。"""

    task_id: UUID4 = Field(
        description="已删除但保留内部诊断关联的 provisional Task UUIDv4。"
    )
    capability_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="发生 provisional Task 回滚的严格 Manifest Capability ID。",
    )


INTERNAL_STATE_PAYLOAD_SCHEMA_BY_TYPE: dict[str, type[BaseModel]] = {
    "internal.run.cancel_requested": RunCancelRequestedPayload,
    "internal.run.recovery_claimed": RunRecoveryClaimedPayload,
    "internal.run.recovery_activated": RunRecoveryActivatedPayload,
}

INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_BY_TYPE: dict[str, type[BaseModel]] = {
    "internal.capability.invocation.started": InvocationStartedPayload,
    "internal.capability.invocation.completed": InvocationCompletedPayload,
    "internal.capability.invocation.failed": InvocationFailedPayload,
    "internal.capability.invocation.cancelled": InvocationCancelledPayload,
    "internal.capability.task.created": CapabilityTaskCreatedPayload,
    "internal.capability.task.current_changed": (
        CapabilityTaskCurrentChangedPayload
    ),
    "internal.capability.task.completed": CapabilityTaskTerminalPayload,
    "internal.capability.task.failed": CapabilityTaskTerminalPayload,
    "internal.capability.task.cancelled": CapabilityTaskTerminalPayload,
    "internal.capability.task.continue_degraded_to_new": (
        CapabilityTaskContinueDegradedPayload
    ),
    "internal.capability.task.rejected_rolled_back": (
        CapabilityTaskRejectedRolledBackPayload
    ),
}

CAPABILITY_INVOCATION_EVENT_TYPES = frozenset(
    event_type
    for event_type in INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_BY_TYPE
    if event_type.startswith("internal.capability.invocation.")
)

CAPABILITY_TASK_EVENT_TYPES = frozenset(
    event_type
    for event_type in INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_BY_TYPE
    if event_type.startswith("internal.capability.task.")
)

INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_TYPES = tuple(
    dict.fromkeys(INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_BY_TYPE.values())
)

INTERNAL_PAYLOAD_SCHEMA_BY_TYPE = {
    **INTERNAL_STATE_PAYLOAD_SCHEMA_BY_TYPE,
    **INTERNAL_CAPABILITY_PAYLOAD_SCHEMA_BY_TYPE,
}

INTERNAL_STATE_PAYLOAD_SCHEMA_TYPES = tuple(
    dict.fromkeys(INTERNAL_PAYLOAD_SCHEMA_BY_TYPE.values())
)


class MessageStartedPayload(_ClosedPayload):
    """一次模型回答尝试开始前的公开数据。"""

    response_message_id: UUID = Field(
        description="本 Run 固定复用的服务端响应消息 UUID，不因恢复尝试而变化。"
    )
    attempt: int = Field(
        ge=1,
        description="当前响应生成尝试编号，从 1 开始并在每次恢复生成前递增。",
    )


class MessageDeltaPayload(_ClosedPayload):
    """仅用于实时传输、不写 PostgreSQL 的文本增量。"""

    response_message_id: UUID = Field(
        description="接收该文本增量的稳定响应消息 UUID。"
    )
    attempt: int = Field(
        ge=1,
        description="产生该文本增量的响应生成尝试编号，从 1 开始。",
    )
    delta: str = Field(
        min_length=1,
        description="本次公开流式文本增量；禁止承载 Prompt、State 或工具原始数据。",
    )


class MessageFinalizedPayload(_ClosedPayload):
    """最终公共 AIMessage 已先行持久化的公开通知。"""

    response_message_id: UUID = Field(
        description="已经写入 Parent 公共 messages 的最终响应消息 UUID。"
    )
    runtime_status: Literal[
        "completed", "unsupported", "incomplete", "stopped"
    ] = Field(
        description="公共消息终态，只允许 completed、unsupported、incomplete 或 stopped。"
    )
    capability_id: Literal["general_chat", "en_to_zh"] | None = Field(
        description="实际完成该公共消息的固定能力标识；无能力结果时允许为空。"
    )


class InterruptRequiredPayload(_ClosedPayload):
    """Run 已持久化单个待处理 Interrupt 的公开通知。"""

    interrupt_id: UUID = Field(
        description="当前 Run 唯一待处理 Interrupt 的服务端 UUID。"
    )


class InterruptResumedPayload(_ClosedPayload):
    """同一 Run 已消费审批输入并恢复的公开通知。"""

    interrupt_id: UUID = Field(
        description="本次恢复所消费的原待处理 Interrupt 服务端 UUID。"
    )


class RunCompletedPayload(_ClosedPayload):
    """Run 成功完成的唯一公开终态数据。"""

    status: Literal["completed"] = Field(
        description="Run 成功终态；Stage 2.5 该字段固定为 completed。"
    )


class RunCancelledPayload(_ClosedPayload):
    """Run 完成显式取消的唯一公开终态数据。"""

    status: Literal["cancelled"] = Field(
        description="Run 取消终态；Stage 2.5 该字段固定为 cancelled。"
    )


class RunFailedPayload(_ClosedPayload):
    """Run 执行失败的唯一公开终态数据。"""

    status: Literal["failed"] = Field(
        description="Run 失败终态；Stage 2.5 该字段固定为 failed。"
    )
    code: str = Field(
        min_length=1,
        description="可供客户端稳定判断的公开错误码，不包含内部异常详情。",
    )
    message: str = Field(
        min_length=1,
        description="面向用户的中文错误说明，不包含凭据、请求正文或内部状态。",
    )
    retryable: bool = Field(
        description="客户端是否可在保持业务语义的前提下重新发起请求。"
    )


PUBLIC_PAYLOAD_SCHEMA_BY_TYPE: dict[str, type[BaseModel]] = {
    "run.started": RunStartedPayload,
    "message.started": MessageStartedPayload,
    "message.delta": MessageDeltaPayload,
    "message.finalized": MessageFinalizedPayload,
    "interrupt.required": InterruptRequiredPayload,
    "interrupt.resumed": InterruptResumedPayload,
    "run.completed": RunCompletedPayload,
    "run.cancelled": RunCancelledPayload,
    "run.failed": RunFailedPayload,
}

PUBLIC_PAYLOAD_SCHEMA_TYPES = tuple(PUBLIC_PAYLOAD_SCHEMA_BY_TYPE.values())


class _PublicEventBase(BaseModel):
    """只包含对外协议允许字段的公开事件基类。"""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID4 = Field(
        description="服务端为该事件分配的全局唯一 UUIDv4。"
    )
    run_id: UUID = Field(description="该事件所属持久 Run 的服务端 UUID。")
    seq: int = Field(
        ge=1,
        description="该 Run 内单调递增的持久序号；允许因序号块租约而缺号。",
    )
    schema_version: Literal[1] = Field(
        description="公开 RuntimeEvent Schema 版本；Stage 2.5 固定为 1。"
    )
    created_at: datetime = Field(
        description="事件在服务端生成时的带时区时间。"
    )


class RunStartedEvent(_PublicEventBase):
    event_type: Literal["run.started"] = Field(
        description="公开事件类型，表示 Run 已进入执行态。"
    )
    payload: RunStartedPayload = Field(
        description="Run 进入执行态所需的最小公开数据。"
    )


class MessageStartedEvent(_PublicEventBase):
    event_type: Literal["message.started"] = Field(
        description="公开事件类型，表示一次响应生成尝试已经开始。"
    )
    payload: MessageStartedPayload = Field(
        description="响应消息标识及本次生成尝试编号。"
    )


class MessageDeltaEvent(_PublicEventBase):
    event_type: Literal["message.delta"] = Field(
        description="公开事件类型，表示一个不持久化的实时文本增量。"
    )
    payload: MessageDeltaPayload = Field(
        description="响应消息标识、尝试编号和公开文本增量。"
    )


class MessageFinalizedEvent(_PublicEventBase):
    event_type: Literal["message.finalized"] = Field(
        description="公开事件类型，表示最终公共 AIMessage 已完成持久化。"
    )
    payload: MessageFinalizedPayload = Field(
        description="最终消息标识、公共消息状态及固定能力标识。"
    )


class InterruptRequiredEvent(_PublicEventBase):
    event_type: Literal["interrupt.required"] = Field(
        description="公开事件类型，表示 Run 等待一次人工审批输入。"
    )
    payload: InterruptRequiredPayload = Field(
        description="已持久化的待处理 Interrupt 标识。"
    )


class InterruptResumedEvent(_PublicEventBase):
    event_type: Literal["interrupt.resumed"] = Field(
        description="公开事件类型，表示同一 Run 已从审批中恢复。"
    )
    payload: InterruptResumedPayload = Field(
        description="被消费并用于恢复的 Interrupt 标识。"
    )


class RunCompletedEvent(_PublicEventBase):
    event_type: Literal["run.completed"] = Field(
        description="公开事件类型，表示 Run 已成功进入唯一终态。"
    )
    payload: RunCompletedPayload = Field(
        description="Run 成功终态的最小公开数据。"
    )


class RunCancelledEvent(_PublicEventBase):
    event_type: Literal["run.cancelled"] = Field(
        description="公开事件类型，表示 Run 已完成取消并进入唯一终态。"
    )
    payload: RunCancelledPayload = Field(
        description="Run 取消终态的最小公开数据。"
    )


class RunFailedEvent(_PublicEventBase):
    event_type: Literal["run.failed"] = Field(
        description="公开事件类型，表示 Run 已失败并进入唯一终态。"
    )
    payload: RunFailedPayload = Field(
        description="可安全公开的失败码、中文说明与重试提示。"
    )


PublicRuntimeEvent = Annotated[
    RunStartedEvent
    | MessageStartedEvent
    | MessageDeltaEvent
    | MessageFinalizedEvent
    | InterruptRequiredEvent
    | InterruptResumedEvent
    | RunCompletedEvent
    | RunCancelledEvent
    | RunFailedEvent,
    Field(discriminator="event_type"),
]

PUBLIC_EVENT_SCHEMA_TYPES = (
    RunStartedEvent,
    MessageStartedEvent,
    MessageDeltaEvent,
    MessageFinalizedEvent,
    InterruptRequiredEvent,
    InterruptResumedEvent,
    RunCompletedEvent,
    RunCancelledEvent,
    RunFailedEvent,
)

_PUBLIC_EVENT_ADAPTER = TypeAdapter(PublicRuntimeEvent)


def serialize_event_draft_payload(
    draft: RuntimeEventDraft,
) -> dict[str, JsonValue]:
    """校验事件类型、可见性和耐久性后生成安全 JSON payload。"""

    if draft.visibility == "internal":
        if not draft.event_type.startswith("internal."):
            raise _schema_error("内部事件类型必须使用 internal. 前缀")
        schema_type = INTERNAL_PAYLOAD_SCHEMA_BY_TYPE.get(
            draft.event_type
        )
        if schema_type is not None:
            try:
                payload = schema_type.model_validate(
                    draft.payload.model_dump(mode="json")
                )
            except ValidationError as error:
                raise _schema_error(
                    "内部 RuntimeEvent payload 不符合固定 Schema"
                ) from error
            return cast(
                dict[str, JsonValue],
                payload.model_dump(mode="json"),
            )
        return cast(
            dict[str, JsonValue],
            draft.payload.model_dump(mode="json"),
        )

    schema_type = PUBLIC_PAYLOAD_SCHEMA_BY_TYPE.get(draft.event_type)
    if schema_type is None:
        raise _schema_error("公开事件类型不在 Stage 2.5 白名单中")
    expected_durability = PUBLIC_EVENT_DURABILITY[draft.event_type]
    if draft.durability != expected_durability:
        raise _schema_error("公开事件耐久性与固定事件契约不一致")
    try:
        payload = schema_type.model_validate(
            draft.payload.model_dump(mode="json")
        )
    except ValidationError as error:
        raise _schema_error("公开事件 payload 不符合固定 Schema") from error
    return cast(dict[str, JsonValue], payload.model_dump(mode="json"))


def validate_internal_event_payload(
    *,
    event_type: str,
    payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """重新校验已构造内部事件，防止绕过草稿 Schema 直接落库。"""

    schema_type = INTERNAL_PAYLOAD_SCHEMA_BY_TYPE.get(event_type)
    if schema_type is None:
        return payload
    try:
        validated = schema_type.model_validate(payload)
    except ValidationError as error:
        raise _schema_error("内部 RuntimeEvent payload 不符合固定 Schema") from error
    return cast(dict[str, JsonValue], validated.model_dump(mode="json"))


def project_public_event(event: RuntimeEvent) -> PublicRuntimeEvent:
    """从持久事件投影固定公开字段，并再次执行白名单 Schema 校验。"""

    if event.visibility != "public":
        raise PublicEventProjectionError(
            code="RUNTIME_EVENT_NOT_PUBLIC",
            message="内部 RuntimeEvent 不允许进入公开事件协议",
            status_code=409,
        )
    expected_durability = PUBLIC_EVENT_DURABILITY.get(event.event_type)
    if expected_durability is None or event.durability != expected_durability:
        raise PublicEventProjectionError(
            code="RUNTIME_EVENT_PUBLIC_SCHEMA_INVALID",
            message="RuntimeEvent 不符合公开事件白名单",
            status_code=409,
        )
    try:
        return _PUBLIC_EVENT_ADAPTER.validate_python(
            {
                "event_id": event.event_id,
                "run_id": event.run_id,
                "seq": event.seq,
                "event_type": event.event_type,
                "payload": event.payload,
                "schema_version": event.schema_version,
                "created_at": event.created_at,
            }
        )
    except ValidationError as error:
        raise PublicEventProjectionError(
            code="RUNTIME_EVENT_PUBLIC_SCHEMA_INVALID",
            message="RuntimeEvent 不符合公开事件 Schema",
            status_code=409,
        ) from error


def parse_public_event_json(payload: str | bytes) -> PublicRuntimeEvent:
    """从 Redis 字段解析并重新校验固定公开事件 Schema。"""

    try:
        return _PUBLIC_EVENT_ADAPTER.validate_json(payload)
    except ValidationError as error:
        raise PublicEventProjectionError(
            code="RUNTIME_EVENT_PUBLIC_SCHEMA_INVALID",
            message="RuntimeEvent 不符合公开事件 Schema",
            status_code=409,
        ) from error


def _schema_error(message: str) -> RuntimeEventSchemaError:
    """构造不暴露 payload 内容的稳定事件校验错误。"""

    return RuntimeEventSchemaError(
        code="RUNTIME_EVENT_SCHEMA_INVALID",
        message=message,
        status_code=409,
    )
