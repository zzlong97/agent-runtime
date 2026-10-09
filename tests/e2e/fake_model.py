"""Stage 2 浏览器验收使用的确定性 Fake Model。"""

import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr


def _latest_user_content(messages: list[BaseMessage]) -> str:
    """从模型输入中还原当前用户消息。"""

    latest = next(
        message
        for message in reversed(messages)
        if isinstance(message, HumanMessage)
    )
    content = str(latest.content)
    for marker in (
        "待路由的用户消息：\n",
        "待判断的当前用户消息：\n",
    ):
        if marker in content:
            return content.rsplit(marker, maxsplit=1)[1]
    return content


def _is_english_to_chinese(content: str) -> bool:
    """判断浏览器验收输入是否属于英译汉。"""

    normalized = content.casefold()
    if "into english" in normalized or "翻译成英文" in content:
        return False
    if "into chinese" in normalized or "翻译成中文" in content:
        return True
    contains_english = any(
        character.isascii() and character.isalpha() for character in content
    )
    contains_chinese = any(
        "\u4e00" <= character <= "\u9fff" for character in content
    )
    return contains_english and not contains_chinese and "translate" not in normalized


def _is_translation_request(content: str) -> bool:
    """判断浏览器验收输入是否明确要求翻译。"""

    return "translate" in content.casefold() or "翻译" in content


class StageTwoBrowserFakeModel(BaseChatModel):
    """按生产 Prompt 返回确定性路由、范围判断和流式文本。"""

    _tool_call_index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "stage-two-browser-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保留同一模型实例，让结构化输出解析器消费工具调用。"""

        return self

    def _tool_result(self, name: str, arguments: dict[str, object]) -> ChatResult:
        """构造 LangChain 结构化输出所需的工具调用结果。"""

        self._tool_call_index += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": arguments,
                                "id": f"browser-tool-{self._tool_call_index}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        """响应 Router 和两个能力的结构化范围判断。"""

        system_content = str(messages[0].content)
        content = _latest_user_content(messages)
        if "意图路由器" in system_content:
            request = str(messages[-1].content)
            candidates = tuple(
                capability_id
                for capability_id in ("general_chat", "en_to_zh")
                if f"- {capability_id}:" in request
            )
            if len(candidates) == 1:
                capability_id = candidates[0]
            elif _is_english_to_chinese(content):
                capability_id = "en_to_zh"
            else:
                capability_id = "general_chat"
            return self._tool_result(
                "RouterDecision",
                {
                    "capability_id": capability_id,
                    "task_action": "continue",
                    "confidence": 0.99,
                },
            )
        if "general_chat 的能力边界" in system_content:
            return self._tool_result(
                "GeneralChatScopeDecision",
                {"is_translation_request": _is_translation_request(content)},
            )
        if "en_to_zh 的能力边界" in system_content:
            return self._tool_result(
                "EnglishToChineseScopeDecision",
                {"can_translate_to_chinese": _is_english_to_chinese(content)},
            )
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content=self._visible_reply(messages)))
            ]
        )

    def _stream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        """把可见回答拆成两个稳定的 SSE 增量。"""

        reply = self._visible_reply(messages)
        split_at = max(1, len(reply) // 2)
        for delta in (reply[:split_at], reply[split_at:]):
            if delta:
                yield ChatGenerationChunk(message=AIMessageChunk(content=delta))

    async def _astream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        """为 Stop 验收保留可取消窗口，其余回答仍立即流式完成。"""

        content = _latest_user_content(messages)
        if "执行长任务" in content:
            yield ChatGenerationChunk(message=AIMessageChunk(content="部分输出"))
            await asyncio.sleep(60)
            yield ChatGenerationChunk(message=AIMessageChunk(content="不应完成"))
            return

        reply = self._visible_reply(messages)
        split_at = max(1, len(reply) // 2)
        for delta in (reply[:split_at], reply[split_at:]):
            if delta:
                yield ChatGenerationChunk(message=AIMessageChunk(content=delta))

    @staticmethod
    def _visible_reply(messages: list[BaseMessage]) -> str:
        """根据当前能力返回浏览器可断言的固定回答。"""

        system_content = str(messages[0].content)
        content = _latest_user_content(messages)
        if "普通聊天助手" in system_content:
            if "FastAPI" in content:
                return "FastAPI 是一个现代 Python Web 框架。"
            return f"普通回复：{content}"
        if "英文到中文翻译助手" in system_content:
            if "good morning" in content.casefold():
                return "早上好。"
            return f"中文译文：{content}"
        raise AssertionError("浏览器验收 Fake Model 收到了未知的系统提示词")
