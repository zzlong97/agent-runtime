"""Parent 与 Child 之间的 Stage 1 调用契约。"""

from dataclasses import dataclass
from typing import Literal, Self

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChildResult(BaseModel):
    """只承载 Child 执行结果和控制信号，不承载用户可见文本。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "rejected", "failed"] = Field(
        description=(
            "Child Agent 本轮执行的控制状态；Stage 1 只允许 completed、rejected "
            "或 failed，分别表示成功完成、能力边界拒绝或实际执行失败。"
        )
    )
    control_signal: Literal["OUT_OF_SCOPE"] | None = Field(
        description=(
            "Child Agent 交给 Parent Graph 的控制信号；Stage 1 只允许 "
            "OUT_OF_SCOPE 或 null，rejected 必须搭配 OUT_OF_SCOPE，completed 和 "
            "failed 必须搭配 null。"
        )
    )

    @model_validator(mode="after")
    def validate_status_signal_pair(self) -> Self:
        """拒绝状态和越界信号必须成对出现。"""

        is_out_of_scope = self.control_signal == "OUT_OF_SCOPE"
        if (self.status == "rejected") != is_out_of_scope:
            raise ValueError("rejected 必须且只能与 OUT_OF_SCOPE 控制信号搭配")
        return self


@dataclass(frozen=True, slots=True)
class CapabilityInvocation:
    """将控制结果与可选公共消息分开交给 Parent Graph。"""

    result: ChildResult
    message: AIMessage | None = None
