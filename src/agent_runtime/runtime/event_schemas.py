"""公开 RuntimeEvent 白名单 Schema、校验与投影。"""

from datetime import datetime
from typing import Annotated, Literal, TypeAlias, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    UUID4,
    ValidationError,
)

from agent_runtime.core.errors import ApplicationError
from agent_runtime.runtime.event_models import RuntimeEvent, RuntimeEventDraft
from agent_runtime.runtime.models import JsonValue

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


class RuntimeEventSchemaError(ApplicationError):
    """事件草稿不符合公开白名单或内部事件边界。"""


class PublicEventProjectionError(ApplicationError):
    """事件不可安全投影为公开 SSE 数据。"""


class _ClosedPayload(BaseModel):
    """拒绝未声明字段的公开事件 payload 基类。"""

    model_config = ConfigDict(extra="forbid")


class RunStartedPayload(_ClosedPayload):
    """Run 首次进入执行态的公开数据。"""

    status: Literal["running"] = Field(
        description="Run 已进入执行态；Stage 2.5 该字段固定为 running。"
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


def _schema_error(message: str) -> RuntimeEventSchemaError:
    """构造不暴露 payload 内容的稳定事件校验错误。"""

    return RuntimeEventSchemaError(
        code="RUNTIME_EVENT_SCHEMA_INVALID",
        message=message,
        status_code=409,
    )
