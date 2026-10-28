"""RA03：真实 Compose PostgreSQL、正式 Runtime 与独立远程 Fake 的补参探针。"""

import asyncio
import json
import os
from uuid import uuid4

import pytest

from tests.integration.test_remote_runtime_postgres import _allow, _configure_runtime, _run

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1", reason="显式启用 RA03 隔离 PostgreSQL 探针")]


class ProcessCrash(BaseException):
    """只终止测试执行器，保留已提交的数据库与独立远程任务。"""


def _runtime(monkeypatch, *, rounds=1):
    from agent_runtime.core.config import Settings
    fake, original, capability_id = _configure_runtime(monkeypatch, state="TASK_STATE_INPUT_REQUIRED")
    settings = Settings(database_url=str(original.database_url).rsplit("/", 1)[0] + "/agent_runtime_ra03_probe",
        redis_url="redis://127.0.0.1:6379/14",
        local_user_id=f"ra03-probe-{uuid4()}", remote_a2a_development_mode=True,
        remote_a2a_poll_interval_seconds=0.001, run_coordinator_scan_interval_seconds=60, _env_file=None)
    fake.input_prompts = ["请补充日期", "请补充范围"][:rounds]
    return fake, settings, capability_id


async def _idle(service):
    await asyncio.wait_for(service._run_coordinator.wait_until_idle(), timeout=20)


async def _facts(service, run_id, *, expected="interrupted"):
    from langchain_core.messages import AIMessage, HumanMessage
    run = await service._persistent_run_repository.get(run_id)
    assert run.status == expected
    snapshot = await service._parent_graph.aget_state({"configurable": {"thread_id": str(run.session_id)}})
    messages = snapshot.values["messages"]
    assert len([m for m in messages if isinstance(m, HumanMessage)]) == 1
    public_ai = [m for m in messages if isinstance(m, AIMessage)]
    pending = await service._run_interrupt_repository.get_pending(run_id=run_id)
    if expected == "interrupted":
        assert len(snapshot.interrupts) == 1 and pending is not None
        assert pending.interrupt_payload["kind"] == "text"
        assert not public_ai
        assert await service._open_invocation_resolver.resolve(run_id=run_id) is not None
    elif expected == "completed":
        assert not snapshot.interrupts and pending is None
        assert len(public_ai) == 1 and public_ai[0].content.strip()
        assert public_ai[0].id == str(run.response_message_id)
        assert await service._open_invocation_resolver.resolve(run_id=run_id) is None
    return run, pending


