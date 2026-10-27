"""隔离 PostgreSQL 与独立 Fake A2A 的真实 Runtime 重建探针。"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="显式启用隔离 PostgreSQL 探针")]


def _configure_runtime(monkeypatch, *, state="TASK_STATE_COMPLETED", timeout=5):
    """只替换网络/路由决策输入；执行、持久化和删除使用正式 Runtime。"""
    from agent_runtime import chat
    from agent_runtime.capabilities.source import CapabilityManifestDocument
    from agent_runtime.core.config import Settings
    from tests.capabilities.test_manifest import _valid_manifest
    from tests.fake_a2a import FakeA2A, remote_transport
    fake = FakeA2A(state=state)
    raw = _valid_manifest()
    raw.update(transport=remote_transport(), side_effect_policy="idempotent")
    fake.manifest = raw
    raw["execution"] = {"timeout_seconds": timeout, "cancel_grace_seconds": 0.01}
    capability_id = raw["capability_id"]
    monkeypatch.setattr(chat.LocalYamlCapabilitySource, "load", lambda self: [CapabilityManifestDocument(source=Path("remote/manifest.yaml"), raw_document=raw, error=None)])
    adapter_type = chat.RemoteA2ACapability
    monkeypatch.setattr(chat, "RemoteA2ACapability", lambda **kwargs: adapter_type(**kwargs, http_transport=fake.transport))
    async def route(self, state, config):
        if capability_id in state["rejected_capability_ids"]:
            return {"resolved_capability_id": None, "task_action": None}
        return {"resolved_capability_id": capability_id, "task_action": "new"}
    monkeypatch.setattr(chat.StageOneRouter, "route", route)
    base = Settings()
    settings = Settings(database_url=str(base.database_url).rsplit("/", 1)[0] + "/agent_runtime_ra02_probe",
        local_user_id=f"ra02-probe-{uuid4()}", remote_a2a_development_mode=True,
        remote_a2a_poll_interval_seconds=0.001, run_coordinator_scan_interval_seconds=60, _env_file=None)
    return fake, settings, capability_id


async def _allow(settings, capability_id):
    from agent_runtime.capabilities.persistence import UserCapabilityPermissionRepository
    from agent_runtime.capabilities.persistence_models import UserCapabilityPermission
    repository = UserCapabilityPermissionRepository(settings)
    await repository.setup()
    now = datetime.now(UTC)
    await repository.set_allowed(UserCapabilityPermission(user_id=settings.local_user_id, capability_id=capability_id, allowed=True, created_at=now, updated_at=now))
    return repository


def _run(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.parametrize("before_binding", [True, False])
def test_health_detected_identity_change_blocks_same_process_task_io(monkeypatch, before_binding):
    """R01：同进程健康刷新识别替换后，取消和删除不能绕过身份失效门禁。"""
    from agent_runtime import chat
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel

    fake, settings, capability_id = _configure_runtime(
        monkeypatch, state="TASK_STATE_COMPLETED" if before_binding else "TASK_STATE_INPUT_REQUIRED"
    )

    async def exercise():
        permissions = await _allow(settings, capability_id)
        fake.disconnect_after_accept = before_binding
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="R01同进程身份刷新探针")
            await service._run_coordinator.wait_until_idle()
            opened = await service._open_invocation_resolver.resolve(run_id=submitted.run_id)
            assert opened is not None
            operation_facts = [(row.operation_id, row.operation_key, row.status) for row in opened.operations]
            registry = service._remote_gateway._registry
            fake.card["version"] = "replacement"
            await registry._health_service._refresh(capability_id, reason="r01_identity_changed")
            assert not await registry.serviceable_router_projections()
            start = len(fake.requests)
            try:
                await service.cancel_persistent_run(run_id=submitted.run_id)
                await service._run_coordinator.wait_until_idle()
                task_requests = [r for r in fake.requests[start:] if not r.url.path.endswith("agent-card.json")]
                run = await service._persistent_run_repository.get(submitted.run_id)
                assert task_requests == [], {"actual_status": run.status, "methods": [r.method for r in task_requests]}
                assert run.status == "cancel_requested"
                current = await service._open_invocation_resolver.resolve(run_id=run.run_id)
                assert current is not None and current.invocation_id == opened.invocation_id
                assert current.task.task_id == opened.task.task_id
                assert [(row.operation_id, row.operation_key, row.status) for row in current.operations] == operation_facts
                assert len(fake.tasks) == 1
                with pytest.raises(Exception) as conflict:
                    await service.submit_chat_run(request_id=uuid4(), session_id=run.session_id, content="必须仍阻塞")
                assert conflict.value.status_code == 409
                with pytest.raises(Exception) as deletion:
                    await service.delete_session(session_id=run.session_id)
                assert deletion.value.status_code == 409 and deletion.value.retryable
                assert await service._session_repository.get(run.session_id) is not None
                assert all(r.url.path.endswith("agent-card.json") for r in fake.requests[start:])
            finally:
                # 先完成核心断言，再显式重验原身份；不能以只恢复 Fake Card 的清理掩盖失败。
                fake.card["version"] = "1.0.0"
                await registry._health_service._refresh(capability_id, reason="r01_original_identity_verified")
                assert await registry.serviceable_router_projections()
                await service.delete_session(session_id=submitted.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)

    _run(exercise())


@pytest.mark.parametrize("previous_current", [False, True])
def test_explicit_scope_rejection_atomically_removes_only_transport(monkeypatch, previous_current):
    from agent_runtime import chat
    from agent_runtime.persistence.database import open_database_connection
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_REJECTED")
    fake.out_of_scope = True
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            session_id = None
            previous_task_id = None
            if previous_current:
                fake.state = "TASK_STATE_COMPLETED"
                first = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="原有任务")
                await service._run_coordinator.wait_until_idle()
                session_id = first.session_id
                async with open_database_connection(settings) as connection:
                    row = await (await connection.execute("SELECT current_task_id FROM capability_task_contexts WHERE session_id=%s", (session_id,))).fetchone()
                    previous_task_id = row["current_task_id"]
                await service._parent_graph.aupdate_state({"configurable": {"thread_id": str(session_id)}}, {"resolved_capability_id": None})
                fake.state = "TASK_STATE_REJECTED"
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=session_id, content="范围外请求")
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status == "completed"
            assert fake.business_creations == (1 if previous_current else 0)
            async with open_database_connection(settings) as connection:
                for table in ("capability_tasks", "capability_task_contexts", "capability_operations"):
                    condition = "run_id=%s" if table == "capability_operations" else "session_id=%s"
                    key = run.run_id if table == "capability_operations" else run.session_id
                    row = await (await connection.execute(f"SELECT count(*) AS total FROM {table} WHERE {condition}", (key,))).fetchone()
                    assert row["total"] == (1 if previous_current and table != "capability_operations" else 0)
                if previous_current:
                    row = await (await connection.execute("SELECT current_task_id FROM capability_task_contexts WHERE session_id=%s", (run.session_id,))).fetchone()
                    assert row["current_task_id"] == previous_task_id
                events = await (await connection.execute("SELECT event_type FROM runtime_events WHERE run_id=%s ORDER BY seq", (run.run_id,))).fetchall()
            kinds = [e["event_type"] for e in events]
            for suffix in ("task_bound", "task_terminal"):
                assert kinds.count("internal.remote_agent." + suffix) == 1
            assert kinds.count("internal.capability.task.rejected_rolled_back") == 1
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_cancel_unknown_blocks_delete_and_rebuild_until_confirmed(monkeypatch):
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="取消探针")
            await service._run_coordinator.wait_until_idle()
            fake.cancel_state = "TASK_STATE_WORKING"
            fake.disconnect_cancel = True
            await service.cancel_persistent_run(run_id=submitted.run_id)
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancel_requested"
            with pytest.raises(Exception) as conflict:
                await service.delete_session(session_id=submitted.session_id)
            assert conflict.value.status_code == 409 and conflict.value.retryable
            opened = await service._open_invocation_resolver.resolve(run_id=submitted.run_id)
            assert opened is not None and any(op.status == "pending" for op in opened.operations)
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancel_requested"
            fake.disconnect_cancel = False
            fake.cancel_state = "TASK_STATE_CANCELED"
            await service._run_coordinator.scan_once()
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancelled"
            assert len(fake.tasks) == 1
            await service.delete_session(session_id=submitted.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_timeout_unconfirmed_remains_cancel_requested(monkeypatch):
    from agent_runtime import chat
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_WORKING", timeout=0.05)
    fake.cancel_state = "TASK_STATE_WORKING"
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="超时探针")
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status == "cancel_requested"
            assert await service._open_invocation_resolver.resolve(run_id=run.run_id) is not None
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(run.run_id)).status == "cancel_requested"
            for task in fake.tasks.values():
                task["status"] = {"state": "TASK_STATE_CANCELED"}
            await service._run_coordinator.scan_once()
            await service._run_coordinator.wait_until_idle()
            final = await service._persistent_run_repository.get(run.run_id)
            assert final.status == "failed" and final.error_code == "CAPABILITY_TIMEOUT"
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("extra", ["business", "remote_cancel:foreign", "remote_send:other", "wrong_owner", "wrong_key", "failed_send", "child_checkpoint", "public_output"])
def test_scope_rejection_never_clears_other_operations(monkeypatch, extra):
    from agent_runtime import chat
    from agent_runtime.capabilities.remote_a2a.capability import RemoteA2ACapability
    from agent_runtime.persistence.database import open_database_connection
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_REJECTED")
    fake.out_of_scope = True
    invoke = RemoteA2ACapability.invoke_bound
    async def with_operation(self, context, agent, task_input, *, sequencer):
        result = await invoke(self, context, agent, task_input, sequencer=sequencer)
        if extra in {"wrong_owner", "wrong_key", "failed_send"}:
            column, value = {"wrong_owner": ("capability_id", "different"), "wrong_key": ("operation_key", "remote_send:unrelated"), "failed_send": ("status", "failed")}[extra]
            async with open_database_connection(settings) as connection:
                await connection.execute(f"UPDATE capability_operations SET {column}=%s WHERE run_id=%s", (value, context.run_id))
                await connection.commit()
        elif extra == "child_checkpoint":
            from langgraph.checkpoint.base import empty_checkpoint
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            async with AsyncPostgresSaver.from_conn_string(settings.database_connection_string) as saver:
                await saver.aput(agent.config, empty_checkpoint(), {"source": "update", "step": 0, "parents": {}}, {})
        elif extra == "public_output":
            from agent_runtime.runtime.agent_contract import AgentTextEvent
            context.events.emit(AgentTextEvent(text="违规拒绝输出"))
        else:
            try:
                async with context.operation(extra):
                    raise OSError("模拟额外操作结果未知")
            except OSError:
                pass
        return result
    monkeypatch.setattr(RemoteA2ACapability, "invoke_bound", with_operation)
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="范围拒绝负条件")
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status == "failed"
            async with open_database_connection(settings) as connection:
                row = await (await connection.execute("SELECT count(*) AS total FROM capability_operations WHERE run_id=%s", (run.run_id,))).fetchone()
                assert row["total"] == (2 if extra in {"business", "remote_cancel:foreign", "remote_send:other"} else 1)
                row = await (await connection.execute("SELECT count(*) AS total FROM capability_tasks WHERE session_id=%s", (run.session_id,))).fetchone()
                assert row["total"] == 1
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


class SimulatedProcessCrash(BaseException):
    """绕过业务失败收尾，保留崩溃时的 durable 事实。"""


@pytest.mark.parametrize("first_reason", ["explicit", "timeout"])
@pytest.mark.parametrize("fail_transaction", [False, True])
def test_first_cancel_intent_wins_and_state_event_are_atomic(monkeypatch, first_reason, fail_transaction):
    from agent_runtime import chat
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    fake.cancel_state = "TASK_STATE_WORKING"
    insert = PostgresRuntimeEventRepository._insert_event
    armed = False
    async def fail_insert(connection, event):
        if armed and event.event_type == "internal.run.cancel_requested":
            raise SimulatedProcessCrash()
        return await insert(connection, event)
    monkeypatch.setattr(PostgresRuntimeEventRepository, "_insert_event", staticmethod(fail_insert))
    async def exercise():
        nonlocal armed
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="取消意图竞争探针")
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            code = "CAPABILITY_REMOTE_TIMEOUT_UNCONFIRMED" if first_reason == "timeout" else "CAPABILITY_REMOTE_CANCEL_UNCONFIRMED"
            if fail_transaction:
                armed = True
                with pytest.raises(SimulatedProcessCrash):
                    await service._defer_remote_run(run, code)
                armed = False
                assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
                assert not await service._runtime_event_repository.has_event_type(run_id=run.run_id, event_type="internal.run.cancel_requested")
            await service._defer_remote_run(run, code)
            opposite = "CAPABILITY_REMOTE_CANCEL_UNCONFIRMED" if first_reason == "timeout" else "CAPABILITY_REMOTE_TIMEOUT_UNCONFIRMED"
            await service._defer_remote_run(run, opposite)
            await service.cancel_persistent_run(run_id=run.run_id)
            await service._run_coordinator.wait_until_idle()
            with pytest.raises(Exception) as conflict:
                await service.delete_session(session_id=run.session_id)
            assert conflict.value.status_code == 409
            async with open_database_connection(settings) as connection:
                rows = await (await connection.execute("SELECT payload FROM runtime_events WHERE run_id=%s AND event_type='internal.run.cancel_requested'", (run.run_id,))).fetchall()
                assert len(rows) == 1 and rows[0]["payload"]["reason"] == first_reason
            for task in fake.tasks.values():
                task["status"] = {"state": "TASK_STATE_CANCELED"}
            await service._run_coordinator.scan_once()
            await service._run_coordinator.wait_until_idle()
            final = await service._persistent_run_repository.get(run.run_id)
            assert final.status == ("failed" if first_reason == "timeout" else "cancelled")
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_started_transaction_failure_never_dispatches_send(monkeypatch):
    from agent_runtime import chat
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch)
    insert = PostgresRuntimeEventRepository._insert_event
    async def fail_insert(connection, event):
        if event.event_type == "internal.capability.invocation.started":
            raise OSError("模拟发送前提交失败")
        return await insert(connection, event)
    monkeypatch.setattr(PostgresRuntimeEventRepository, "_insert_event", staticmethod(fail_insert))
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="发送前事务探针")
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(run.run_id)).status == "failed"
            assert not fake.tasks and not fake.messages
            assert not await service._runtime_event_repository.has_event_type(run_id=run.run_id, event_type="internal.capability.invocation.started")
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_timeout_checkpoint_before_run_projection_replays_same_failure(monkeypatch):
    from agent_runtime import chat
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.sequencer import RunSequencer, _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_WORKING", timeout=0.05)
    fake.cancel_state = "TASK_STATE_WORKING"
    transition = RunSequencer.transition_run
    armed = False
    async def crash(self, **kwargs):
        nonlocal armed
        if armed and kwargs["target_status"] == "failed":
            armed = False
            raise SimulatedProcessCrash()
        return await transition(self, **kwargs)
    monkeypatch.setattr(RunSequencer, "transition_run", crash)
    async def exercise():
        nonlocal armed
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="超时投影崩溃探针")
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancel_requested"
            for task in fake.tasks.values():
                task["status"] = {"state": "TASK_STATE_CANCELED"}
            armed = True
            await service._run_coordinator.scan_once()
            await service._run_coordinator.wait_until_idle()
            assert not armed
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "cancel_requested"
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status == "failed" and run.error_code == "CAPABILITY_TIMEOUT"
            state = await service._parent_graph.aget_state({"configurable": {"thread_id": str(run.session_id)}})
            ai = [message for message in state.values["messages"] if message.id == str(run.response_message_id)]
            assert len(ai) == 1 and ai[0].additional_kwargs["runtime_status"] == "incomplete"
            async with open_database_connection(settings) as connection:
                for kind in ("message.finalized", "run.failed", "internal.capability.invocation.failed"):
                    row = await (await connection.execute("SELECT count(*) AS total FROM runtime_events WHERE run_id=%s AND event_type=%s", (run.run_id, kind))).fetchone()
                    assert row["total"] == 1
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_manual_policy_cannot_release_unknown_remote_task(monkeypatch):
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    fake.manifest["recovery_policy"] = "manual"
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="手动策略未知屏障")
            await service._run_coordinator.wait_until_idle()
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
            assert await service._open_invocation_resolver.resolve(run_id=run.run_id) is not None
            await service.cancel_persistent_run(run_id=run.run_id)
            await service._run_coordinator.wait_until_idle()
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("replacement_local", [False, True])
def test_missing_manifest_cannot_terminate_or_delete_durable_remote_binding(monkeypatch, replacement_local):
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    source_load = chat.LocalYamlCapabilitySource.load
    original_transport = fake.manifest["transport"]
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="远程配置缺失屏障")
            await service._run_coordinator.wait_until_idle()
        _LIVE_SEQUENCERS.clear()
        if replacement_local:
            fake.manifest["transport"] = {"type": "local", "entrypoint": "agent_runtime.capabilities.general_chat.entrypoint:create_capability"}
        else:
            monkeypatch.setattr(chat.LocalYamlCapabilitySource, "load", lambda self: [])
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
            with pytest.raises(Exception) as conflict:
                await service.delete_session(session_id=run.session_id)
            assert conflict.value.status_code == 409
            assert await service._open_invocation_resolver.resolve(run_id=run.run_id) is not None
        _LIVE_SEQUENCERS.clear()
        monkeypatch.setattr(chat.LocalYamlCapabilitySource, "load", source_load)
        fake.manifest["transport"] = original_transport
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            await service.cancel_persistent_run(run_id=run.run_id)
            await service._run_coordinator.wait_until_idle()
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("before_binding", [True, False])
@pytest.mark.parametrize("change_source", [False, True])
def test_replacement_identity_receives_no_task_operations(monkeypatch, before_binding, change_source):
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.fake_a2a import FakeA2A
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_COMPLETED" if before_binding else "TASK_STATE_INPUT_REQUIRED")
    original_transport = fake.transport
    original_source = fake.manifest["transport"]["agent_card_url"]
    replacement = FakeA2A()
    replacement.card["version"] = "replacement"
    async def exercise():
        permissions = await _allow(settings, capability_id)
        fake.disconnect_after_accept = before_binding
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="身份替换探针")
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "recovering"
        _LIVE_SEQUENCERS.clear()
        if change_source:
            fake.manifest["transport"]["agent_card_url"] = "https://agent.example/changed/agent-card.json"
            monitored = fake
            request_start = len(fake.requests)
        else:
            fake.transport = replacement.transport
            monitored = replacement
            request_start = 0
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "recovering"
            with pytest.raises(Exception) as conflict:
                await service.delete_session(session_id=submitted.session_id)
            assert conflict.value.status_code == 409
            assert len(replacement.tasks) == 0
            assert all(request.url.path.endswith("agent-card.json") for request in monitored.requests[request_start:])
        _LIVE_SEQUENCERS.clear()
        fake.transport = original_transport
        fake.manifest["transport"]["agent_card_url"] = original_source
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            await service.cancel_persistent_run(run_id=submitted.run_id)
            await service._run_coordinator.wait_until_idle()
            await service.delete_session(session_id=submitted.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("corruption", ["missing_started_hash", "binding_mismatch"])
def test_old_or_inconsistent_identity_evidence_never_refilled(monkeypatch, corruption):
    from agent_runtime import chat
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_COMPLETED" if corruption == "missing_started_hash" else "TASK_STATE_INPUT_REQUIRED")
    async def exercise():
        permissions = await _allow(settings, capability_id)
        fake.disconnect_after_accept = corruption == "missing_started_hash"
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="历史身份凭据探针")
            await service._run_coordinator.wait_until_idle()
        _LIVE_SEQUENCERS.clear()
        kind = "internal.capability.invocation.started" if corruption == "missing_started_hash" else "internal.remote_agent.task_bound"
        async with open_database_connection(settings) as connection:
            row = await (await connection.execute("SELECT payload FROM runtime_events WHERE run_id=%s AND event_type=%s", (submitted.run_id, kind))).fetchone()
            original_payload = row["payload"]
            expression = "payload - 'remote_agent_identity_hash'" if corruption == "missing_started_hash" else "jsonb_set(payload, '{remote_agent_identity_hash}', to_jsonb(%s::text))"
            arguments = (submitted.run_id, kind) if corruption == "missing_started_hash" else ("b" * 64, submitted.run_id, kind)
            await connection.execute(f"UPDATE runtime_events SET payload={expression} WHERE run_id=%s AND event_type=%s", arguments)
            await connection.commit()
        start = len(fake.requests)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            assert (await service._persistent_run_repository.get(submitted.run_id)).status == "recovering"
            assert all(request.url.path.endswith("agent-card.json") for request in fake.requests[start:])
            async with open_database_connection(settings) as connection:
                row = await (await connection.execute("SELECT payload FROM runtime_events WHERE run_id=%s AND event_type=%s", (submitted.run_id, kind))).fetchone()
                assert row["payload"].get("remote_agent_identity_hash") == (None if corruption == "missing_started_hash" else "b" * 64)
        # 仅测试夹具恢复自己注入前的精确字节以清理；产品恢复没有回填路径。
        from psycopg.types.json import Jsonb
        async with open_database_connection(settings) as connection:
            await connection.execute("UPDATE runtime_events SET payload=%s WHERE run_id=%s AND event_type=%s", (Jsonb(original_payload), submitted.run_id, kind))
            await connection.commit()
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            await service.cancel_persistent_run(run_id=submitted.run_id)
            await service._run_coordinator.wait_until_idle()
            await service.delete_session(session_id=submitted.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("crash_at", ["binding", "send_exit", "rejected_send_exit", "rollback_before_commit", "rollback_after_commit", "parent_before_projection"])
def test_crash_boundaries_reconcile_without_duplicate_send(monkeypatch, crash_at):
    from agent_runtime import chat
    from agent_runtime.capabilities import persistence
    from agent_runtime.capabilities.task_service import CapabilityTaskService
    from agent_runtime.capabilities.persistence import CapabilityOperationRepository
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository
    from agent_runtime.runtime.sequencer import RunSequencer, _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    rejected = crash_at.startswith("rollback") or crash_at == "rejected_send_exit"
    fake, settings, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_REJECTED" if rejected else "TASK_STATE_COMPLETED")
    fake.out_of_scope = rejected
    fired = False
    if crash_at == "binding":
        original = PostgresRuntimeEventRepository.record_remote_agent_event
        async def crash(self, event):
            nonlocal fired
            result = await original(self, event)
            if not fired and event.event_type.endswith("task_bound"):
                fired = True
                raise SimulatedProcessCrash()
            return result
        monkeypatch.setattr(PostgresRuntimeEventRepository, "record_remote_agent_event", crash)
    elif crash_at in {"send_exit", "rejected_send_exit"}:
        original = CapabilityOperationRepository.transition_pending
        async def crash(self, **kwargs):
            nonlocal fired
            if not fired:
                fired = True
                raise SimulatedProcessCrash()
            return await original(self, **kwargs)
        monkeypatch.setattr(CapabilityOperationRepository, "transition_pending", crash)
    elif crash_at == "rollback_before_commit":
        original = persistence.insert_capability_invocation_terminal
        async def crash(**kwargs):
            nonlocal fired
            if not fired and kwargs["event"].payload.get("outcome") == "rejected":
                fired = True
                raise SimulatedProcessCrash()
            return await original(**kwargs)
        monkeypatch.setattr(persistence, "insert_capability_invocation_terminal", crash)
    elif crash_at == "rollback_after_commit":
        original = CapabilityTaskService.rollback_rejected_new
        async def crash(self, **kwargs):
            nonlocal fired
            result = await original(self, **kwargs)
            if not fired:
                fired = True
                raise SimulatedProcessCrash()
            return result
        monkeypatch.setattr(CapabilityTaskService, "rollback_rejected_new", crash)
    else:
        original = RunSequencer.transition_run
        async def crash(self, **kwargs):
            nonlocal fired
            if not fired and kwargs["target_status"] == "completed":
                fired = True
                raise SimulatedProcessCrash()
            return await original(self, **kwargs)
        monkeypatch.setattr(RunSequencer, "transition_run", crash)
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="崩溃边界探针")
            await service._run_coordinator.wait_until_idle()
            assert fired
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status in {"running", "recovering"}
            if crash_at != "rollback_after_commit":
                opened = await service._open_invocation_resolver.resolve(run_id=run.run_id)
                if crash_at != "parent_before_projection":
                    assert opened is not None
                if crash_at in {"binding", "send_exit", "rejected_send_exit"}:
                    assert opened.operations[0].status == "pending"
            if crash_at.startswith("rollback"):
                from agent_runtime.persistence.database import open_database_connection
                async with open_database_connection(settings) as connection:
                    for table, condition, value in (("capability_tasks", "session_id", run.session_id), ("capability_task_contexts", "session_id", run.session_id), ("capability_operations", "run_id", run.run_id)):
                        row = await (await connection.execute(f"SELECT count(*) AS total FROM {table} WHERE {condition}=%s", (value,))).fetchone()
                        assert row["total"] == (1 if crash_at == "rollback_before_commit" else 0)
            sends = sum(request.url.path.endswith("/message:send") for request in fake.requests)
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(submitted.run_id)
            assert run.status == "completed"
            assert sum(request.url.path.endswith("/message:send") for request in fake.requests) == sends == 1
            state = await service._parent_graph.aget_state({"configurable": {"thread_id": str(run.session_id)}})
            ai = [m for m in state.values["messages"] if m.id == str(run.response_message_id)]
            assert len(ai) == 1 and ai[0].content.strip()
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_accept_before_binding_survives_four_service_rebuilds(monkeypatch):
    from agent_runtime import chat
    from agent_runtime.capabilities.persistence import UserCapabilityPermissionRepository
    from agent_runtime.capabilities.persistence_models import UserCapabilityPermission
    from agent_runtime.capabilities.source import CapabilityManifestDocument
    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    from tests.capabilities.test_manifest import _valid_manifest
    from tests.fake_a2a import FakeA2A, remote_transport

    fake = FakeA2A()
    raw = _valid_manifest()
    raw["transport"] = remote_transport()
    raw["side_effect_policy"] = "idempotent"
    capability_id = raw["capability_id"]
    monkeypatch.setattr(chat.LocalYamlCapabilitySource, "load", lambda self: [CapabilityManifestDocument(source=Path("remote/manifest.yaml"), raw_document=raw, error=None)])
    adapter_type = chat.RemoteA2ACapability
    monkeypatch.setattr(chat, "RemoteA2ACapability", lambda **kwargs: adapter_type(**kwargs, http_transport=fake.transport))

    async def route(self, state, config):
        return {"resolved_capability_id": capability_id, "task_action": "new"}
    monkeypatch.setattr(chat.StageOneRouter, "route", route)
    base = Settings()
    settings = Settings(database_url=str(base.database_url).rsplit("/", 1)[0] + "/agent_runtime_ra02_probe",
        local_user_id=f"ra02-probe-{uuid4()}", remote_a2a_development_mode=True,
        remote_a2a_poll_interval_seconds=0.001, run_coordinator_scan_interval_seconds=60, _env_file=None)

    async def exercise():
        permissions = UserCapabilityPermissionRepository(settings)
        await permissions.setup()
        now = datetime.now(UTC)
        await permissions.set_allowed(UserCapabilityPermission(user_id=settings.local_user_id, capability_id=capability_id, allowed=True, created_at=now, updated_at=now))
        original = None
        invocation_id = None
        message_id = None
        remote_identity = None
        for attempt in range(5):
            fake.disconnect_after_accept = True
            async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
                if original is None:
                    original = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="远程恢复探针正文")
                await service._run_coordinator.wait_until_idle()
                run = await service._persistent_run_repository.get(original.run_id)
                assert run.status == "recovering"
                assert run.recovery_attempts == attempt
                opened = await service._open_invocation_resolver.resolve(run_id=run.run_id)
                assert opened is not None
                if invocation_id is None:
                    invocation_id = opened.invocation_id
                    message_id = next(iter(fake.messages))
                    remote_identity = next(iter(fake.tasks.items()))
                assert opened.invocation_id == invocation_id
                assert len(fake.tasks) == len(fake.messages) == 1
                assert next(iter(fake.messages)) == message_id
                assert next(iter(fake.tasks.items())) == remote_identity
                assert len(opened.operations) == 1 and opened.operations[0].status == "pending"
                assert await service._runtime_event_repository.get_remote_agent_event(run_id=run.run_id, invocation_id=invocation_id, event_type="internal.remote_agent.task_bound") is None
                with pytest.raises(Exception) as conflict:
                    await service.submit_chat_run(request_id=uuid4(), session_id=run.session_id, content="不得并行")
                assert getattr(conflict.value, "status_code", None) == 409
            # 进程重建会丢弃单进程 Sequencer 缓存；远程 Fake 与数据库均保留。
            from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
            _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await service._run_coordinator.wait_until_idle()
            run = await service._persistent_run_repository.get(original.run_id)
            assert run.status == "completed" and run.recovery_attempts == 5
            state = await service._parent_graph.aget_state({"configurable": {"thread_id": str(run.session_id)}})
            ai = [m for m in state.values["messages"] if m.id == str(run.response_message_id)]
            assert len(ai) == 1 and ai[0].content == "第一段第二段"
            assert len(fake.tasks) == 1
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
