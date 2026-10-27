"""RA-DEV-01-R01：验证 HTTP 解析异常在 SDK 遥测前完成脱敏。"""

import asyncio
from contextlib import contextmanager
import logging
import traceback

import h11
import httpx
import pytest
from a2a.utils import telemetry

from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError
from tests.capabilities.test_remote_a2a_client import make_client
from tests.fake_a2a import FakeA2A


def _exception_chain_text(error):
    """连 suppress_context 隐藏的链也检查，避免仅固定异常文案却保留敏感原因。"""
    pending, visited, descriptions = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        descriptions.extend(
            (str(current), repr(current), "".join(traceback.format_exception(current)))
        )
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
    return "\n".join(descriptions)


@pytest.fixture
def sdk_telemetry(monkeypatch):
    """保留官方 tracing wrapper，直接检查其实际异常及状态说明出口。"""
    exceptions, statuses = [], []

    class Span:
        def record_exception(self, error):
            exceptions.append(_exception_chain_text(error))

        def set_status(self, *args, **kwargs):
            statuses.append((args, kwargs))

    class Tracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            yield Span()

    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda *args: Tracer())
    return exceptions, statuses


def _raise_http_parse_error(request, *, marker, invalid_header):
    """由实际 h11 解析器生成含原始字节的错误，模拟 httpx 的异常转换。"""
    connection = h11.Connection(h11.CLIENT)
    raw = (
        ("HTTP/1.1 200 OK\r\nInvalid " + marker + "\r\n\r\n")
        if invalid_header
        else marker + "\r\n\r\n"
    )
    connection.receive_data(raw.encode())
    try:
        connection.next_event()
    except h11.RemoteProtocolError as error:
        assert marker in str(error)
        raise httpx.RemoteProtocolError(str(error), request=request) from error
    raise AssertionError("非法字节必须触发实际 HTTP 解析失败")


@pytest.mark.parametrize("operation", ["send", "get", "cancel"])
@pytest.mark.parametrize(
    "failure",
    [
        "invalid_status",
        "invalid_header",
        "connect",
        "read",
        "timeout",
        "body_read",
        "invalid_enum",
        "invalid_json",
    ],
)
def test_http_failures_sanitized_before_sdk_telemetry_and_all_exception_chains(
    operation, failure, sdk_telemetry, caplog
):
    marker = "RA_R01_REMOTE_PRIVATE_BYTES"
    fake = FakeA2A(state="TASK_STATE_WORKING")
    enabled = False
    exceptions, statuses = sdk_telemetry
    caplog.set_level(
        logging.INFO, logger="agent_runtime.capabilities.remote_a2a.client"
    )

    class FailingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError(marker)
            yield b""  # pragma: no cover

    def handler(request):
        if not enabled:
            return fake.handle(request)
        if failure in {"invalid_status", "invalid_header"}:
            _raise_http_parse_error(
                request, marker=marker, invalid_header=failure == "invalid_header"
            )
        if failure in {"connect", "read", "timeout"}:
            error_type = {
                "connect": httpx.ConnectError,
                "read": httpx.ReadError,
                "timeout": httpx.ReadTimeout,
            }[failure]
            raise error_type(marker, request=request) from RuntimeError(marker)
        if failure == "body_read":
            return httpx.Response(200, stream=FailingBody())
        if failure == "invalid_json":
            return httpx.Response(200, text=marker)
        task = {"id": "task-id", "contextId": "context-id", "status": {"state": marker}}
        return httpx.Response(200, json={"task": task} if operation == "send" else task)

    fake.transport = httpx.MockTransport(handler)

    async def run():
        nonlocal enabled
        client = make_client(fake)
        try:
            await client.discover()
            task = await client.send_message(message_id="seed-id", text="种子输入")
            enabled = True
            exceptions.clear()
            statuses.clear()
            caplog.clear()
            with pytest.raises(RemoteA2AError) as caught:
                if operation == "send":
                    await client.send_message(message_id="fault-id", text="当前输入")
                elif operation == "get":
                    await client.get_task(task.id)
                else:
                    await client.cancel_task(task.id)
            protocol_failure = failure in {"invalid_enum", "invalid_json"}
            assert caught.value.code == (
                "CAPABILITY_REMOTE_PROTOCOL_ERROR"
                if protocol_failure
                else "CAPABILITY_REMOTE_UNAVAILABLE"
            )
            assert caught.value.retryable is (not protocol_failure)
            assert exceptions, "必须真实经过官方 SDK 的异常遥测出口"
            descriptions = [
                kwargs["description"]
                for _, kwargs in statuses
                if "description" in kwargs
            ]
            assert descriptions, "必须真实经过官方 SDK 的失败状态说明出口"
            # 在各出口分别断言，防止外层修正文案掩盖 SDK 已经采集的原始错误。
            assert all(marker not in error for error in exceptions), (
                "SDK 异常链或堆栈泄露原始字节"
            )
            assert all(marker not in description for description in descriptions), (
                "SDK 状态说明泄露原始字节"
            )
            assert marker not in _exception_chain_text(caught.value), (
                "调用者异常链或堆栈泄露原始字节"
            )
            assert marker not in caplog.text
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["send", "get", "cancel"])
def test_http_failure_boundary_preserves_cancelled_error(
    operation, sdk_telemetry, caplog
):
    cancellation = asyncio.CancelledError("测试取消")
    fake = FakeA2A(state="TASK_STATE_WORKING")
    enabled = False
    caplog.set_level(
        logging.INFO, logger="agent_runtime.capabilities.remote_a2a.client"
    )

    def handler(request):
        if enabled:
            raise cancellation
        return fake.handle(request)

    fake.transport = httpx.MockTransport(handler)

    async def run():
        nonlocal enabled
        client = make_client(fake)
        try:
            await client.discover()
            task = await client.send_message(message_id="seed-id", text="种子输入")
            enabled = True
            with pytest.raises(asyncio.CancelledError) as caught:
                if operation == "send":
                    await client.send_message(message_id="cancel-id", text="当前输入")
                elif operation == "get":
                    await client.get_task(task.id)
                else:
                    await client.cancel_task(task.id)
            assert caught.value is cancellation
            assert "远程协议请求取消" in caplog.text
        finally:
            await client.close()

    asyncio.run(run())