async def _events(settings, run_id):
    from agent_runtime.persistence.database import open_database_connection
    async with open_database_connection(settings) as connection:
        return await (await connection.execute(
            "SELECT event_type, payload, seq FROM runtime_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()


@pytest.mark.parametrize("window", ["pending_rebuild", "checkpoint_gap", "resume_gap", "accepted_unknown", "two_rounds"])
def test_remote_text_interrupt_survives_real_runtime_rebuild(monkeypatch, window):
    """覆盖持久投影、Command 前崩溃、补参受理未知和顺序两轮；只注入边界故障。"""
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import RunSequencer, _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _runtime(monkeypatch, rounds=2 if window == "two_rounds" else 1)
    original_require = RunSequencer.require_interrupt
    original_transition = RunSequencer.transition_run
    armed = window == "checkpoint_gap"
    active_service = None

    async def stop_before_projection(self, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            raise ProcessCrash()
        return await original_require(self, **kwargs)
    monkeypatch.setattr(RunSequencer, "require_interrupt", stop_before_projection)

    async def inspect_before_terminal(self, **kwargs):
        if kwargs["target_status"] == "completed":
            from langchain_core.messages import AIMessage
            current = await active_service._persistent_run_repository.get(self._run_id)
            snapshot = await active_service._parent_graph.aget_state({"configurable": {"thread_id": str(current.session_id)}})
            assert any(isinstance(m, AIMessage) and m.id == str(current.response_message_id) and m.content.strip()
                for m in snapshot.values["messages"])
            assert current.status == "running"
        return await original_transition(self, **kwargs)
    monkeypatch.setattr(RunSequencer, "transition_run", inspect_before_terminal)

    async def exercise():
        nonlocal active_service
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            active_service = service
            submitted = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="补参真实入口探针")
            await _idle(service)
            if window == "checkpoint_gap":
                run = await service._persistent_run_repository.get(submitted.run_id)
                assert run.status == "running"
                assert await service._run_interrupt_repository.get_pending(run_id=run.run_id) is None
                snapshot = await service._parent_graph.aget_state({"configurable": {"thread_id": str(run.session_id)}})
                assert len(snapshot.interrupts) == 1
            else:
                run, pending = await _facts(service, submitted.run_id)
                with pytest.raises(Exception) as conflict:
                    await service.submit_chat_run(request_id=uuid4(), session_id=run.session_id, content="必须阻塞")
                assert conflict.value.status_code == 409
            opened = await service._open_invocation_resolver.resolve(run_id=submitted.run_id)
            identity = (opened.invocation_id, opened.task.task_id)
            if window in {"resume_gap", "accepted_unknown", "two_rounds"}:
                if window == "resume_gap":
                    async def wake_failure(run_id):
                        raise OSError("模拟事务提交后尚未执行 Command")
                    monkeypatch.setattr(service._run_coordinator, "wake_resumed", wake_failure)
                    with pytest.raises(OSError):
                        await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                            request_id=uuid4(), resume_payload={"answer": "日期答案"})
                else:
                    fake.disconnect_after_accept = True
                    await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                        request_id=uuid4(), resume_payload={"answer": "日期答案"})
                    await _idle(service)
                    assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            active_service = service
            await _idle(service)
            if window in {"pending_rebuild", "checkpoint_gap"}:
                run, pending = await _facts(service, submitted.run_id)
                current = await service._open_invocation_resolver.resolve(run_id=run.run_id)
                assert (current.invocation_id, current.task.task_id) == identity
                await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                    request_id=uuid4(), resume_payload={"answer": "日期答案"})
                await _idle(service)
            if window == "two_rounds":
                run, pending = await _facts(service, submitted.run_id)
                assert pending.interrupt_payload["prompt"] == "请补充范围"
                current = await service._open_invocation_resolver.resolve(run_id=run.run_id)
                assert (current.invocation_id, current.task.task_id) == identity
                await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                    request_id=uuid4(), resume_payload={"answer": "范围答案"})
                await _idle(service)
            run, _ = await _facts(service, submitted.run_id, expected="completed")
            events = await _events(settings, run.run_id)
            types = [row["event_type"] for row in events]
            assert types.count("internal.capability.invocation.started") == 1
            assert types.count("internal.capability.invocation.completed") == 1
            assert types.count("run.completed") == 1 and "run.failed" not in types
            assert types.count("interrupt.required") == (2 if window == "two_rounds" else 1)
            assert len({row["seq"] for row in events}) == len(events)
            assert fake.business_creations == 1 and len(fake.tasks) == 1
            sends = [json.loads(r.content)["message"] for r in fake.requests if r.url.path.endswith("message:send")]
            supplement_ids = {m["messageId"] for m in sends if m.get("taskId")}
            assert len(supplement_ids) == (2 if window == "two_rounds" else 1)
            assert sends[0]["messageId"] not in supplement_ids
            for key in supplement_ids:
                fingerprints = {json.dumps(m, sort_keys=True) for m in sends if m["messageId"] == key}
                assert len(fingerprints) == 1
            from agent_runtime.persistence.database import open_database_connection
            async with open_database_connection(settings) as connection:
                operations = await (await connection.execute("SELECT operation_key, status, invocation_id FROM capability_operations WHERE run_id=%s", (run.run_id,))).fetchall()
                assert {row["operation_key"] for row in operations} == {"remote_send:" + m["messageId"] for m in sends}
                assert {row["status"] for row in operations} == {"succeeded"}
                assert {row["invocation_id"] for row in operations} == {identity[0]}
                assert (await (await connection.execute("SELECT count(*) AS n FROM capability_tasks WHERE session_id=%s", (run.session_id,))).fetchone())["n"] == 1
            assert sorted(fake.answers.values()) == sorted(["日期答案", "范围答案"] if window == "two_rounds" else ["日期答案"])
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


