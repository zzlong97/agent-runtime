"""普通Router在线准备工具必须默认无副作用，未知提交不可重送。"""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest


def test_controlled_recheck_uses_fixed_new_id_and_independent_receipt():
    from pathlib import Path
    from uuid import UUID, uuid5
    from tests.e2e import real_router_runtime as runtime, real_router_probe as probe

    assert runtime.REQUEST_ID == "d3e517ba-0747-5446-a0c6-8e061d045f0e"
    assert runtime.REQUEST_ID == str(uuid5(UUID(runtime.BATCH), "full-parameters-observed-recheck"))
    assert probe.REQUEST_ID == runtime.REQUEST_ID
    assert probe.RECEIPT == Path(".dev-team/tasks/RA-INT-02/evidence/普通Router受控复测回执.json")
    assert probe.RECEIPT != Path(".dev-team/tasks/RA-INT-02/evidence/普通Router运行回执.json")


def test_router_observer_preserves_success_result_and_original_arguments(monkeypatch):
    from tests.e2e import real_router_runtime as runtime

    router, state, config = object(), object(), object()
    result = {"resolved_capability_id": "semantic_pattern_mining", "task_action": "new"}
    calls = []

    async def original(actual_router, actual_state, actual_config):
        calls.append((actual_router, actual_state, actual_config))
        return result

    def forbid_log(*args, **kwargs):
        raise AssertionError("成功路径不应产生失败旁路")

    monkeypatch.setattr(runtime, "log_business_event", forbid_log)
    observed = runtime.observe_router_route(original)
    assert asyncio.run(observed(router, state, config)) is result
    assert calls == [(router, state, config)]


@pytest.mark.parametrize("provider_code", ["unsupported_value", "sk-private-code-must-not-leak"])
def test_router_observer_preserves_failure_and_only_logs_safe_metadata(monkeypatch, provider_code):
    from agent_runtime.graph.router import RouterError
    from tests.e2e import real_router_runtime as runtime

    secret = "私人输入和认证秘密不得输出"

    class ProviderFailure(RuntimeError):
        status_code = 400
        code = provider_code

        @property
        def body(self):
            raise AssertionError("不能读取错误正文")

        def __str__(self):
            raise AssertionError("不能读取异常字符串")

    cause = ProviderFailure(secret)
    error = RouterError(code="ROUTER_CALL_FAILED", message=secret)
    error.__cause__ = cause
    calls = []

    async def original(router, state, config):
        calls.append((router, state, config))
        raise error

    captured = []
    monkeypatch.setattr(runtime, "log_business_event", lambda logger, event, **fields: captured.append((event, fields)))
    args = (object(), object(), object())
    try:
        asyncio.run(runtime.observe_router_route(original)(*args))
    except RouterError as actual:
        assert actual is error and actual.__cause__ is cause
    else:
        raise AssertionError("旁路不得吞掉原route异常")
    assert calls == [args]
    event, facts = captured[0]
    assert event == "普通Router失败安全观测"
    assert facts["request_id"] == runtime.REQUEST_ID
    assert facts["error_type"] == "RouterError"
    assert [item["error_type"] for item in facts["cause_chain"]] == ["RouterError", "ProviderFailure"]
    assert facts["cause_chain"][-1]["http_status"] == 400
    assert facts["cause_chain"][-1]["provider_code"] == (provider_code if provider_code == "unsupported_value" else None)
    text = json.dumps(captured, ensure_ascii=False)
    assert secret not in text and "sk-private-code" not in text


def test_router_observer_logging_failure_cannot_replace_original_exception(monkeypatch):
    from tests.e2e import real_router_runtime as runtime

    error = ValueError("原异常")

    async def original(router, state, config):
        raise error

    def failed_logger(*args, **kwargs):
        raise OSError("模拟日志写入失败")

    monkeypatch.setattr(runtime, "log_business_event", failed_logger)
    try:
        asyncio.run(runtime.observe_router_route(original)(object(), object(), object()))
    except ValueError as actual:
        assert actual is error
    else:
        raise AssertionError("应保留原异常")


