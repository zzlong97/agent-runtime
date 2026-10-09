"""Stage 1 A～G 主链路与持久化集成验收。"""

import asyncio
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import PrivateAttr

from agent_runtime.capabilities.en_to_zh.adapter import EnglishToChineseAdapter
from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability
from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
from agent_runtime.chat import ChatService
from agent_runtime.core.config import Settings
from agent_runtime.graph.config import child_thread_config, parent_thread_config
from agent_runtime.graph.parent import UNSUPPORTED_REPLY, build_parent_graph
from agent_runtime.graph.router import StageOneRouter
from agent_runtime.main import create_app
from agent_runtime.sessions.models import Session
from agent_runtime.sessions.repository import SessionNotFoundError
from agent_runtime.sessions.service import SessionService
from agent_runtime.streaming.sse import stream_chat_sse


def _latest_user_content(messages: list[BaseMessage]) -> str:
    """从模型调用消息中提取最新用户文本。"""

    latest = next(
        message for message in reversed(messages) if isinstance(message, HumanMessage)
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
    """为验收 Fake Model 提供确定性的英译汉范围判断。"""

    normalized = content.casefold()
    if "into english" in normalized or "翻译成英文" in content:
        return False
    if "into chinese" in normalized or "翻译成中文" in content:
        return True
    contains_english = any(character.isascii() and character.isalpha() for character in content)
    contains_chinese = any("\u4e00" <= character <= "\u9fff" for character in content)
    return contains_english and not contains_chinese and "translate" not in normalized


def _is_translation_request(content: str) -> bool:
    """判断验收输入是否明确要求翻译。"""

    return "translate" in content.casefold() or "翻译" in content


class StageOneAcceptanceFakeModel(BaseChatModel):
    """按真实 Prompt 类型响应并记录调用的确定性 Stage 1 Fake Model。"""

    _route_calls: list[tuple[str, tuple[str, ...], str]] = PrivateAttr(
        default_factory=list
    )
    _general_scope_calls: list[tuple[str, bool]] = PrivateAttr(default_factory=list)
    _translation_scope_calls: list[tuple[str, bool]] = PrivateAttr(
        default_factory=list
    )
    _general_generation_calls: list[list[BaseMessage]] = PrivateAttr(
        default_factory=list
    )
    _translation_generation_calls: list[list[BaseMessage]] = PrivateAttr(
        default_factory=list
    )
    _tool_call_index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "stage-one-acceptance-fake"

    @property
    def route_calls(self) -> list[tuple[str, tuple[str, ...], str]]:
        """返回用户文本、候选能力和选择结果的路由记录。"""

        return self._route_calls

    @property
    def general_scope_calls(self) -> list[tuple[str, bool]]:
        """返回普通聊天范围守卫的判断记录。"""

        return self._general_scope_calls

    @property
    def translation_scope_calls(self) -> list[tuple[str, bool]]:
        """返回英译汉范围守卫的判断记录。"""

        return self._translation_scope_calls

    @property
    def general_generation_calls(self) -> list[list[BaseMessage]]:
        """返回普通聊天模型实际收到的完整消息列表。"""

        return self._general_generation_calls

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保持同一 Fake Model，并由 System Prompt 决定结构化结果类型。"""

        return self

    def _tool_result(self, name: str, arguments: dict[str, object]) -> ChatResult:
        """构造 LangChain 结构化输出解析器可消费的工具调用。"""

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
                                "id": f"acceptance-tool-{self._tool_call_index}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )

    def _route_result(self, messages: list[BaseMessage]) -> ChatResult:
        """只从 Router Prompt 中实际提供的候选能力选择一个。"""

        request = str(messages[-1].content)
        content = _latest_user_content(messages)
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
        self._route_calls.append((content, candidates, capability_id))
        return self._tool_result(
            "RouterDecision",
            {
                "capability_id": capability_id,
                "task_action": "continue",
                "confidence": 0.99,
            },
        )

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        """处理结构化判断，以及未启用 token stream 时的可见回复。"""

        system_content = str(messages[0].content)
        content = _latest_user_content(messages)
        if "意图路由器" in system_content:
            return self._route_result(messages)
        if "general_chat 的能力边界" in system_content:
            rejected = _is_translation_request(content)
            self._general_scope_calls.append((content, rejected))
            return self._tool_result(
                "GeneralChatScopeDecision",
                {"is_translation_request": rejected},
            )
        if "en_to_zh 的能力边界" in system_content:
            accepted = _is_english_to_chinese(content)
            self._translation_scope_calls.append((content, accepted))
            return self._tool_result(
                "EnglishToChineseScopeDecision",
                {"can_translate_to_chinese": accepted},
            )
        reply = self._visible_reply(messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=reply))]
        )

    def _stream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        """把用户可见回复稳定拆成两个 token delta。"""

        reply = self._visible_reply(messages)
        split_at = max(1, len(reply) // 2)
        for delta in (reply[:split_at], reply[split_at:]):
            if delta:
                yield ChatGenerationChunk(message=AIMessageChunk(content=delta))

    def _visible_reply(self, messages: list[BaseMessage]) -> str:
        """根据能力 Prompt 生成固定普通回复或中文译文。"""

        system_content = str(messages[0].content)
        content = _latest_user_content(messages)
        if "普通聊天助手" in system_content:
            self._general_generation_calls.append(list(messages))
            if "FastAPI" in content:
                return "FastAPI 是一个现代 Python Web 框架。"
            if "LangGraph" in content:
                return "LangGraph 用于构建有状态的 Agent 工作流。"
            return f"普通回复：{content}"
        if "英文到中文翻译助手" in system_content:
            self._translation_generation_calls.append(list(messages))
            if "how are you" in content.casefold():
                return "你好吗？"
            if "good morning" in content.casefold():
                return "早上好。"
            return f"中文译文：{content}"
        raise AssertionError("验收 Fake Model 收到了未知的 System Prompt")


class InMemorySessionRepository:
    """为默认验收保存最小 Session 元数据。"""

    def __init__(self) -> None:
        self.sessions: dict[UUID, Session] = {}

    async def add(self, session: Session) -> None:
        self.sessions[session.session_id] = session

    async def get(self, session_id: UUID) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise SessionNotFoundError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            ) from error

    async def touch(
        self,
        *,
        session_id: UUID,
        user_id: str,
        updated_at: datetime,
    ) -> Session:
        session = await self.get(session_id)
        if session.user_id != user_id:
            raise SessionNotFoundError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        touched = replace(session, updated_at=updated_at)
        self.sessions[session_id] = touched
        return touched


class InMemoryParentStateStore:
    """以 InMemorySaver 模拟生产 Parent 首消息持久化顺序。"""

    def __init__(self, checkpointer: InMemorySaver) -> None:
        self._checkpointer = checkpointer

    async def store_initial_human_message(
        self,
        session_id: UUID,
        message: HumanMessage,
    ) -> None:
        version = self._checkpointer.get_next_version(None, None)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"]["messages"] = [message]
        checkpoint["channel_versions"]["messages"] = version
        checkpoint["updated_channels"] = ["messages"]
        await self._checkpointer.aput(
            {
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_ns": "",
                }
            },
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            {"messages": version},
        )


@dataclass(slots=True)
class InMemoryRuntime:
    """保存默认端到端验收需要观察的真实运行组件。"""

    app: object
    model: StageOneAcceptanceFakeModel
    parent: object
    parent_saver: InMemorySaver
    general_saver: InMemorySaver
    translation_saver: InMemorySaver


def _build_in_memory_runtime() -> InMemoryRuntime:
    """按生产拓扑组装仅替换持久化介质和模型的 Stage 1 Runtime。"""

    settings = Settings(
        local_user_id="stage-one-acceptance-user",
        dashscope_api_key=None,
        llm_base_url=None,
        llm_model=None,
        _env_file=None,
    )
    model = StageOneAcceptanceFakeModel()
    parent_saver = InMemorySaver()
    general_saver = InMemorySaver()
    translation_saver = InMemorySaver()
    router = StageOneRouter(model)
    general_chat = GeneralChatAdapter(
        capability=GeneralChatCapability(
            scope_model=model,
            response_model=model,
            checkpointer=general_saver,
        )
    )
    en_to_zh = EnglishToChineseAdapter(
        capability=EnglishToChineseCapability(
            scope_model=model,
            translation_model=model,
            checkpointer=translation_saver,
        )
    )

    async def invoke_capability(state, config):
        """保持与生产代码相同的两个固定能力分发规则。"""

        if state["resolved_capability_id"] == "general_chat":
            return await general_chat.invoke(state, config)
        return await en_to_zh.invoke(state, config)

    parent = build_parent_graph(
        route=router.route,
        invoke_capability=invoke_capability,
        checkpointer=parent_saver,
    )
    repository = InMemorySessionRepository()
    session_service = SessionService(
        settings=settings,
        session_repository=repository,
        parent_state_store=InMemoryParentStateStore(parent_saver),
    )
    chat_service = ChatService(
        settings=settings,
        session_repository=repository,
        session_service=session_service,
        parent_graph=parent,
    )
    return InMemoryRuntime(
        app=create_app(settings, chat_service=chat_service),
        model=model,
        parent=parent,
        parent_saver=parent_saver,
        general_saver=general_saver,
        translation_saver=translation_saver,
    )


def _parse_sse(response: httpx.Response) -> list[tuple[str, dict[str, object]]]:
    """把产品 SSE 文本还原为便于验收的事件列表。"""

    events: list[tuple[str, dict[str, object]]] = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append(
            (
                lines[0].removeprefix("event: "),
                json.loads(lines[1].removeprefix("data: ")),
            )
        )
    return events


def _event_reply(events: list[tuple[str, dict[str, object]]]) -> str:
    """聚合一轮所有 message 事件的文本增量。"""

    return "".join(
        str(data["delta"])
        for event_name, data in events
        if event_name == "message"
    )


async def _post_message(
    client: httpx.AsyncClient,
    content: str,
    *,
    session_id: UUID | None = None,
) -> list[tuple[str, dict[str, object]]]:
    """直接验证 Stage 1 图与旧产品事件适配，不绑定 S2.5 Run API。"""

    transport = client._transport
    app = transport.app
    service = app.state.chat_service
    turn = await service.prepare_turn(session_id=session_id, content=content)
    response_text = "".join(
        [frame async for frame in stream_chat_sse(service, turn)]
    )
    return _parse_sse(httpx.Response(200, text=response_text))


def _checkpoint_messages(
    saver: InMemorySaver,
    session_id: UUID,
    capability_id: str,
) -> list[BaseMessage] | None:
    """读取一个 Child thread 的派生消息快照。"""

    checkpoint = saver.get_tuple(child_thread_config(session_id, capability_id))
    if checkpoint is None:
        return None
    return list(checkpoint.checkpoint["channel_values"]["messages"])


def test_cases_a_to_d_and_g_cover_routing_continuation_and_zero_side_effects() -> None:
    """验证普通聊天、双向切换、翻译续用及拒绝零副作用。"""

    runtime = _build_in_memory_runtime()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://stage-one.test",
        ) as client:
            case_a = await _post_message(client, "介绍一下 LangGraph")
            session_id = UUID(str(case_a[0][1]["session_id"]))
            assert _event_reply(case_a) == "LangGraph 用于构建有状态的 Agent 工作流。"
            assert case_a[-1][1]["status"] == "completed"
            assert runtime.model.route_calls[-1][2] == "general_chat"

            general_before_rejection = _checkpoint_messages(
                runtime.general_saver,
                session_id,
                "general_chat",
            )
            route_count_before_switch = len(runtime.model.route_calls)
            case_b = await _post_message(
                client,
                'Translate "How are you?" into Chinese',
                session_id=session_id,
            )
            general_after_rejection = _checkpoint_messages(
                runtime.general_saver,
                session_id,
                "general_chat",
            )
            assert _event_reply(case_b) == "你好吗？"
            assert [event_name for event_name, _data in case_b].count("message") == 2
            assert len(runtime.model.route_calls) == route_count_before_switch + 1
            assert runtime.model.route_calls[-1][1:] == (("en_to_zh",), "en_to_zh")
            assert general_after_rejection == general_before_rejection
            parent_after_case_b = await runtime.parent.aget_state(
                parent_thread_config(session_id)
            )
            assert [
                message.content for message in parent_after_case_b.values["messages"]
            ].count('Translate "How are you?" into Chinese') == 1

            route_count_before_continuation = len(runtime.model.route_calls)
            case_c = await _post_message(
                client,
                "Good morning",
                session_id=session_id,
            )
            assert _event_reply(case_c) == "早上好。"
            assert len(runtime.model.route_calls) == route_count_before_continuation
            assert runtime.model.translation_scope_calls[-1] == ("Good morning", True)

            translation_before_rejection = _checkpoint_messages(
                runtime.translation_saver,
                session_id,
                "en_to_zh",
            )
            case_d = await _post_message(
                client,
                "帮我介绍一下 FastAPI",
                session_id=session_id,
            )
            translation_after_rejection = _checkpoint_messages(
                runtime.translation_saver,
                session_id,
                "en_to_zh",
            )
            assert _event_reply(case_d) == "FastAPI 是一个现代 Python Web 框架。"
            assert runtime.model.route_calls[-1][1:] == (
                ("general_chat",),
                "general_chat",
            )
            assert translation_after_rejection == translation_before_rejection
            parent_after_case_d = await runtime.parent.aget_state(
                parent_thread_config(session_id)
            )
            assert parent_after_case_d.values["resolved_capability_id"] == "general_chat"
            assert len(parent_after_case_d.values["messages"]) == 8

    asyncio.run(exercise())


def test_case_e_returns_unsupported_after_each_capability_rejects_once() -> None:
    """验证全能力拒绝只产生固定 unsupported 回复而不产生 error。"""

    runtime = _build_in_memory_runtime()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://stage-one.test",
        ) as client:
            events = await _post_message(client, "把“你好”翻译成英文")

        session_id = UUID(str(events[0][1]["session_id"]))
        assert [event_name for event_name, _data in events] == ["message", "done"]
        assert _event_reply(events) == UNSUPPORTED_REPLY
        assert events[-1][1]["status"] == "unsupported"
        assert [call[2] for call in runtime.model.route_calls] == [
            "general_chat",
            "en_to_zh",
        ]
        assert runtime.model.general_scope_calls == [("把“你好”翻译成英文", True)]
        assert runtime.model.translation_scope_calls == [
            ("把“你好”翻译成英文", False)
        ]
        assert _checkpoint_messages(
            runtime.general_saver,
            session_id,
            "general_chat",
        ) is None
        assert _checkpoint_messages(
            runtime.translation_saver,
            session_id,
            "en_to_zh",
        ) is None
        parent_state = await runtime.parent.aget_state(parent_thread_config(session_id))
        assert [message.content for message in parent_state.values["messages"]] == [
            "把“你好”翻译成英文",
            UNSUPPORTED_REPLY,
        ]

    asyncio.run(exercise())


def test_case_f_trims_eleventh_child_input_but_keeps_full_parent_history() -> None:
    """验证第十一轮只向 Child 传最近五轮，而 Parent 保留十一轮。"""

    runtime = _build_in_memory_runtime()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=runtime.app)
        session_id: UUID | None = None
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://stage-one.test",
        ) as client:
            for round_number in range(1, 12):
                events = await _post_message(
                    client,
                    f"第 {round_number} 轮",
                    session_id=session_id,
                )
                if session_id is None:
                    session_id = UUID(str(events[0][1]["session_id"]))
                assert events[-1][1]["status"] == "completed"

        assert session_id is not None
        assert len(runtime.model.route_calls) == 1
        tenth_input = [
            message.content
            for message in runtime.model.general_generation_calls[9]
            if not isinstance(message, SystemMessage)
        ]
        eleventh_input = [
            message.content
            for message in runtime.model.general_generation_calls[10]
            if not isinstance(message, SystemMessage)
        ]
        assert len(tenth_input) == 19
        assert tenth_input[0] == "第 1 轮"
        assert tenth_input[-1] == "第 10 轮"
        assert len(eleventh_input) == 11
        assert eleventh_input[0] == "第 6 轮"
        assert eleventh_input[-1] == "第 11 轮"

        parent_state = await runtime.parent.aget_state(parent_thread_config(session_id))
        assert len(parent_state.values["messages"]) == 22
        assert parent_state.values["messages"][0].content == "第 1 轮"
        assert parent_state.values["messages"][-1].content == "普通回复：第 11 轮"

    asyncio.run(exercise())


def test_default_acceptance_needs_no_bailian_configuration() -> None:
    """验证默认验收不读取 `.env` 中的真实模型配置。"""

    runtime = _build_in_memory_runtime()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://stage-one.test",
        ) as client:
            events = await _post_message(client, "介绍一下 LangGraph")
        assert events[-1][1]["status"] == "completed"

    asyncio.run(exercise())


def _run_on_psycopg_compatible_loop(coroutine) -> None:
    """在 Windows 上使用 psycopg 支持的 SelectorEventLoop。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_postgres_restart_restores_session_parent_and_isolated_children() -> None:
    """验证 HTTP 会话重启后续用当前能力并恢复三类独立状态。"""

    from agent_runtime.chat import open_chat_service
    from agent_runtime.persistence.database import open_database_connection

    settings = Settings()
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            first_model = StageOneAcceptanceFakeModel()
            async with open_chat_service(settings, model=first_model) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://stage-one.test",
                ) as client:
                    first_events = await _post_message(client, "介绍一下 LangGraph")
                session_id = UUID(str(first_events[0][1]["session_id"]))
                assert first_model.route_calls[-1][2] == "general_chat"

            restarted_model = StageOneAcceptanceFakeModel()
            async with open_chat_service(settings, model=restarted_model) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://stage-one.test",
                ) as client:
                    continued_events = await _post_message(
                        client,
                        "再介绍一下 LangGraph",
                        session_id=session_id,
                    )
                    switched_events = await _post_message(
                        client,
                        'Translate "How are you?" into Chinese',
                        session_id=session_id,
                    )
                assert _event_reply(continued_events).startswith("LangGraph")
                assert _event_reply(switched_events) == "你好吗？"
                assert [call[2] for call in restarted_model.route_calls] == ["en_to_zh"]

            assert session_id is not None
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as checkpointer:
                parent_checkpoint = await checkpointer.aget_tuple(
                    parent_thread_config(session_id)
                )
                general_checkpoint = await checkpointer.aget_tuple(
                    child_thread_config(session_id, "general_chat")
                )
                translation_checkpoint = await checkpointer.aget_tuple(
                    child_thread_config(session_id, "en_to_zh")
                )

                assert parent_checkpoint is not None
                assert general_checkpoint is not None
                assert translation_checkpoint is not None
                parent_values = parent_checkpoint.checkpoint["channel_values"]
                general_values = general_checkpoint.checkpoint["channel_values"]
                translation_values = translation_checkpoint.checkpoint["channel_values"]
                assert parent_values["resolved_capability_id"] == "en_to_zh"
                assert [message.content for message in parent_values["messages"]] == [
                    "介绍一下 LangGraph",
                    "LangGraph 用于构建有状态的 Agent 工作流。",
                    "再介绍一下 LangGraph",
                    "LangGraph 用于构建有状态的 Agent 工作流。",
                    'Translate "How are you?" into Chinese',
                    "你好吗？",
                ]
                assert [message.content for message in general_values["messages"]] == [
                    "介绍一下 LangGraph",
                    "LangGraph 用于构建有状态的 Agent 工作流。",
                    "再介绍一下 LangGraph",
                    "LangGraph 用于构建有状态的 Agent 工作流。",
                ]
                assert [
                    message.content for message in translation_values["messages"]
                ] == [message.content for message in parent_values["messages"]]
                assert "draft_translation" not in parent_values
                assert "draft_translation" not in general_values
                assert translation_values["draft_translation"] == "你好吗？"
        finally:
            if session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    await checkpointer.adelete_thread(str(session_id))
                    await checkpointer.adelete_thread(f"{session_id}:general_chat")
                    await checkpointer.adelete_thread(f"{session_id}:en_to_zh")

    _run_on_psycopg_compatible_loop(exercise())
