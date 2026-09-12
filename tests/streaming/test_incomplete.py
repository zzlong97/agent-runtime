import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import InMemorySaver


class AcceptingGeneralChatScopeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake-accepting-general-scope"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "GeneralChatScopeDecision",
                                "args": {"is_translation_request": False},
                                "id": "scope-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


class PartiallyFailingResponseModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake-partially-failing-response"

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        raise AssertionError("流式测试不应调用非流式生成")

    def _stream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        yield ChatGenerationChunk(message=AIMessageChunk(content="部分输出"))
        raise RuntimeError("provider disconnected")


def test_partial_model_output_is_persisted_as_incomplete_parent_message() -> None:
    from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.service import SessionStart
    from agent_runtime.streaming.sse import stream_chat_sse

    session_id = UUID("00000000-0000-0000-0000-000000000851")
    response_message_id = UUID("00000000-0000-0000-0000-000000000853")
    now = datetime.now(UTC)
    started = SessionStart(
        session=Session(
            session_id=session_id,
            user_id="configured-user",
            title="触发失败",
            created_at=now,
            updated_at=now,
        ),
        human_message=HumanMessage(
            content="触发失败",
            id="00000000-0000-0000-0000-000000000852",
        ),
    )
    parent_saver = InMemorySaver()
    general_adapter = GeneralChatAdapter(
        capability=GeneralChatCapability(
            scope_model=AcceptingGeneralChatScopeModel(),
            response_model=PartiallyFailingResponseModel(),
            checkpointer=InMemorySaver(),
        )
    )

    async def route(state, config):
        return {"resolved_capability_id": "general_chat"}

    parent = build_parent_graph(
        route=route,
        invoke_capability=general_adapter.invoke,
        checkpointer=parent_saver,
    )

    class FakeSessionService:
        async def prepare_new_session(self, *, content: str):
            return started

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=parent,
        response_message_id_factory=lambda: response_message_id,
    )

    async def exercise():
        turn = await service.prepare_turn(session_id=None, content="触发失败")
        frames = [frame async for frame in stream_chat_sse(service, turn)]
        state = await parent.aget_state(turn.config)
        return frames, state

    frames, state = asyncio.run(exercise())
    event_names = [frame.splitlines()[0] for frame in frames]
    error_data = json.loads(frames[1].splitlines()[1].removeprefix("data: "))

    assert event_names == ["event: message", "event: error", "event: done"]
    assert error_data["code"] == "GENERAL_CHAT_CALL_FAILED"
    assert state.next == ()
    assert [message.content for message in state.values["messages"]] == [
        "触发失败",
        "部分输出",
    ]
    incomplete_message = state.values["messages"][-1]
    assert incomplete_message.id == str(response_message_id)
    assert incomplete_message.additional_kwargs["runtime_status"] == "incomplete"
    assert state.values["completion_status"] is None
