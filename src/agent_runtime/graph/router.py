"""结构化 Capability 路由器。"""

import logging
from time import perf_counter
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.capabilities.manifest import ManifestCapabilityId
from agent_runtime.capabilities.registry import RouterProjection
from agent_runtime.capabilities.routing import RouterCandidateProvider
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.state import ParentState

ROUTER_SYSTEM_PROMPT = """你是 AgentRuntime 的意图路由器，只负责选择能力，不回答用户问题。
你必须从本轮给出的候选能力中选择一个最匹配的能力，并返回符合 Schema 的结果。
task_action 只能是 continue 或 new；不要选择或编造 task_id。
confidence 表示你对本次能力选择的置信度，范围为 0.0 至 1.0。
不要返回候选列表之外的能力，不要返回 TopN，不要请求用户确认。"""

_LEGACY_PROJECTIONS = (
    RouterProjection(
        capability_id="general_chat",
        name="普通聊天",
        description="普通聊天、知识问答和简单咨询；不处理任何翻译任务。",
        enabled=True,
    ),
    RouterProjection(
        capability_id="en_to_zh",
        name="英译汉",
        description="仅处理把英文内容忠实、完整地翻译成中文的请求。",
        enabled=True,
    ),
)
logger = logging.getLogger(__name__)


class RouterError(ApplicationError):
    """Router 输入、调用或输出不符合运行约束。"""


class RouterDecision(BaseModel):
    """Router 模型必须返回的最小结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    capability_id: ManifestCapabilityId = Field(
        description=(
            "路由器从本轮动态候选列表中选出的唯一能力标识；必须符合 Manifest ID "
            "规则，且不得返回候选列表之外的值。"
        ),
    )
    task_action: Literal["continue", "new"] = Field(
        description=(
            "本轮希望使用的 Capability Task 动作；continue 表示继续当前 Task，new 表示"
            "创建新 Task，Router 不得直接选择或修改 task_id。"
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "路由器对 capability_id 选择结果的置信度，取值范围为 0.0 至 1.0；"
            "Stage 3 仅记录和校验该值，不触发中断或人工确认。"
        ),
    )


class StageOneRouter:
    """兼容既有链路，并可从 Registry 与实时权限读取动态候选。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        candidate_provider: RouterCandidateProvider | None = None,
        user_id: str | None = None,
    ) -> None:
        """绑定关闭的结构化输出，并校验动态候选依赖必须成对提供。"""

        if (candidate_provider is None) != (user_id is None):
            raise ValueError("candidate_provider 与 user_id 必须同时提供")
        self._model = model.with_structured_output(RouterDecision)
        self._candidate_provider = candidate_provider
        self._user_id = user_id

    async def route(
        self,
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, str | None]:
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
        if self._candidate_provider is None:
            candidate_projections = tuple(
                projection
                for projection in _LEGACY_PROJECTIONS
                if projection.capability_id not in rejected_capability_ids
            )
        else:
            assert self._user_id is not None
            candidate_projections = await self._candidate_provider.get_candidates(
                user_id=self._user_id,
                rejected_capability_ids=rejected_capability_ids,
            )
        candidates = {
            projection.capability_id: projection
            for projection in candidate_projections
        }
        if not candidates:
            if self._candidate_provider is not None:
                log_business_event(
                    logger,
                    "Router候选已全部拒绝",
                    rejected_capability_ids=sorted(rejected_capability_ids),
                    status="all_rejected",
                    duration_ms=round(
                        (perf_counter() - decision_started_at) * 1000,
                        2,
                    ),
                    **log_context,
                )
                return {
                    "resolved_capability_id": None,
                    "task_action": None,
                }
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
            (
                f"- {capability_id}: 名称：{projection.name} | "
                f"说明：{projection.description} | enabled={projection.enabled}"
            )
            for capability_id, projection in candidates.items()
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
            task_action=decision.task_action,
            confidence=decision.confidence,
            duration_ms=round(
                (perf_counter() - decision_started_at) * 1000,
                2,
            ),
            **log_context,
        )
        return {
            "resolved_capability_id": decision.capability_id,
            "task_action": decision.task_action,
        }
