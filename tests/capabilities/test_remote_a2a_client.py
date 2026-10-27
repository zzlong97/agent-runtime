"""官方 SDK 与强制文本 Profile 的纯离线互操作。"""

import asyncio
import json

import pytest
from a2a.types import a2a_pb2 as wire

from tests.fake_a2a import FakeA2A, remote_transport


def make_client(fake, **kwargs):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AClient
    from agent_runtime.capabilities.manifest import RemoteA2ATransport

    transport = kwargs.pop("transport", remote_transport())
    return RemoteA2AClient(
        capability_id="remote_test",
        transport=RemoteA2ATransport.model_validate(transport),
        http_transport=fake.transport,
        development_mode=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "state", ["TASK_STATE_COMPLETED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"]
)
@pytest.mark.parametrize("tenant", ["", "tenant-a"])
def test_official_sdk_send_get_cancel_nonblocking_wire(state, tenant):
    fake = FakeA2A(state=state, tenant=tenant)

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            task = await client.send_message(
                message_id="stable-id", text="当前用户输入"
            )
            assert isinstance(task, wire.Task)
            assert task.status.state == wire.TaskState.Value(state)
            assert (await client.get_task(task.id)).id == task.id
            assert (
                await client.cancel_task(task.id)
            ).status.state == wire.TASK_STATE_CANCELED
            payload = json.loads(fake.requests[1].content)
            assert payload["configuration"] == {
                "returnImmediately": True,
                "acceptedOutputModes": ["text/plain"],
            }
            assert payload["message"] == {
                "messageId": "stable-id",
                "role": "ROLE_USER",
                "parts": [{"text": "当前用户输入"}],
            }
            prefix = "/api" + ("/tenant-a" if tenant else "")
            assert [(r.method, r.url.path) for r in fake.requests[1:]] == [
                ("POST", prefix + "/message:send"),
                ("GET", prefix + "/tasks/" + task.id),
                ("POST", prefix + "/tasks/" + task.id + ":cancel"),
            ]
            assert all(r.headers["A2A-Version"] == "1.0" for r in fake.requests)
        finally:
            await client.close()

    asyncio.run(run())


def test_idempotency_conflict_resume_and_acceptance_disconnect():
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A(state="TASK_STATE_WORKING")

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            fake.disconnect_after_accept = True
            with pytest.raises(RemoteA2AError) as caught:
                await client.send_message(message_id="same", text="原输入")
            assert caught.value.code == "CAPABILITY_REMOTE_UNAVAILABLE"
            assert caught.value.retryable is True
            task = await client.send_message(message_id="same", text="原输入")
            assert (
                await client.send_message(message_id="same", text="原输入")
            ).id == task.id
            assert len(fake.tasks) == 1
            with pytest.raises(RemoteA2AError) as conflict:
                await client.send_message(message_id="same", text="不同输入")
            assert conflict.value.code == "CAPABILITY_REMOTE_PROTOCOL_ERROR"
            assert conflict.value.retryable is False
            fake.state = "TASK_STATE_COMPLETED"
            answer = await client.send_message(
                message_id="resume-id",
                text="补充",
                task_id=task.id,
                context_id=task.context_id,
            )
            assert (answer.id, answer.context_id) == (task.id, task.context_id)
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "AUTH_FAILED", False),
        (403, "AUTH_FAILED", False),
        (409, "PROTOCOL_ERROR", False),
        (500, "UNAVAILABLE", True),
        (503, "UNAVAILABLE", True),
        (404, "PROTOCOL_ERROR", False),
    ],
)
def test_http_errors_are_controlled_and_do_not_leak(status, code, retryable, caplog):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            fake.status_code = status
            with pytest.raises(RemoteA2AError) as caught:
                await client.send_message(message_id="id", text="敏感用户正文")
            assert caught.value.code == "CAPABILITY_REMOTE_" + code
            assert caught.value.retryable is retryable
            assert "远程内部正文" not in str(caught.value)
            assert "敏感用户正文" not in caplog.text
            assert "远程内部正文" not in caplog.text
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {
            "message": {
                "messageId": "bare",
                "role": "ROLE_AGENT",
                "parts": [{"text": "不接受裸 Message"}],
            }
        },
        {
            "task": {
                "id": "id",
                "contextId": "ctx",
                "status": {"state": "TASK_STATE_COMPLETED"},
            }
        },
        {
            "task": {
                "id": "id",
                "contextId": "ctx",
                "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
            }
        },
        {"task": {"id": "id", "status": {"state": "TASK_STATE_WORKING"}}},
        {"task": {"id": "id", "contextId": "ctx", "status": {"state": "working"}}},
    ],
)
def test_profile_rejects_unrecoverable_or_invalid_task(payload):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            fake.raw_response = payload
            with pytest.raises(RemoteA2AError, match="远程协议"):
                await client.send_message(message_id="id", text="输入")
        finally:
            await client.close()

    asyncio.run(run())


