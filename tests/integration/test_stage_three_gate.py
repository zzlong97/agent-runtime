"""S3-14 静态 Capability Runtime 集成验收门禁。"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

import agent_runtime.chat as chat_module
from agent_runtime.capabilities.persistence import (
    UserCapabilityPermissionRepository,
)
from agent_runtime.capabilities.persistence_models import (
    UserCapabilityPermission,
)
from agent_runtime.capabilities.source import CapabilityManifestDocument
from agent_runtime.chat import open_chat_service
from agent_runtime.core.config import Settings
from agent_runtime.main import create_app
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime.event_repository import (
    PostgresRuntimeEventRepository,
)
from agent_runtime.runtime.repository import PostgresRunRepository
from tests.integration.test_stage_one_acceptance import (
    StageOneAcceptanceFakeModel,
)
from tests.protocol_test_capability import (
    PROTOCOL_CAPABILITY_ID,
    PROTOCOL_REPLY,
)


def _protocol_manifest_document() -> CapabilityManifestDocument:
    """构造只供验收使用的第三方协议 Manifest 来源。"""

    return CapabilityManifestDocument(
        source=Path("tests/protocol_test_capability/manifest.yaml"),
        raw_document={
            "manifest_schema_version": 1,
            "capability_id": PROTOCOL_CAPABILITY_ID,
            "name": "第三方协议测试能力",
            "description": "验证任意合法 Capability ID 的正式产品执行链路。",
            "enabled": True,
            "entrypoint": (
                "tests.protocol_test_capability:create_capability"
            ),
            "version": "1.0.0",
            "state_scope": "invocation",
            "state_schema_version": "v1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 10.0,
                "cancel_grace_seconds": 1.0,
            },
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        },
        error=None,
    )


class _ProtocolCapabilitySource:
    """把单个测试 Manifest 注入正式 Registry 构建入口。"""

    def __init__(self, _root_directory: Path) -> None:
        self._document = _protocol_manifest_document()

    def load(self) -> list[CapabilityManifestDocument]:
        """返回稳定且互不共享可变容器的来源列表。"""

        return [self._document]


def _run_on_psycopg_compatible_loop(coroutine) -> None:
    """在 Windows 上使用 psycopg 支持的 SelectorEventLoop。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1"
    or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 和 RUN_REDIS_TESTS=1 后运行真实依赖测试",
)
def test_third_capability_id_traverses_formal_product_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第三个协议 ID 贯穿 HTTP、Registry、Run、SSE、Task 与公共历史。"""

    monkeypatch.setattr(
        chat_module,
        "LocalYamlCapabilitySource",
        _ProtocolCapabilitySource,
    )
    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        redis_url=base_settings.redis_url,
        local_user_id=f"s3-14-third-capability-{uuid4()}",
        run_coordinator_scan_interval_seconds=0.02,
        _env_file=None,
    )
    permission_repository = UserCapabilityPermissionRepository(settings)
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        await permission_repository.setup()
        now = datetime.now(UTC)
        await permission_repository.set_allowed(
            UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id=PROTOCOL_CAPABILITY_ID,
                allowed=True,
                created_at=now,
                updated_at=now,
            )
        )
        model = StageOneAcceptanceFakeModel()
        try:
            async with open_chat_service(settings, model=model) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://stage-three.test",
                ) as client:
                    response = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            "request_id": str(uuid4()),
                            "message": {"content": "执行第三方协议能力测试"},
                        },
                    )
                    assert response.status_code == 202, response.text
                    summary = response.json()
                    run_id = UUID(str(summary["run_id"]))
                    session_id = UUID(str(summary["session_id"]))

                    run_repository = PostgresRunRepository(settings)
                    for _ in range(500):
                        run = await run_repository.get(run_id)
                        if run.status in {"completed", "failed", "cancelled"}:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("第三方协议能力 Run 未在限定时间内完成")
                    assert run.status == "completed"

                    history_response = await client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                    assert history_response.status_code == 200
                    history = history_response.json()["items"]
                    assert history[-1]["role"] == "assistant"
                    assert history[-1]["content"] == PROTOCOL_REPLY
                    assert history[-1]["runtime_status"] == "completed"
                    assert (
                        history[-1]["capability_id"]
                        == PROTOCOL_CAPABILITY_ID
                    )

                    events_response = await client.get(
                        f"/api/v1/chat/runs/{run_id}/events"
                    )
                    assert events_response.status_code == 200
                    assert "event: message.delta" in events_response.text
                    assert "event: message.finalized" in events_response.text
                    assert "event: run.completed" in events_response.text
                    assert PROTOCOL_CAPABILITY_ID in events_response.text

                public_events = await PostgresRuntimeEventRepository(
                    settings
                ).list_public(run_id=run_id, after_seq=0, limit=100)
                finalized = [
                    event
                    for event in public_events
                    if event.event_type == "message.finalized"
                ]
                assert len(finalized) == 1
                assert finalized[0].payload["capability_id"] == (
                    PROTOCOL_CAPABILITY_ID
                )

                async with open_database_connection(settings) as connection:
                    event_cursor = await connection.execute(
                        """
                        SELECT event_type, payload
                        FROM runtime_events
                        WHERE run_id = %s
                          AND event_type IN (
                              'internal.capability.invocation.started',
                              'internal.capability.invocation.completed'
                          )
                        ORDER BY seq ASC
                        """,
                        (run_id,),
                    )
                    invocation_events = await event_cursor.fetchall()
                    task_cursor = await connection.execute(
                        """
                        SELECT capability_id, state_schema_version, status
                        FROM capability_tasks
                        WHERE session_id = %s
                        """,
                        (session_id,),
                    )
                    tasks = await task_cursor.fetchall()

                assert [row["event_type"] for row in invocation_events] == [
                    "internal.capability.invocation.started",
                    "internal.capability.invocation.completed",
                ]
                started_payload = invocation_events[0]["payload"]
                completed_payload = invocation_events[1]["payload"]
                assert started_payload["capability_id"] == PROTOCOL_CAPABILITY_ID
                assert started_payload["requested_task_action"] == "continue"
                assert started_payload["effective_task_action"] is None
                assert completed_payload["capability_id"] == PROTOCOL_CAPABILITY_ID
                assert completed_payload["requested_task_action"] == "continue"
                assert completed_payload["effective_task_action"] == "new"
                assert completed_payload["outcome"] == "completed"
                assert len(tasks) == 1
                assert tasks[0]["capability_id"] == PROTOCOL_CAPABILITY_ID
                assert tasks[0]["state_schema_version"] == "v1"
                assert tasks[0]["status"] == "active"
                assert model.route_calls == [
                    (
                        "执行第三方协议能力测试",
                        (PROTOCOL_CAPABILITY_ID,),
                        PROTOCOL_CAPABILITY_ID,
                    )
                ]
        finally:
            if session_id is not None:
                async with open_chat_service(settings, model=model) as cleanup:
                    await cleanup.delete_session(session_id=session_id)
            await permission_repository.revoke(
                user_id=settings.local_user_id,
                capability_id=PROTOCOL_CAPABILITY_ID,
            )

    _run_on_psycopg_compatible_loop(exercise())