def test_startup_requires_new_approval_before_loading_settings(monkeypatch):
    from tests.e2e import real_router_runtime as runtime

    monkeypatch.delenv("RA_INT02_APPROVED", raising=False)
    with pytest.raises(RuntimeError, match="批准"):
        asyncio.run(runtime.serve())


def test_real_service_does_not_inject_model_or_demo(monkeypatch, tmp_path):
    from agent_runtime import chat
    from agent_runtime.core.config import Settings
    from tests.e2e.real_router_runtime import open_real_service

    calls = []

    @asynccontextmanager
    async def service(settings, **kwargs):
        calls.append((settings.runtime_demo_mode, kwargs))
        yield object()

    monkeypatch.setattr(chat, "open_chat_service", service)

    async def run():
        async with open_real_service(Settings(_env_file=None), tmp_path):
            pass

    asyncio.run(run())
    assert calls == [(False, {})]


def _client(probe, receipt_path, *, existing=None, timeout=False, remote_healthy=True):
    calls = []
    summary = {"run_id": str(uuid4()), "session_id": str(uuid4()),
               "response_message_id": str(uuid4()), "status": "queued", "recovery_attempts": 0}

    def handle(request):
        calls.append(request.method)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"batch_id": probe.BATCH, "database": probe.DATABASE,
                "user_id": probe.USER, "router_mode": "production",
                "capability_ids": ["general_chat", "en_to_zh"] + (["semantic_pattern_mining"] if remote_healthy else [])})
        if "/requests/" in request.url.path:
            return httpx.Response(200, json={"request_id": probe.REQUEST_ID, **existing}) if existing else httpx.Response(404)
        assert request.method == "POST"
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "sending"
        assert json.loads(request.content)["request_id"] == probe.REQUEST_ID
        if timeout:
            raise httpx.ReadTimeout("模拟响应丢失", request=request)
        return httpx.Response(202, json=summary)

    return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False), calls, summary


