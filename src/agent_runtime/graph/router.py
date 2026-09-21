"""Stage 1 固定能力路由器。"""

import logging
from time import perf_counter

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.state import ParentState

ROUTER_SYSTEM_PROMPT = """你是 AgentRuntime 的意图路由器，只负责选择能力，不回答用户问题。
你必须从本轮给出的候选能力中选择一个最匹配的能力，并返回符合 Schema 的结果。
confidence 表示你对本次能力选择的置信度，范围为 0.0 至 1.0。
不要返回候选列表之外的能力，不要返回 TopN，不要请求用户确认。"""

_CAPABILITY_DESCRIPTIONS = {
    "general_chat": "普通聊天、知识问答和简单咨询；不处理任何翻译任务。",
    "en_to_zh": "仅处理把英文内容忠实、完整地翻译成中文的请求。",
}
logger = logging.getLogger(__name__)


class RouterError(ApplicationError):
    """Router 输入、调用或输出不符合运行约束。"""


class RouterDecision(BaseModel):
    """Router 模型必须返回的最小结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(
        min_length=1,
        description=(
            "路由器从本轮候选列表中选出的唯一能力标识；Stage 1 仅允许 "
            "general_chat 或 en_to_zh。"
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "路由器对 capability_id 选择结果的置信度，取值范围为 0.0 至 1.0；"
            "Stage 1 仅记录和校验该值，不触发中断或人工确认。"
        ),
    )


class StageOneRouter:
    """使用结构化模型在两个固定能力之间进行选择。"""

    def __init__(self, model: BaseChatModel) -> None:
        """绑定 RouterDecision，禁止使用自由文本解析模型输出。"""

        self._model = model.with_structured_output(RouterDecision)

    async def route(
        self,
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, str]:
        """根据最新 HumanMessage 返回 Parent Graph 的路由状态更新。"""

        configurable = config.get("configurable", {})
        log_context = {
            "session_id": configurable.get("thread_id"),
            "message_id": configurable.get("message_id"),
        }
        decision_started_at = perf_counter()
        latest_message = next(
            (
                message
                for message in reversed(state["messages"])
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_message is None:
            log_business_event(
                logger,
                "Router决策失败",
                level=logging.ERROR,
                error_code="ROUTER_INVALID_INPUT",
                duration_ms=round(
                    (perf_counter() - decision_started_at) * 1000,
                    2,
                ),
                **log_context,
            )
            raise RouterError(
                code="ROUTER_INVALID_INPUT",
                message="Parent State 中缺少可路由的 HumanMessage",
            )
        rejected_capability_ids = set(state["rejected_capability_ids"])
        candidates = {
            capability_id: description
            for capability_id, description in _CAPABILITY_DESCRIPTIONS.items()
            if capability_id not in rejected_capability_ids
        }
        if not candidates:
            log_business_event(
                logger,
                "Router决策失败",
                level=logging.ERROR,
                error_code="ROUTER_NO_CANDIDATE",
                rejected_capability_ids=sorted(rejected_capability_ids),
                duration_ms=round(
                    (perf_counter() - decision_started_at) * 1000,
                    2,
                ),
                **log_context,
            )
            raise RouterError(
                code="ROUTER_NO_CANDIDATE",
                message="本轮没有可供路由的能力",
            )
        candidate_text = "\n".join(
            f"- {capability_id}: {description}"
            for capability_id, description in candidates.items()
        )
        request = HumanMessage(
            content=(
                f"本轮候选能力：\n{candidate_text}\n\n"
                f"待路由的用户消息：\n{latest_message.content}"
            )
        )
        log_business_event(
            logger,
            "Router决策开始",
            candidate_capability_ids=sorted(candidates),
            rejected_capability_ids=sorted(rejected_capability_ids),
            **log_context,
        )
        try:
            output = await self._model.ainvoke(
                [SystemMessage(content=ROUTER_SYSTEM_PROMPT), request],
                config,
            )
            decision = RouterDecision.model_validate(output)
        except ValidationError as error:
            log_business_event(
                logger,
                "Router决策失败",
                level=logging.ERROR,
                error_code="ROUTER_INVALID_OUTPUT",
                duration_ms=round(
                    (perf_counter() - decision_started_at) * 1000,
                    2,
                ),
                **log_context,
            )
            raise RouterError(
                code="ROUTER_INVALID_OUTPUT",
                message="路由模型返回的结构化结果不符合 Schema",
            ) from error
        except Exception as error:
            log_business_event(
                logger,
                "Router决策失败",
                level=logging.ERROR,
                error_code="ROUTER_CALL_FAILED",
                error_type=type(error).__name__,
                duration_ms=round(
                    (perf_counter() - decision_started_at) * 1000,
                    2,
                ),
                **log_context,
            )
            raise RouterError(
                code="ROUTER_CALL_FAILED",
                message="路由模型调用失败",
            ) from error
        if decision.capability_id not in candidates:
            log_business_event(
                logger,
                "Router决策失败",
                level=logging.ERROR,
                error_code="ROUTER_INVALID_CAPABILITY",
                capability_id=decision.capability_id,
                duration_ms=round(
                    (perf_counter() - decision_started_at) * 1000,
                    2,
                ),
                **log_context,
            )
            raise RouterError(
                code="ROUTER_INVALID_CAPABILITY",
                message="路由模型选择了本轮候选列表之外的能力",
            )
        log_business_event(
            logger,
            "Router决策完成",
            capability_id=decision.capability_id,
            confidence=decision.confidence,
            duration_ms=round(
                (perf_counter() - decision_started_at) * 1000,
                2,
            ),
            **log_context,
        )
        return {"resolved_capability_id": decision.capability_id}
