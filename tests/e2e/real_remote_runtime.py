"""RA-INT-01 专用真实 HTTP Runtime；不挂载到产品或主服务。"""

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

os.environ["LOG_LEVEL"] = "WARNING"

import uvicorn
import yaml
from fastapi.encoders import jsonable_encoder

from agent_runtime import chat
from agent_runtime.capabilities.remote_a2a.client import RemoteA2AClient
from agent_runtime.capabilities.remote_a2a.errors import unavailable_error
from agent_runtime.capabilities.source import CapabilityManifestDocument
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.core.logging import configure_logging
from agent_runtime.main import create_app
from agent_runtime.persistence.database import open_database_connection
from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
from tests.integration.test_remote_runtime_postgres import _allow

BATCH = "a9549d9c-6976-4721-8fed-f5fe863c928a"
DATABASE = "agent_runtime_ra_int01_a9549d9c"
MANIFEST = Path(".dev-team/tasks/RA-INT-01/evidence/联调候选/manifest.yaml")
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
STARTED_AT = datetime.now(UTC).isoformat()
CONTROL = {"fault": os.getenv("RA_INT_FAULT", "none"), "held": False}
OBSERVATIONS = []


class FixedRouterModel(_BuiltinCapabilityFakeModel):
    """只替换模型输出，保留真实 Router 候选、Schema 和权限检查。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if "意图路由器" in str(messages[0].content):
            return self._tool_result("RouterDecision", {
                "capability_id": "semantic_pattern_mining",
                "task_action": "new", "confidence": 1.0,
            })
        return super()._generate(messages, stop, run_manager, **kwargs)


def _observe(operation, task, message_id=None):
    """仅保留公开身份和状态；不复制远程正文或私有状态。"""
    item = {"at": datetime.now(UTC).isoformat(), "operation": operation,
            "task_id": task.id, "context_id": task.context_id,
            "state": int(task.status.state)}
    if message_id:
        item["message_id"] = str(message_id)
    # Get 的重复状态无须产生逐次日志，保留状态变化及所有 Send/Cancel。
    if operation != "get" or not OBSERVATIONS or any(
            item[key] != OBSERVATIONS[-1].get(key) for key in ("task_id", "state")):
        OBSERVATIONS.append(item)
    return task


async def _serve():
    """单独隔离库、Redis15、固定测试用户和回环端口；不清理任何既有数据。"""
    if os.getenv("RA_INT_APPROVED") != BATCH:
        raise RuntimeError("真实联调必须显式绑定已批准批次")
    base = Settings()
    settings = Settings(
        database_url=str(base.database_url).rsplit("/", 1)[0] + "/" + DATABASE,
        redis_url="redis://127.0.0.1:6379/15", local_user_id="ra-int-" + BATCH,
        dashscope_api_key=None, llm_base_url=None, llm_model=None,
        runtime_demo_mode=False, remote_a2a_development_mode=True,
        remote_a2a_http_timeout_seconds=10, remote_a2a_poll_interval_seconds=1,
        run_coordinator_scan_interval_seconds=2, run_cancel_grace_seconds=2,
        log_level="WARNING", log_dir=Path("logs/ra-int-" + BATCH), _env_file=None,
    )
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert raw["capability_id"] == "semantic_pattern_mining"
    assert raw["transport"]["agent_card_url"] == "http://127.0.0.1:18080/.well-known/agent-card.json"
    configure_logging("INFO", log_dir=settings.log_dir)
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    await _allow(settings, raw["capability_id"])
    original_send = RemoteA2AClient.send_message
    original_get = RemoteA2AClient.get_task
    original_cancel = RemoteA2AClient.cancel_task
    original_acquire = chat.RemoteA2ACapability.acquire_task

    async def send(self, **kwargs):
        task = _observe("send", await original_send(self, **kwargs), kwargs["message_id"])
        if CONTROL["fault"] == "before_binding" and not kwargs.get("task_id"):
            CONTROL["held"] = True
            await asyncio.Event().wait()
        return task

    async def get(self, task_id):
        task = await original_get(self, task_id)
        if CONTROL["fault"] == "unknown":
            raise unavailable_error()
        return _observe("get", task)

    async def cancel(self, task_id):
        task = _observe("cancel", await original_cancel(self, task_id))
        if CONTROL["fault"] == "unknown":
            raise unavailable_error()
        return task

    async def acquire(self, *args, **kwargs):
        task = await original_acquire(self, *args, **kwargs)
        if CONTROL["fault"] == "after_binding":
            CONTROL["held"] = True
            await asyncio.Event().wait()
        return task

    with patch.object(chat.LocalYamlCapabilitySource, "load", lambda self: [
            CapabilityManifestDocument(source=MANIFEST, raw_document=raw, error=None)]), \
            patch.object(RemoteA2AClient, "send_message", send), \
            patch.object(RemoteA2AClient, "get_task", get), \
            patch.object(RemoteA2AClient, "cancel_task", cancel), \
            patch.object(chat.RemoteA2ACapability, "acquire_task", acquire):
        async with chat.open_chat_service(settings, model=FixedRouterModel()) as service:
            app = create_app(settings, chat_service=service)

            @app.get("/__ra_int__/health")
            async def health():
                return {"batch": BATCH, "database": DATABASE, "pid": os.getpid(),
                        "source_sha256": SOURCE_SHA256, "started_at": STARTED_AT,
                        "control": CONTROL, "observations": OBSERVATIONS}

            @app.post("/__ra_int__/fault/{fault}")
            async def fault(fault: str):
                assert fault in {"none", "before_binding", "after_binding", "unknown"}
                CONTROL.update(fault=fault, held=False)
                return CONTROL

            @app.get("/__ra_int__/facts/{run_id}")
            async def facts(run_id: UUID):
                run = await service._persistent_run_repository.get(run_id)
                async with open_database_connection(settings) as connection:
                    events = await (await connection.execute("""
                        SELECT seq,event_type,visibility,
                          payload->>'invocation_id' AS invocation_id,
                          payload->>'task_id' AS task_id,
                          payload->>'capability_id' AS capability_id,
                          payload->>'remote_task_id' AS remote_task_id,
                          payload->>'remote_context_id' AS remote_context_id,
                          payload->>'initial_message_id' AS initial_message_id,
                          payload->>'remote_agent_identity_hash' AS identity_hash,
                          payload->>'remote_status' AS remote_status,
                          payload->>'error_code' AS error_code
                        FROM runtime_events WHERE run_id=%s ORDER BY seq
                        """, (run_id,))).fetchall()
                    tasks = await (await connection.execute("""
                        SELECT task_id,capability_id,status,last_run_id,last_invocation_id
                        FROM capability_tasks WHERE session_id=%s
                        """, (run.session_id,))).fetchall()
                    operations = await (await connection.execute("""
                        SELECT operation_key,status,invocation_id,task_id
                        FROM capability_operations WHERE run_id=%s ORDER BY created_at
                        """, (run_id,))).fetchall()
                history = await service.list_messages(session_id=run.session_id, before=None, limit=100)
                public = []
                for item in history.items:
                    text = item.content
                    public.append({"message_id": item.message_id, "role": item.role,
                        "runtime_status": item.runtime_status, "capability_id": item.capability_id,
                        "nonempty": bool(text.strip()), "chars": len(text),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "contains_start": "2026-10-01" in text,
                        "contains_end": "2026-10-20" in text})
                return jsonable_encoder({"run": {name: getattr(run, name) for name in (
                    "run_id", "session_id", "request_id", "response_message_id", "status", "recovery_attempts")},
                    "events": events, "tasks": tasks, "operations": operations,
                    "history": public, "control": CONTROL, "observations": OBSERVATIONS})

            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18781,
                                               log_level="warning", access_log=False)).serve()


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(_serve())
