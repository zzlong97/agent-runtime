"""两个内置测试 Capability 的标准 entrypoint 与 AgentResult 契约。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from pydantic import PrivateAttr

from agent_runtime.capabilities.contracts import (
    AgentResult,
    Capability,
    CapabilityBootstrapContext,
)
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    RunContext,
    TaskInput,
)


class _BuiltinCapabilityFakeModel(BaseChatModel):
    """只覆盖两个测试 Capability 的范围判断和可见文本生成。"""

    _tool_call_index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "builtin-capability-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    @staticmethod
    def _latest_content(messages: list[BaseMessage]) -> str:
        latest = next(
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        )
        return str(latest.content).rsplit("待判断的当前用户消息：\n", 1)[-1]

    def _tool_result(self, name: str, arguments: dict[str, object]) -> ChatResult:
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
                                "id": f"tool-{self._tool_call_index}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system_content = str(messages[0].content)
        content = self._latest_content(messages)
        if "general_chat 的能力边界" in system_content:
            return self._tool_result(
                "GeneralChatScopeDecision",
                {"is_translation_request": "translate" in content.casefold()},
            )
        if "en_to_zh 的能力边界" in system_content:
            return self._tool_result(
                "EnglishToChineseScopeDecision",
                {
                    "can_translate_to_chinese": (
                        "good morning" in content.casefold()
                    )
                },
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="unused"))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        system_content = str(messages[0].content)
        reply = (
            "FastAPI 是一个现代 Python Web 框架。"
            if "普通聊天助手" in system_content
            else "早上好。"
        )
        yield ChatGenerationChunk(message=AIMessageChunk(content=reply))


def _manifest(capability_id: str) -> CapabilityManifest:
    """读取仓库内已确认的测试 Capability Manifest。"""

    import yaml

    path = (
        Path(__file__).parents[2]
        / "src"
        / "agent_runtime"
        / "capabilities"
        / capability_id
        / "manifest.yaml"
    )
    return CapabilityManifest.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _bootstrap(capability_id: str) -> CapabilityBootstrapContext:
    """构造不含 Runtime 私有对象的最小 Bootstrap Context。"""

    return CapabilityBootstrapContext(
        manifest=_manifest(capability_id),
        child_checkpointer=MemorySaver(),
        model_factory=lambda: _BuiltinCapabilityFakeModel(),
        capability_config={},
        register_cleanup=lambda _cleanup: None,
    )


async def _invoke(capability: Capability, capability_id: str, content: str):
    """通过标准三上下文执行测试 Capability。"""

    emitted: list[str] = []
    session_id = uuid4()
    task_id = uuid4()
    invocation_id = uuid4()

    async def never_cancelled() -> bool:
        return False

    return emitted, await capability.invoke(
        RunContext(
            run_id=uuid4(),
            request_id=uuid4(),
            input_message_id=uuid4(),
            response_message_id=uuid4(),
            invocation_id=invocation_id,
            cancellation=AgentCancellation(never_cancelled),
            events=AgentEventOutlet(lambda event: emitted.append(event.text)),
        ),
        AgentContext(
            session_id=session_id,
            capability_id=capability_id,
            task_id=task_id,
            state_scope="invocation",
            state_schema_version="1",
            thread_id=(
                f"capability:v1:{session_id}:{capability_id}:schema:1:"
                f"invocation:{invocation_id}"
            ),
            config={
                "configurable": {
                    "thread_id": (
                        f"capability:v1:{session_id}:{capability_id}:schema:1:"
                        f"invocation:{invocation_id}"
                    )
                }
            },
        ),
        TaskInput(messages=(HumanMessage(content=content),)),
    )


@pytest.mark.parametrize(
    ("module_name", "capability_id"),
    [
        ("agent_runtime.capabilities.general_chat.entrypoint", "general_chat"),
        ("agent_runtime.capabilities.en_to_zh.entrypoint", "en_to_zh"),
    ],
)
def test_entrypoint_returns_initialized_standard_capability(
    module_name: str,
    capability_id: str,
) -> None:
    """Manifest entrypoint 必须返回健康且符合统一协议的实例。"""

    async def scenario() -> None:
        module = __import__(module_name, fromlist=["create_capability"])
        capability = module.create_capability(_bootstrap(capability_id))

        assert isinstance(capability, Capability)
        assert await capability.initialize() is None
        health = await capability.health_check()
        assert health.status == "healthy"
        assert health.summary_code == "CAPABILITY_READY"

    asyncio.run(scenario())


def test_general_chat_returns_canonical_agent_result() -> None:
    """普通聊天必须通过 AgentResult 返回规范全文，同时保留实时事件。"""

    async def scenario() -> None:
        from agent_runtime.capabilities.general_chat.entrypoint import (
            create_capability,
        )

        emitted, result = await _invoke(
            create_capability(_bootstrap("general_chat")),
            "general_chat",
            "介绍 FastAPI",
        )

        assert isinstance(result, AgentResult)
        assert result.status == "completed"
        assert result.content == "FastAPI 是一个现代 Python Web 框架。"
        assert "".join(emitted) == result.content

    asyncio.run(scenario())


def test_en_to_zh_returns_canonical_agent_result() -> None:
    """英译汉必须通过 AgentResult 返回完整译文，同时保留实时事件。"""

    async def scenario() -> None:
        from agent_runtime.capabilities.en_to_zh.entrypoint import (
            create_capability,
        )

        emitted, result = await _invoke(
            create_capability(_bootstrap("en_to_zh")),
            "en_to_zh",
            "Good morning",
        )

        assert isinstance(result, AgentResult)
        assert result.status == "completed"
        assert result.content == "早上好。"
        assert "".join(emitted) == result.content

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("capability_id", "content"),
    [
        ("general_chat", 'Translate "hello" into Chinese'),
        ("en_to_zh", "介绍 FastAPI"),
    ],
)
def test_builtin_capability_rejects_without_public_output(
    capability_id: str,
    content: str,
) -> None:
    """范围拒绝必须返回关闭的 OUT_OF_SCOPE 结果且不发送公开文本。"""

    async def scenario() -> None:
        module = __import__(
            f"agent_runtime.capabilities.{capability_id}.entrypoint",
            fromlist=["create_capability"],
        )
        emitted, result = await _invoke(
            module.create_capability(_bootstrap(capability_id)),
            capability_id,
            content,
        )

        assert result == AgentResult(
            status="rejected",
            content="",
            metadata={
                "control_signal": "OUT_OF_SCOPE",
                "task_transition": "keep_active",
            },
        )
        assert emitted == []

    asyncio.run(scenario())
