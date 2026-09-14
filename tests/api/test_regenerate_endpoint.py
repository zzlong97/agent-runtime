"""重新生成消息 HTTP + SSE 产品接口测试。"""

import asyncio
import json
from uuid import UUID

import httpx


class FakeRegenerationService:
    def __init__(self) -> None:
        self.session_id = UUID("00000000-0000-0000-0000-000000001901")
        self.old_message_id = UUID(
            "00000000-0000-0000-0000-000000001902"
        )
        self.new_message_id = UUID(
            "00000000-0000-0000-0000-000000001903"
        )
        self.calls: list[tuple[UUID, UUID]] = []
        self.error: Exception | None = None

    async def prepare_regeneration(self, *, session_id, message_id):
        from agent_runtime.chat import PreparedChatTurn
        from agent_runtime.runs import ActiveRun

        self.calls.append((session_id, message_id))
        if self.error is not None:
            raise self.error
        return PreparedChatTurn(
            session_id=session_id,
            human_message=None,
            response_message_id=self.new_message_id,
            config={
                "configurable": {
                    "thread_id": str(session_id),
                    "checkpoint_id": "fork-checkpoint",
                    "message_id": str(self.new_message_id),
                }
            },
            active_run=ActiveRun(session_id, self.new_message_id),
            is_regeneration=True,
        )

    async def start_producer(self, turn) -> None:
        from agent_runtime.api.schemas.chat import (
            DoneEventData,
            MessageEventData,
        )
        from agent_runtime.runs import ProductRunEvent

        await turn.active_run.event_queue.put(
            ProductRunEvent(
                name="message",
                data=MessageEventData(
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    capability_id="general_chat",
                    delta="重新生成的回复",
                ),
            )
        )
        await turn.active_run.event_queue.put(
            ProductRunEvent(
                name="done",
                data=DoneEventData(
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    capability_id="general_chat",
                    status="completed",
                ),
            )
        )
        await turn.active_run.event_queue.put(None)
        turn.active_run.terminal_future.set_result(None)

    async def cancel_run(self, run, *, reason):
        return False


def _post(app, session_id: UUID, message_id: UUID, *, json_body=None):
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                (
                    f"/api/v1/chat/sessions/{session_id}/messages/"
                    f"{message_id}/regenerate"
                ),
                json=json_body,
            )

    return asyncio.run(request())


def test_regenerate_endpoint_uses_same_product_sse_with_new_message_id() -> None:
    from agent_runtime.main import create_app

    service = FakeRegenerationService()
    app = create_app(chat_service=service)

    response = _post(app, service.session_id, service.old_message_id)

    frames = response.text.strip().split("\n\n")
    events = [
        (
            frame.splitlines()[0].removeprefix("event: "),
            json.loads(frame.splitlines()[1].removeprefix("data: ")),
        )
        for frame in frames
    ]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert service.calls == [(service.session_id, service.old_message_id)]
    assert [name for name, _data in events] == ["message", "done"]
    assert {data["message_id"] for _name, data in events} == {
        str(service.new_message_id)
    }
    assert events[0][1]["delta"] == "重新生成的回复"
    assert events[1][1]["status"] == "completed"
    assert "checkpoint" not in response.text
    assert "node" not in response.text


def test_regenerate_endpoint_maps_eligibility_error_before_sse() -> None:
    from agent_runtime.regeneration import RegenerationError
    from agent_runtime.main import create_app

    service = FakeRegenerationService()
    service.error = RegenerationError(
        code="MESSAGE_REGENERATE_NOT_ALLOWED",
        message="仅允许重新生成当前活动分支最新的 completed AIMessage",
        status_code=409,
    )
    app = create_app(chat_service=service)

    response = _post(app, service.session_id, service.old_message_id)

    assert response.status_code == 409
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == {
        "detail": {
            "code": "MESSAGE_REGENERATE_NOT_ALLOWED",
            "message": "仅允许重新生成当前活动分支最新的 completed AIMessage",
            "retryable": False,
        }
    }


def test_regenerate_endpoint_does_not_accept_client_capability_as_an_argument() -> None:
    from agent_runtime.main import create_app

    service = FakeRegenerationService()
    app = create_app(chat_service=service)

    response = _post(
        app,
        service.session_id,
        service.old_message_id,
        json_body={"capability_id": "en_to_zh"},
    )

    assert response.status_code == 200
    assert service.calls == [(service.session_id, service.old_message_id)]
    operation = app.openapi()["paths"][
        "/api/v1/chat/sessions/{session_id}/messages/{message_id}/regenerate"
    ]["post"]
    assert "requestBody" not in operation
    assert {
        parameter["name"] for parameter in operation["parameters"]
    } == {"session_id", "message_id"}
