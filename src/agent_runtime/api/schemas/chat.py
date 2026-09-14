"""Stage 2 聊天请求与 SSE 产品协议 Schema。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

type CapabilityId = Literal["general_chat", "en_to_zh"]


class ChatMessageRequest(BaseModel):
    """聊天入口接受的单条用户文本消息。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        description=(
            "当前用户提交的消息正文；Stage 2 必须是去除首尾空白后仍非空的"
            "字符串，并作为本轮 HumanMessage 内容。"
        )
    )

    @field_validator("content")
    @classmethod
    def require_non_blank_content(cls, value: str) -> str:
        """拒绝空字符串或仅包含空白字符的用户消息。"""

        if not value.strip():
            raise ValueError("message.content 必须是非空字符串")
        return value


class ChatCompletionRequest(BaseModel):
    """最小聊天请求，用户身份始终由服务端控制。"""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = Field(
        default=None,
        description=(
            "要继续对话的 Session UUID；省略或传 null 时由服务端创建新 "
            "Session，Stage 2 不接受客户端指定 user_id。"
        ),
    )
    message: ChatMessageRequest = Field(
        description="本轮唯一的用户消息；Stage 2 只接受文本 content。"
    )


class MessageEventData(BaseModel):
    """SSE message 事件的数据字段。"""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(
        description="本轮对话所属的稳定 Session UUID。"
    )
    message_id: UUID = Field(
        description="本轮 AIMessage 的服务端稳定 UUID；同一回复的所有 delta 相同。"
    )
    capability_id: CapabilityId | None = Field(
        description=(
            "生成本次增量的能力标识；Stage 2 只允许 general_chat、"
            "en_to_zh 或尚未确定能力时的 null。"
        )
    )
    delta: str = Field(
        description="本次 SSE message 事件携带的增量文本，不包含内部图事件。"
    )


class ErrorEventData(BaseModel):
    """SSE error 事件的数据字段。"""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(
        description="发生流式运行错误的 Session UUID。"
    )
    code: str = Field(
        min_length=1,
        description="稳定的应用错误码；不得包含内部节点或 checkpoint 信息。",
    )
    message: str = Field(
        min_length=1,
        description="面向客户端的中文错误说明，不暴露敏感异常详情。",
    )
    retryable: bool = Field(
        description="客户端是否可以安全重试本轮请求的布尔标记。"
    )


class DoneEventData(BaseModel):
    """SSE done 事件的数据字段。"""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(
        description="本轮对话所属的稳定 Session UUID。"
    )
    message_id: UUID = Field(
        description=(
            "本轮 AIMessage 的服务端稳定 UUID；与同一回复的 message 事件一致。"
        )
    )
    capability_id: CapabilityId | None = Field(
        description=(
            "本轮最终采用的能力标识；Stage 2 只允许 general_chat、"
            "en_to_zh 或未采用能力时的 null。"
        )
    )
    status: Literal["completed", "unsupported", "stopped", "failed"] = Field(
        description=(
            "本轮最终状态；Stage 2 只允许 completed、unsupported、stopped 或 failed。"
        )
    )