def test_probe_finds_existing_run_before_any_post(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    existing = {"run_id": str(uuid4()), "session_id": str(uuid4()), "response_message_id": str(uuid4()), "status": "completed", "recovery_attempts": 0}
    client, calls, _ = _client(probe, path, existing=existing)
    with client:
        result = probe.execute("submit", client, path, approved=False)
    assert result["run"]["run_id"] == existing["run_id"]
    assert "POST" not in calls


def test_probe_requires_approval_and_never_posts_for_status(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    client, calls, _ = _client(probe, path)
    with client:
        assert probe.execute("status", client, path, approved=False)["run"] is None
        with pytest.raises(RuntimeError, match="批准"):
            probe.execute("submit", client, path, approved=False)
    assert "POST" not in calls and not path.exists()


def test_probe_writes_intent_before_single_post_and_blocks_unknown_retry(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    client, calls, _ = _client(probe, path, timeout=True)
    with client:
        with pytest.raises(httpx.ReadTimeout):
            probe.execute("submit", client, path, approved=True)
        with pytest.raises(RuntimeError, match="禁止重送"):
            probe.execute("submit", client, path, approved=True)
    assert calls.count("POST") == 1
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "uncertain" and receipt["request_id"] == probe.REQUEST_ID


def test_probe_success_records_only_summary_and_never_resubmits_deleted_run(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    client, calls, summary = _client(probe, path)
    with client:
        result = probe.execute("submit", client, path, approved=True)
        assert result["run"] == summary
        with pytest.raises(RuntimeError, match="禁止重送"):
            probe.execute("submit", client, path, approved=True)
    assert calls.count("POST") == 1
    assert "content" not in path.read_text(encoding="utf-8")


def test_existing_run_remains_queryable_when_remote_health_is_lost(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    existing = {"run_id": str(uuid4()), "session_id": str(uuid4()), "response_message_id": str(uuid4()),
                "status": "running", "recovery_attempts": 0}
    client, calls, _ = _client(probe, path, existing=existing, remote_healthy=False)
    with client:
        assert probe.execute("status", client, path, approved=False)["run"]["run_id"] == existing["run_id"]
    assert "POST" not in calls


def test_new_submission_requires_all_three_healthy_candidates(tmp_path):
    from tests.e2e import real_router_probe as probe

    path = tmp_path / "receipt.json"
    client, calls, _ = _client(probe, path, remote_healthy=False)
    with client, pytest.raises(RuntimeError, match="候选"):
        probe.execute("submit", client, path, approved=True)
    assert "POST" not in calls and not path.exists()


def test_manifest_copy_rejects_main_source_directory():
    from tests.e2e.real_router_runtime import ROOT, prepare_manifest_directory

    with pytest.raises(ValueError, match="隔离"):
        prepare_manifest_directory(ROOT / "accidental-test")


def test_server_configuration_keeps_daily_business_logging(tmp_path, monkeypatch):
    import logging
    from agent_runtime.core.logging import configure_logging, log_business_event
    from tests.e2e import real_router_runtime as runtime

    package_logger = logging.getLogger("agent_runtime")
    monkeypatch.setattr(package_logger, "handlers", [])
    monkeypatch.setattr(package_logger, "level", package_logger.level)
    monkeypatch.setattr(package_logger, "propagate", package_logger.propagate)
    try:
        configure_logging("INFO", log_dir=tmp_path)
        log_business_event(logging.getLogger("agent_runtime.diagnostic"), "配置前日志检查")
        runtime.build_server(lambda *args: None)
        log_business_event(logging.getLogger("agent_runtime.diagnostic"), "配置后日志检查")
        text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.log"))
        assert "配置前日志检查" in text
        assert "配置后日志检查" in text
    finally:
        for handler in package_logger.handlers:
            handler.close()


@pytest.mark.parametrize("event_problem", [None, "foreign_run", "extra_terminal"])
def test_verification_requires_own_run_unique_terminal_and_remote_public_reply(event_problem):
    from tests.e2e.real_router_probe import BASE, verify

    run = {"run_id": str(uuid4()), "session_id": str(uuid4()), "response_message_id": str(uuid4()),
           "status": "completed", "recovery_attempts": 0}
    reply = {"message_id": run["response_message_id"], "role": "assistant", "content": "授权数据的共性条款结果。",
             "runtime_status": "completed", "capability_id": "semantic_pattern_mining", "feedback": None}
    messages = {"items": [{"message_id": str(uuid4()), "role": "user", "content": "合成测试请求",
                            "runtime_status": None, "capability_id": None, "feedback": None}, reply], "next_before": None}
    events = []
    for seq, (kind, payload) in enumerate([
        ("message.finalized", {"response_message_id": run["response_message_id"], "runtime_status": "completed", "capability_id": "semantic_pattern_mining"}),
        ("run.completed", {"status": "completed"}),
    ], 1):
        events.append({"event_id": str(uuid4()), "run_id": str(uuid4()) if event_problem == "foreign_run" else run["run_id"],
                       "seq": seq, "schema_version": 1, "created_at": "2026-10-29T00:00:00Z", "event_type": kind, "payload": payload})
    if event_problem == "extra_terminal":
        events.append({**events[-1], "event_id": str(uuid4()), "seq": 3, "event_type": "run.cancelled", "payload": {"status": "cancelled"}})

    def handle(request):
        assert request.method == "GET"
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=messages)
        return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    with httpx.Client(transport=httpx.MockTransport(handle), base_url=BASE) as client:
        if event_problem:
            with pytest.raises(RuntimeError):
                verify(client, run)
        else:
            result = verify(client, run)
            assert all(result.values()) and "content" not in json.dumps(result)