def test_task_path_is_validated_before_http_request():
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            for task_id in (
                "../admin",
                "x?token=secret",
                "x#fragment",
                "x/y",
                "x%2Fadmin",
            ):
                with pytest.raises(RemoteA2AError):
                    await client.get_task(task_id)
            assert len(fake.requests) == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_invalid_wire_body_is_sanitized_before_sdk_telemetry(monkeypatch):
    from contextlib import contextmanager
    from a2a.utils import telemetry
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    recorded = []

    class Span:
        def set_status(self, *args, **kwargs):
            pass

        def record_exception(self, error):
            recorded.append(str(error))

    class Tracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            yield Span()

    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda *args: Tracer())
    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            fake.raw_response = {
                "task": {"id": "id", "status": {"state": "不允许泄露的远程正文"}}
            }
            with pytest.raises(RemoteA2AError):
                await client.send_message(message_id="id", text="输入")
            assert recorded
            assert all("不允许泄露的远程正文" not in error for error in recorded)
        finally:
            await client.close()

    asyncio.run(run())


def test_business_logs_include_available_message_id_without_text(caplog):
    import logging

    caplog.set_level(
        logging.INFO, logger="agent_runtime.capabilities.remote_a2a.client"
    )
    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            await client.send_message(message_id="correlation-id", text="私密请求文本")
        finally:
            await client.close()

    asyncio.run(run())
    assert '"message_id": "correlation-id"' in caplog.text
    assert "私密请求文本" not in caplog.text


@pytest.mark.parametrize("changed", ["version", "name", "interface"])
def test_discovered_identity_mismatch_blocks_all_task_io_until_original_is_verified(changed):
    """已证实身份改变后，旧快照不能授权任何业务请求；恢复须重新验证原身份。"""
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A(state="TASK_STATE_WORKING")

    async def exercise():
        client = make_client(fake)
        try:
            original = await client.discover()
            task = await client.send_message(message_id="seed", text="初始请求")
            if changed == "interface":
                fake.card["supportedInterfaces"][0]["url"] = "https://agent.example/replacement"
            else:
                fake.card[changed] = "replacement"
            with pytest.raises(RemoteA2AError):
                await client.discover()
            start = len(fake.requests)
            operations = (
                lambda: client.send_message(message_id="later", text="后续请求"),
                lambda: client.get_task(task.id),
                lambda: client.cancel_task(task.id),
            )
            for operation in operations:
                with pytest.raises(RemoteA2AError) as caught:
                    await operation()
                assert caught.value.code == "CAPABILITY_REMOTE_PROTOCOL_ERROR"
            assert fake.requests[start:] == []
            assert client.snapshot == original

            # 仅恢复远程 Card 不等于 Runtime 已重新建立信任。
            fake.card["version"] = "1.0.0"
            fake.card["name"] = "离线测试 Agent"
            fake.card["supportedInterfaces"][0]["url"] = "https://agent.example/api"
            for operation in operations:
                with pytest.raises(RemoteA2AError):
                    await operation()
            assert fake.requests[start:] == []
            fake.status_code = 503
            with pytest.raises(RemoteA2AError):
                await client.discover()
            start = len(fake.requests)
            with pytest.raises(RemoteA2AError):
                await client.get_task(task.id)
            assert fake.requests[start:] == []

            fake.status_code = None
            assert await client.discover() == original
            assert (await client.get_task(task.id)).id == task.id
            assert (await client.cancel_task(task.id)).status.state == wire.TASK_STATE_CANCELED
            assert len(fake.tasks) == 1
        finally:
            await client.close()

    asyncio.run(exercise())
