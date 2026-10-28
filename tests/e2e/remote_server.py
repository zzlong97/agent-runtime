"""浏览器补参验收的独立正式Runtime；只替换远程网络与确定路由输入。"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import uvicorn

from agent_runtime import chat
from agent_runtime.capabilities.source import CapabilityManifestDocument
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.main import create_app
from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel
from tests.capabilities.test_manifest import _valid_manifest
from tests.e2e.server import _delete_e2e_sessions
from tests.fake_a2a import FakeA2A, remote_transport
from tests.integration.test_remote_runtime_postgres import _allow


class BrowserRemoteFake(FakeA2A):
    """初次文本选定测试情景；补参只推进原Task，保留messageId幂等校验。"""

    def handle(self, request):
        if request.url.path.endswith("message:send"):
            message = json.loads(request.content)["message"]
            if not message.get("taskId"):
                command = "".join(part["text"] for part in message["parts"])
                self.state = "TASK_STATE_COMPLETED" if command == "/remote normal" else "TASK_STATE_INPUT_REQUIRED"
                self.input_prompts = ["请补充日期", "请补充范围"] if self.state == "TASK_STATE_INPUT_REQUIRED" else None
        return super().handle(request)


async def _serve():
    """显式测试环境、专用用户/端口/隔离库，不重启任何既有Runtime。"""
    run_id, port = os.getenv("PLAYWRIGHT_E2E_RUN_ID"), os.getenv("PLAYWRIGHT_E2E_PORT")
    if os.getenv("RUN_POSTGRES_TESTS") != "1" or not run_id or not port or not 1024 <= int(port) <= 65535:
        raise RuntimeError("远程浏览器验收需要显式PG授权、运行标识和独立端口")
    original = Settings()
    settings = Settings(database_url=str(original.database_url).rsplit("/", 1)[0] + "/agent_runtime_ra03_probe",
        redis_url="redis://127.0.0.1:6379/14", local_user_id="ra03-browser-" + run_id,
        dashscope_api_key=None, llm_base_url=None, llm_model=None,
        runtime_demo_mode=False, remote_a2a_development_mode=True,
        remote_a2a_poll_interval_seconds=0.001, run_cancel_grace_seconds=0.1, _env_file=None)
    fake = BrowserRemoteFake()
    raw = _valid_manifest()
    raw.update(transport=remote_transport(), side_effect_policy="idempotent")
    raw["execution"] = {"timeout_seconds": 30, "cancel_grace_seconds": 0.01}
    adapter = chat.RemoteA2ACapability
    async def route(self, state, config):
        return {"resolved_capability_id": raw["capability_id"], "task_action": "new"}
    permissions = await _allow(settings, raw["capability_id"])
    with patch.object(chat.LocalYamlCapabilitySource, "load", lambda self: [CapabilityManifestDocument(
            source=Path("tests/remote-browser-manifest.yaml"), raw_document=raw, error=None)]), \
            patch.object(chat, "RemoteA2ACapability", lambda **kwargs: adapter(**kwargs, http_transport=fake.transport)), \
            patch.object(chat.StageOneRouter, "route", route):
        async with chat.open_chat_service(settings, model=_BuiltinCapabilityFakeModel()) as service:
            await _delete_e2e_sessions(service)
            try:
                await uvicorn.Server(uvicorn.Config(create_app(settings, chat_service=service),
                    host="127.0.0.1", port=int(port), log_level="warning")).serve()
            finally:
                await _delete_e2e_sessions(service)
                assert fake.business_creations == len(fake.tasks)
                await permissions.revoke(user_id=settings.local_user_id, capability_id=raw["capability_id"])


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(_serve())
