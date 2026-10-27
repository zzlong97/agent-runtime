"""独立审核的隔离 PostgreSQL 风险复现；不修改被审实现或原测试。"""

from uuid import uuid4

from tests.integration.test_remote_runtime_postgres import _allow, _configure_runtime, _run


def test_health_detected_identity_change_blocks_cancellation_io(monkeypatch):
    """健康刷新已证实身份替换后，取消不能再向旧 Task 发协议请求。"""
    from agent_runtime import chat
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel

    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")

    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="审核身份刷新探针")
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "recovering"
            registry = service._remote_gateway._registry
            fake.card["version"] = "replacement"
            await registry._health_service._refresh(capability_id, reason="reviewer_identity_changed")
            assert not await registry.serviceable_router_projections()
            start = len(fake.requests)
            await service.cancel_persistent_run(run_id=submitted.run_id)
            await service._run_coordinator.wait_until_idle()
            task_requests = [request.method + " " + request.url.path for request in fake.requests[start:] if "/tasks/" in request.url.path]
            actual_status = (await service._persistent_run_repository.get(submitted.run_id)).status
            # 恢复本探针自身的 Fake 身份，按正式接口清理唯一测试 Session。
            fake.card["version"] = "1.0.0"
            await service.delete_session(session_id=submitted.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
        assert not task_requests, {"actual_status": actual_status, "task_request_methods": [item.split(" ", 1)[0] for item in task_requests]}

    _run(exercise())
