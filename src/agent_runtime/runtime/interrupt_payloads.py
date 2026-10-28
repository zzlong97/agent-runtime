"""确认及自由文本补参的关闭公开契约与持久目标校验。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, TypeAdapter, ValidationError

from agent_runtime.core.errors import ApplicationError


class ConfirmationInterruptPayload(BaseModel):
    """只公开人工确认的展示文本。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["confirmation"] = Field(description="固定为 confirmation；仅接受严格布尔 approved 恢复同一 Run。")
    prompt: str = Field(min_length=1, max_length=4000, pattern=r"\S", strict=True,
        description="供用户确认的非空提示，至少含一个非空白字符，最多4000字符，不包含内部身份。")


class TextInterruptPayload(BaseModel):
    """只公开自由文本补参的展示文本。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["text"] = Field(description="固定为 text；仅接受非空字符串 answer 补参并恢复同一 Run。")
    prompt: str = Field(min_length=1, max_length=4000, pattern=r"\S", strict=True,
        description="要求用户补充文本的提示，至少含一个非空白字符，最多4000字符，不包含远程Task或Checkpoint。")


class ConfirmationResumePayload(BaseModel):
    """确认回答禁止字符串或数字到布尔值的隐式转换。"""

    model_config = ConfigDict(extra="forbid")
    approved: StrictBool = Field(description="用户对 confirmation 中断的确认或拒绝，只允许JSON布尔true/false；沿用同一Run且不新增HumanMessage。")


class TextResumePayload(BaseModel):
    """自由文本只检查类型及非空边界，保留用户原文。"""

    model_config = ConfigDict(extra="forbid")
    answer: StrictStr = Field(min_length=1, pattern=r"\S",
        description="text 中断的补参答案；只允许非空且非纯空白字符串，不额外限制业务长度；沿用同一Run且不新增HumanMessage。")


PublicInterruptPayload = Annotated[ConfirmationInterruptPayload | TextInterruptPayload, Field(discriminator="kind")]
ResumePayload = ConfirmationResumePayload | TextResumePayload
_PUBLIC_INTERRUPT = TypeAdapter(PublicInterruptPayload)


def project_interrupt_payload(payload):
    """只投影kind/prompt；旧单prompt持久记录兼容为confirmation。"""
    try:
        return _PUBLIC_INTERRUPT.validate_python({"kind": payload.get("kind", "confirmation"), "prompt": payload.get("prompt")}).model_dump(mode="json")
    except (ValidationError, AttributeError):
        raise ApplicationError(code="INTERRUPT_PAYLOAD_INVALID", message="中断提示不符合公开契约", status_code=409) from None


def validate_resume_payload(interrupt_payload, resume_payload):
    """在消费事务内按持久kind选择唯一类型，非法载荷不改变中断或事件。"""
    kind = project_interrupt_payload(interrupt_payload)["kind"]
    model = ConfirmationResumePayload if kind == "confirmation" else TextResumePayload
    try:
        model.model_validate(resume_payload)
    except ValidationError:
        raise ApplicationError(code="INTERRUPT_RESUME_INPUT_INVALID", message="恢复输入与当前中断类型不匹配", status_code=422) from None
