"""Stage 1 英文到中文翻译 Child Graph。"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.capabilities.en_to_zh.state import EnglishToChineseState
from agent_runtime.core.errors import ApplicationError
from agent_runtime.graph.child_result import ChildResult
from agent_runtime.runtime.agent_contract import (
    AgentContext,
    AgentTextEvent,
    RunContext,
    TaskInput,
)

EN_TO_ZH_SCOPE_PROMPT = """你是 en_to_zh 的能力边界判断器，只判断当前用户消息能否由英文到中文翻译能力处理。
纯英文内容视为待翻译原文；明确要求把英文内容翻译成中文也属于能力范围。
普通聊天、中文翻英文、其他语言互译、代码生成和其他非翻译任务均不属于能力范围。
你只返回符合 Schema 的判断结果，不回答用户问题，也不输出译文。"""

EN_TO_ZH_SYSTEM_PROMPT = """你是 AgentRuntime 的英文到中文翻译助手。
只翻译当前 HumanMessage 中的英文原文，输出忠实、完整、自然的中文译文。
历史消息仅用于理解代词、语境和术语，不得翻译或复述历史消息。
不得省略、概括、缩写、解释或添加原文没有的信息；最终只输出当前原文的中文译文。"""

class EnglishToChineseError(ApplicationError):
    """英译汉能力输入、边界判断或执行不符合运行约束。"""


class EnglishToChineseScopeDecision(BaseModel):
    """英译汉能力的范围判断结果。"""

    model_config = ConfigDict(extra="forbid")

    can_translate_to_chinese: bool = Field(
        description=(
            "判断当前 HumanMessage 是否属于 Stage 1 允许的英文到中文翻译；true "
            "表示内容是纯英文待译文本或明确的英译汉请求，可以进入翻译图；false "
            "表示普通聊天、中文翻英文、其他语言互译、代码生成或其他非翻译任务，"
            "必须返回 OUT_OF_SCOPE。"
        )
    )


def build_english_to_chinese_graph(
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """构建拥有独立私有状态和 Checkpointer 的英译汉图。"""

    async def translate_current_message(
        state: EnglishToChineseState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        """使用历史辅助语境，并只翻译当前 HumanMessage。"""

        response = await model.ainvoke(
            [SystemMessage(content=EN_TO_ZH_SYSTEM_PROMPT), *state["messages"]],
            config,
        )
        return {
            "messages": [response],
            "draft_translation": str(response.content),
        }

    builder = StateGraph(EnglishToChineseState)
    builder.add_node("translate_current_message", translate_current_message)
    builder.add_edge(START, "translate_current_message")
    builder.add_edge("translate_current_message", END)
    return builder.compile(checkpointer=checkpointer, name="en_to_zh")


class EnglishToChineseCapability:
    """在范围守卫通过后执行可持久化的英译汉 StateGraph。"""

    def __init__(
        self,
        *,
        scope_model: BaseChatModel,
        translation_model: BaseChatModel,
        checkpointer: BaseCheckpointSaver,
    ) -> None:
        """分别绑定范围判断模型、翻译模型和独立 Checkpointer。"""

        self._scope_model = scope_model.with_structured_output(
            EnglishToChineseScopeDecision
        )
        self._graph = build_english_to_chinese_graph(
            model=translation_model,
            checkpointer=checkpointer,
        )

    async def run(
        self,
        *,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> ChildResult:
        """先执行范围守卫，再转发翻译图的消息事件。"""

        messages = list(task_input.messages)
        latest_message = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_message is None:
            raise EnglishToChineseError(
                code="EN_TO_ZH_INVALID_INPUT",
                message="英译汉输入中缺少 HumanMessage",
            )

        scope_request = HumanMessage(
            content=f"待判断的当前用户消息：\n{latest_message.content}"
        )
        try:
            await run_context.cancellation.raise_if_requested()
            output = await self._scope_model.ainvoke(
                [SystemMessage(content=EN_TO_ZH_SCOPE_PROMPT), scope_request],
                agent_context.config,
            )
            decision = EnglishToChineseScopeDecision.model_validate(output)
        except ValidationError as error:
            raise EnglishToChineseError(
                code="EN_TO_ZH_SCOPE_INVALID_OUTPUT",
                message="英译汉范围判断结果不符合 Schema",
            ) from error
        except Exception as error:
            raise EnglishToChineseError(
                code="EN_TO_ZH_SCOPE_CALL_FAILED",
                message="英译汉范围判断模型调用失败",
            ) from error

        if not decision.can_translate_to_chinese:
            return ChildResult(
                status="rejected",
                control_signal="OUT_OF_SCOPE",
            )

        try:
            async for event in self._graph.astream(
                {
                    "messages": messages,
                    "draft_translation": "",
                },
                agent_context.config,
                stream_mode="messages",
            ):
                await run_context.cancellation.raise_if_requested()
                message, _metadata = event
                text = str(message.text)
                if text:
                    run_context.events.emit(AgentTextEvent(text=text))
        except Exception as error:
            raise EnglishToChineseError(
                code="EN_TO_ZH_CALL_FAILED",
                message="英译汉模型调用失败",
            ) from error
        return ChildResult(status="completed", control_signal=None)
