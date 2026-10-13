"""S3 集成门禁使用的第三方协议测试 Capability。"""

from langchain_core.messages import HumanMessage

from agent_runtime.capabilities.contracts import (
    AgentResult,
    CapabilityBootstrapContext,
    HealthResult,
)
from agent_runtime.runtime.agent_contract import (
    AgentContext,
    AgentTextEvent,
    RunContext,
    TaskInput,
)

PROTOCOL_CAPABILITY_ID = "weather_lookup"
PROTOCOL_REPLY = "第三方协议能力已完成。"


class ProtocolTestCapability:
    """验证 Runtime 能按统一协议承载非内置 ID。"""

    def __init__(self, bootstrap_context: CapabilityBootstrapContext) -> None:
        """保存经 Registry 校验的只读启动上下文。"""

        self._bootstrap_context = bootstrap_context

    async def initialize(self) -> None:
        """协议测试能力不初始化额外资源。"""

    async def health_check(self) -> HealthResult:
        """返回稳定健康结果，使能力进入 Router 候选。"""

        return HealthResult(
            status="healthy",
            summary_code="PROTOCOL_TEST_READY",
        )

    async def invoke(
        self,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> AgentResult:
        """校验三上下文关联并通过受控出口返回固定测试结果。"""

        if agent_context.capability_id != PROTOCOL_CAPABILITY_ID:
            raise AssertionError("协议测试能力收到错误的 Capability ID")
        if (
            self._bootstrap_context.manifest.capability_id
            != agent_context.capability_id
        ):
            raise AssertionError("Bootstrap Manifest 与调用上下文不一致")
        if run_context.run_id is None or run_context.invocation_id is None:
            raise AssertionError("正式调用必须包含 Run 与 Invocation 标识")
        if agent_context.task_id is None:
            raise AssertionError("正式调用必须解析 Capability Task")
        if not any(
            isinstance(message, HumanMessage) for message in task_input.messages
        ):
            raise AssertionError("协议测试能力输入缺少 HumanMessage")

        run_context.events.emit(AgentTextEvent(text=PROTOCOL_REPLY))
        return AgentResult(status="completed", content=PROTOCOL_REPLY)


def create_capability(
    bootstrap_context: CapabilityBootstrapContext,
) -> ProtocolTestCapability:
    """按 Stage 3 统一工厂契约创建协议测试能力。"""

    return ProtocolTestCapability(bootstrap_context)
