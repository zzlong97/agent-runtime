"""独立复核 R01：保持真实PG与Fake存活，并在可信重发现后清理测试Session。"""

from uuid import uuid4

import pytest

from tests.integration.test_remote_runtime_postgres import _allow, _configure_runtime, _run


@pytest.mark.parametrize("before_binding", [False, True])
def test_reviewer_identity_fence_preserves_cancellation_evidence(monkeypatch, before_binding):
    """失配前后同一持久调用不得被取消屏障、删除或暂态Health恢复绕过。"""
    from agent_runtime import chat
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel

    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    fake.disconnect_after_accept = before_binding

    async def exercise():
        permissions = await _allow(settings, capability_id)
        try:
            async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
                submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="独立复核身份门禁")
                await service._run_coordinator.wait_until_idle()
                opened = await service._open_invocation_resolver.resolve(run_id=submitted.run_id)
                assert opened and opened.task and len(fake.tasks) == 1
                registry = service._remote_gateway._registry
                client = registry.get(capability_id).capability.client
                trusted_snapshot = client.snapshot
                original_operations = tuple((op.operation_id, op.operation_key, op.status) for op in opened.operations)
                facts = service._runtime_event_repository
                original_bound = await facts.get_remote_agent_event(run_id=submitted.run_id, invocation_id=opened.invocation_id, event_type="internal.remote_agent.task_bound")
                assert (original_bound is None) == before_binding
                try:
                    fake.card["name"] = "替换身份"
                    await registry._health_service._refresh(capability_id, reason="reviewer_r01_mismatch")
                    assert not await registry.serviceable_router_projections()
                    no_io_start = len(fake.requests)
                    await service.cancel_persistent_run(run_id=submitted.run_id)
                    await service._run_coordinator.wait_until_idle()
                    assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancel_requested"
                    preserved = await service._open_invocation_resolver.resolve(run_id=submitted.run_id)
                    assert preserved.invocation_id == opened.invocation_id and preserved.task.task_id == opened.task.task_id
                    assert tuple((op.operation_id, op.operation_key, op.status) for op in preserved.operations) == original_operations
                    assert preserved.started_event.payload == opened.started_event.payload
                    assert not await facts.has_event_type(run_id=submitted.run_id, event_type="message.finalized")
                    assert not await facts.has_event_type(run_id=submitted.run_id, event_type="internal.remote_agent.task_terminal")
                    with pytest.raises(Exception) as conflict:
                        await service.submit_chat_run(request_id=uuid4(), session_id=submitted.session_id, content="仍应占用Session")
                    assert conflict.value.status_code == 409
                    with pytest.raises(Exception) as deletion:
                        await service.delete_session(session_id=submitted.session_id)
                    assert deletion.value.status_code == 409 and deletion.value.retryable
                    assert await service._session_repository.get(submitted.session_id) is not None
                    assert fake.requests[no_io_start:] == []

                    # 只恢复Card及失败重发现均不解锁；仅可信原身份成功重发现后收尾。
                    fake.card["name"] = "离线测试 Agent"
                    task_id = next(iter(fake.tasks))
                    with pytest.raises(RemoteA2AError):
                        await client.get_task(task_id)
                    assert fake.requests[no_io_start:] == []
                    fake.status_code = 503
                    await registry._health_service._refresh(capability_id, reason="reviewer_r01_unavailable")
                    after_failed_discovery = len(fake.requests)
                    with pytest.raises(RemoteA2AError):
                        await client.cancel_task(task_id)
                    assert len(fake.requests) == after_failed_discovery
                    assert client.snapshot == trusted_snapshot
                finally:
                    fake.card["name"] = "离线测试 Agent"
                    fake.status_code = None
                    await registry._health_service._refresh(capability_id, reason="reviewer_r01_trusted_identity_restored")
                    assert await registry.serviceable_router_projections()
                    await service.cancel_persistent_run(run_id=submitted.run_id)
                    await service._run_coordinator.wait_until_idle()
                    assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancelled"
                    assert len(fake.tasks) == fake.business_creations == 1
                    await service.delete_session(session_id=submitted.session_id)
        finally:
            await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)

    _run(exercise())
