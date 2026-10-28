"""独立审核：旧轮次幂等重试与第二轮持久Resume重建不串答案。"""

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

from tests.integration.test_remote_interrupt_resume_postgres import _runtime, _idle, _facts, _events
from tests.integration.test_remote_runtime_postgres import _allow, _run


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="显式启用RA03审核隔离库")
def test_previous_resume_retry_cannot_consume_second_question(monkeypatch):
    """API原请求在第二题pending时幂等202，第二题已消费未Command重建保持独立答案。"""
    from agent_runtime import chat
    from agent_runtime.main import create_app
    from agent_runtime.runtime.sequencer import _LIVE_SEQUENCERS
    from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel

    fake, settings, capability_id = _runtime(monkeypatch, rounds=2)

    async def exercise():
        permissions = await _allow(settings, capability_id)
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            run = await service.submit_chat_run(request_id=uuid4(), session_id=None, content="审核两轮幂等交叉")
            await _idle(service)
            _, first = await _facts(service, run.run_id)
            original = {"interrupt_id": str(first.interrupt_id), "request_id": str(uuid4()),
                "resume_payload": {"answer": "第一轮独立日期"}}
            app = create_app(settings, chat_service=service)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://review") as client:
                path = f"/api/v1/chat/runs/{run.run_id}/resume"
                response = await client.post(path, json=original)
                assert response.status_code == 202
                await _idle(service)
                _, second = await _facts(service, run.run_id)
                assert first.interrupt_id != second.interrupt_id
                events_before = await _events(settings, run.run_id)
                sends_before = len(fake.messages)
                response = await client.post(path, json=original)
                assert response.status_code == 202 and response.json()["status"] == "interrupted"
                await _idle(service)
                _, unchanged = await _facts(service, run.run_id)
                assert unchanged.interrupt_id == second.interrupt_id
                assert len(fake.messages) == sends_before
                assert await _events(settings, run.run_id) == events_before
                active = await client.get(f"/api/v1/chat/sessions/{run.session_id}/active-run")
                assert active.status_code == 200
                assert active.json()["pending_interrupt"]["interrupt_payload"] == {"kind": "text", "prompt": "请补充范围"}

                async def no_command(_run_id):
                    raise OSError("审核：第二轮事务提交后唤醒中断")

                monkeypatch.setattr(service._run_coordinator, "wake_resumed", no_command)
                with pytest.raises(OSError):
                    await service.resume_persistent_run(run_id=run.run_id, interrupt_id=second.interrupt_id,
                        request_id=uuid4(), resume_payload={"answer": "第二轮独立范围"})
        _LIVE_SEQUENCERS.clear()
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await _idle(service)
            completed, _ = await _facts(service, run.run_id, expected="completed")
            assert sorted(fake.answers.values()) == ["第一轮独立日期", "第二轮独立范围"]
            assert len(fake.messages) == 3 and fake.business_creations == 1
            sends = [json.loads(request.content)["message"] for request in fake.requests if request.url.path.endswith("message:send")]
            assert len({message["messageId"] for message in sends}) == 3
            await service.delete_session(session_id=completed.session_id)
        await permissions.revoke(user_id=settings.local_user_id, capability_id=capability_id)

    _run(exercise())