@pytest.mark.parametrize("resume_first", [False, True])
def test_remote_cancel_resume_compete_on_original_task(monkeypatch, resume_first):
    """取消先持久化拒绝补参；补参先持久化仍取消原远程任务并保留未知屏障。"""
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import RunSequencer, _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _runtime(monkeypatch)
    original_transition = RunSequencer.transition_run
    stop_terminal = False
    async def checkpoint_then_crash(self, **kwargs):
        nonlocal stop_terminal
        if stop_terminal and kwargs["target_status"] == "cancelled":
            stop_terminal = False
            raise ProcessCrash()
        return await original_transition(self, **kwargs)
    monkeypatch.setattr(RunSequencer, "transition_run", checkpoint_then_crash)
    async def exercise():
        nonlocal stop_terminal
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="取消补参竞争")
            await _idle(service)
            run, pending = await _facts(service, run.run_id)
            fake.cancel_state = "TASK_STATE_WORKING"
            async def delayed_wake(run_id):
                return None
            monkeypatch.setattr(service._run_coordinator, "wake_resumed", delayed_wake)
            args = dict(run_id=run.run_id, interrupt_id=pending.interrupt_id, request_id=uuid4(), resume_payload={"answer": "不应发送"})
            if resume_first:
                await service.resume_persistent_run(**args)
            await service.cancel_persistent_run(run_id=run.run_id)
            await _idle(service)
            assert (await service._persistent_run_repository.get(run.run_id)).status == "cancel_requested"
            if not resume_first:
                with pytest.raises(Exception) as conflict:
                    await service.resume_persistent_run(**args)
                assert conflict.value.status_code == 409
            assert not [r for r in fake.requests if r.url.path.endswith("message:send") and json.loads(r.content)["message"].get("taskId")]
            with pytest.raises(Exception) as deletion:
                await service.delete_session(session_id=run.session_id)
            assert deletion.value.status_code == 409
            for task in fake.tasks.values():
                task["status"] = {"state": "TASK_STATE_CANCELED"}
            stop_terminal = True
            await service._run_coordinator.scan_once()
            await _idle(service)
            assert (await service._persistent_run_repository.get(run.run_id)).status == "cancel_requested"
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await _idle(service)
            assert (await service._persistent_run_repository.get(run.run_id)).status == "cancelled"
            events = await _events(settings, run.run_id)
            assert [e["event_type"] for e in events].count("run.cancelled") == 1
            assert len(fake.tasks) == 1 and fake.business_creations == 1
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_resume_validation_retry_and_request_ownership_are_atomic(monkeypatch):
    """目标持久kind在行锁内校验；唤醒失败重试不能消费下一题或再次发送。"""
    from agent_runtime import chat
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _runtime(monkeypatch, rounds=2)
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="严格校验和恢复重试")
            await _idle(service)
            run, pending = await _facts(service, run.run_id)
            before = len(fake.messages)
            for invalid in ({"approved": True}, {"answer": " "}, {"answer": 3}, {"answer": True}, {"answer": "日期", "extra": 1}):
                with pytest.raises(Exception) as invalid_error:
                    await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                        request_id=uuid4(), resume_payload=invalid)
                assert invalid_error.value.status_code == 422
                assert (await service.get_pending_interrupt(run_id=run.run_id)).interrupt_id == pending.interrupt_id
                assert len(fake.messages) == before
                assert not [e for e in await _events(settings, run.run_id) if e["event_type"] == "interrupt.resumed"]
            wake = service._run_coordinator.wake_resumed
            async def fail_once(run_id):
                raise OSError("模拟唤醒失败")
            monkeypatch.setattr(service._run_coordinator, "wake_resumed", fail_once)
            request_id = uuid4()
            args = dict(run_id=run.run_id, interrupt_id=pending.interrupt_id, request_id=request_id, resume_payload={"answer": "日期答案"})
            with pytest.raises(OSError):
                await service.resume_persistent_run(**args)
            for changed in ({**args, "resume_payload": {"answer": "冲突"}}, {**args, "request_id": uuid4()}):
                with pytest.raises(Exception) as conflict:
                    await service.resume_persistent_run(**changed)
                assert conflict.value.status_code == 409
            monkeypatch.setattr(service._run_coordinator, "wake_resumed", wake)
            await asyncio.gather(service.resume_persistent_run(**args), service.resume_persistent_run(**args))
            await _idle(service)
            _, second = await _facts(service, run.run_id)
            assert second.interrupt_id != pending.interrupt_id
            with pytest.raises(Exception) as conflict:
                await service.resume_persistent_run(run_id=run.run_id, interrupt_id=second.interrupt_id,
                    request_id=request_id, resume_payload={"answer": "范围答案"})
            assert conflict.value.status_code == 409
            await service.resume_persistent_run(run_id=run.run_id, interrupt_id=second.interrupt_id,
                request_id=uuid4(), resume_payload={"answer": "范围答案"})
            await _idle(service)
            await _facts(service, run.run_id, expected="completed")
            assert len(fake.messages) == 3 and fake.business_creations == 1
            # 原Session必须保留到跨Run断言结束；删除会按正式契约清除幂等投影。
            other = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="跨Run恢复请求归属")
            await _idle(service)
            _, other_pending = await _facts(service, other.run_id)
            with pytest.raises(Exception) as cross_run:
                await service.resume_persistent_run(run_id=other.run_id, interrupt_id=other_pending.interrupt_id,
                    request_id=request_id, resume_payload={"answer": "跨Run禁止复用"})
            assert cross_run.value.status_code == 409
            assert (await service.get_pending_interrupt(run_id=other.run_id)).interrupt_id == other_pending.interrupt_id
            await service.cancel_persistent_run(run_id=other.run_id)
            await _idle(service)
            await service.delete_session(session_id=other.session_id)
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_manual_recovery_cannot_bypass_policy_via_resumed_interrupt(monkeypatch):
    """补参Send未知后，旧中断快照不得绕过既有manual恢复屏障。"""
    from agent_runtime import chat
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _runtime(monkeypatch)
    fake.manifest["recovery_policy"] = "manual"
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="manual补参未知屏障")
            await _idle(service)
            _, pending = await _facts(service, run.run_id)
            fake.disconnect_after_accept = True
            await service.resume_persistent_run(run_id=run.run_id, interrupt_id=pending.interrupt_id,
                request_id=uuid4(), resume_payload={"answer": "保留原回答"})
            await _idle(service)
            assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
            opened = await service._open_invocation_resolver.resolve(run_id=run.run_id)
            operations = [(row.operation_id, row.operation_key, row.status) for row in opened.operations]
        _LIVE_SEQUENCERS.clear()
        start = len(fake.requests)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await _idle(service)
            assert (await service._persistent_run_repository.get(run.run_id)).status == "recovering"
            assert all(request.url.path.endswith("agent-card.json") for request in fake.requests[start:])
            current = await service._open_invocation_resolver.resolve(run_id=run.run_id)
            assert current.invocation_id == opened.invocation_id
            assert [(row.operation_id, row.operation_key, row.status) for row in current.operations] == operations
            resumed = await service._run_interrupt_repository.get(run_id=run.run_id, interrupt_id=pending.interrupt_id)
            assert resumed.status == "resumed" and resumed.resume_payload == {"answer": "保留原回答"}
            with pytest.raises(Exception) as conflict:
                await service.submit_chat_run(request_id=uuid4(), session_id=run.session_id, content="仍须占用")
            assert conflict.value.status_code == 409
            await service.cancel_persistent_run(run_id=run.run_id)
            await _idle(service)
            await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())


def test_pending_interrupt_releases_bounded_capability_slot(monkeypatch):
    """同能力单槽下两个Session都能进入持久等待，等待期间不占用执行槽。"""
    from agent_runtime import chat
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
    fake, settings, capability_id = _runtime(monkeypatch)
    fake.manifest["concurrency"] = {"mode": "bounded", "max_concurrency": 1, "acquire_timeout_seconds": 2}
    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            runs = await asyncio.gather(*[service.submit_chat_run(request_id=uuid4(), session_id=None,
                content="并发槽等待探针") for _ in range(2)])
            await _idle(service)
            for run in runs:
                await _facts(service, run.run_id)
                assert "run.failed" not in [event["event_type"] for event in await _events(settings, run.run_id)]
            assert fake.business_creations == 2
            for run in runs:
                await service.delete_session(session_id=run.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)
    _run(exercise())
