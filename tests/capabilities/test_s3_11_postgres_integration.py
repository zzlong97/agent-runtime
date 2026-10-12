"""S3-11 Regenerate 安全门禁的真实 PostgreSQL 与 Checkpointer 集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


def _manifest(*, state_scope: str = "invocation", side_effect_policy: str = "none"):
    """构造本阶段安全或不安全组合的严格 Manifest。"""

    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "general_chat",
            "name": "通用对话",
            "description": "用于验证真实重新生成门禁。",
            "enabled": True,
            "entrypoint": "tests.fake_capability:create_capability",
            "version": "1.0.0",
            "state_scope": state_scope,
            "state_schema_version": "1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 30.0,
                "cancel_grace_seconds": 1.0,
            },
            "recovery_policy": (
                "manual" if side_effect_policy == "unsafe" else "automatic"
            ),
            "side_effect_policy": side_effect_policy,
        }
    )


class _Registry:
    """只向门禁暴露一个 active 且健康可服务的测试 Capability。"""

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def get(self, capability_id: str):
        if capability_id != self.manifest.capability_id:
            return None
        return SimpleNamespace(manifest=self.manifest)

    async def serviceable_router_projections(self):
        return (SimpleNamespace(capability_id=self.manifest.capability_id),)


class _Coordinator:
    """记录提交唤醒，不启动后台执行器。"""

    def __init__(self) -> None:
        self.wakes: list[UUID] = []

    async def wake(self, run_id: UUID) -> None:
        self.wakes.append(run_id)


class _StaticHistory:
    """为拒绝路径提供不触碰 Checkpointer 的确定公共历史。"""

    def __init__(self, *, human_id: UUID, response_id: UUID) -> None:
        self.human_id = human_id
        self.response_id = response_id

    async def get_active_messages(self, *, session_id: UUID):
        from agent_runtime.history import ProductMessage

        return (
            ProductMessage(
                message_id=self.human_id,
                role="user",
                content="测试输入",
                runtime_status=None,
                capability_id=None,
                feedback=None,
            ),
            ProductMessage(
                message_id=self.response_id,
                role="assistant",
                content="测试回答",
                runtime_status="completed",
                capability_id="general_chat",
                feedback=None,
            ),
        )


class _RejectPathForker:
    """拒绝路径若错误进入 Parent Fork 立即使测试失败。"""

    async def find_start_checkpoint(self, **_kwargs):
        raise AssertionError("安全门禁拒绝后不得读取或写入 Parent Checkpoint")


class _GraphParentStateStore:
    """让历史 Adapter 直接读取同一真实 Parent Graph。"""

    def __init__(self, graph) -> None:
        self.graph = graph

    async def get_messages(self, session_id: UUID):
        from agent_runtime.graph.config import parent_thread_config

        state = await self.graph.aget_state(parent_thread_config(session_id))
        return list(state.values["messages"])


async def _setup_repositories(settings):
    """初始化 S3-11 测试涉及的既有表。"""

    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.repository import PostgresSessionRepository

    sessions = PostgresSessionRepository(settings)
    runs = PostgresRunRepository(settings)
    events = PostgresRuntimeEventRepository(settings)
    tasks = CapabilityTaskRepository(settings)
    contexts = CapabilityTaskContextRepository(settings)
    operations = CapabilityOperationRepository(settings)
    permissions = UserCapabilityPermissionRepository(settings)
    for repository in (
        sessions,
        runs,
        events,
        tasks,
        contexts,
        operations,
        permissions,
    ):
        await repository.setup()
    return sessions, runs, events, tasks, contexts, operations, permissions


async def _delete_case(settings, *, session_id: UUID, user_id: str) -> None:
    """按外键顺序清理测试创建的业务记录。"""

    from agent_runtime.persistence.database import open_database_connection

    async with open_database_connection(settings) as connection:
        await connection.execute(
            "DELETE FROM capability_task_contexts WHERE session_id = %s",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM capability_operations WHERE run_id IN "
            "(SELECT run_id FROM runs WHERE session_id = %s)",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM capability_tasks WHERE session_id = %s",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM runtime_events WHERE run_id IN "
            "(SELECT run_id FROM runs WHERE session_id = %s)",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM runs WHERE session_id = %s",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM user_capability_permissions WHERE user_id = %s",
            (user_id,),
        )
        await connection.execute(
            "DELETE FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        await connection.commit()


def test_api_rejects_unsafe_regenerate_before_any_runtime_persistence() -> None:
    """真实 API 409 路径不新增 Run、Task、Invocation、Operation 或事件。"""

    async def exercise() -> None:
        from agent_runtime.capabilities.persistence import (
            CapabilityTaskContextRepository,
            CapabilityTaskRepository,
            UserCapabilityPermissionRepository,
        )
        from agent_runtime.capabilities.regeneration import (
            RegenerateSubmissionValidator,
        )
        from agent_runtime.capabilities.routing import CapabilityPermissionService
        from agent_runtime.chat import ChatService
        from agent_runtime.core.config import Settings
        from agent_runtime.main import create_app
        from agent_runtime.persistence.database import open_database_connection
        from agent_runtime.sessions.models import Session

        base = Settings()
        user_id = f"s3-11-reject-{uuid4()}"
        settings = Settings(
            database_url=base.database_url,
            local_user_id=user_id,
            _env_file=None,
        )
        session_id = uuid4()
        human_id = uuid4()
        response_id = uuid4()
        request_id = uuid4()
        now = datetime.now(UTC)
        sessions, runs, _events, tasks, contexts, _operations, permissions = (
            await _setup_repositories(settings)
        )
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=user_id,
                title="S3-11 拒绝测试",
                created_at=now,
                updated_at=now,
            )
        )
        validator = RegenerateSubmissionValidator(
            registry=_Registry(_manifest(state_scope="run")),
            permission_service=CapabilityPermissionService(permissions),
            task_context_repository=contexts,
            task_repository=tasks,
        )
        coordinator = _Coordinator()
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=object(),
            parent_graph=object(),
            history_adapter=_StaticHistory(
                human_id=human_id,
                response_id=response_id,
            ),
            checkpoint_forker=_RejectPathForker(),
            persistent_run_repository=runs,
            run_coordinator=coordinator,
            regenerate_submission_validator=validator,
        )
        app = create_app(chat_service=service)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/v1/chat/sessions/{session_id}/messages/"
                    f"{response_id}/regenerate",
                    json={"request_id": str(request_id)},
                )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == (
                "CAPABILITY_REGENERATE_UNSUPPORTED"
            )
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM runs WHERE session_id = %s) AS runs,
                      (SELECT count(*) FROM capability_tasks WHERE session_id = %s) AS tasks,
                      (SELECT count(*) FROM capability_task_contexts WHERE session_id = %s) AS contexts,
                      (SELECT count(*) FROM capability_operations WHERE run_id IN
                        (SELECT run_id FROM runs WHERE session_id = %s)) AS operations,
                      (SELECT count(*) FROM runtime_events WHERE run_id IN
                        (SELECT run_id FROM runs WHERE session_id = %s)) AS events
                    """,
                    (session_id, session_id, session_id, session_id, session_id),
                )
                counts = await cursor.fetchone()
            assert dict(counts) == {
                "runs": 0,
                "tasks": 0,
                "contexts": 0,
                "operations": 0,
                "events": 0,
            }
            assert coordinator.wakes == []
        finally:
            await _delete_case(settings, session_id=session_id, user_id=user_id)

    run_on_psycopg_compatible_loop(exercise())


def test_legal_regenerate_forks_real_parent_and_uses_new_invocation_thread() -> None:
    """合法路径复用 current Task，但 Parent fork 和 Child Invocation 身份彼此隔离。"""

    async def exercise() -> None:
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from agent_runtime.capabilities.invocation_service import (
            CapabilityInvocationService,
        )
        from agent_runtime.capabilities.persistence_models import (
            CapabilityTask,
            UserCapabilityPermission,
        )
        from agent_runtime.capabilities.regeneration import (
            RegenerateSubmissionValidator,
        )
        from agent_runtime.capabilities.routing import CapabilityPermissionService
        from agent_runtime.capabilities.state_scope import ChildThreadIdFactory
        from agent_runtime.capabilities.task_service import CapabilityTaskService
        from agent_runtime.chat import ChatService
        from agent_runtime.core.config import Settings
        from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
        from agent_runtime.graph.config import parent_thread_config
        from agent_runtime.graph.parent import build_parent_graph
        from agent_runtime.history import MessageHistoryAdapter
        from agent_runtime.regeneration import CheckpointForker
        from agent_runtime.runtime.sequencer import RunSequencer
        from agent_runtime.sessions.models import Session

        base = Settings()
        user_id = f"s3-11-legal-{uuid4()}"
        settings = Settings(
            database_url=base.database_url,
            local_user_id=user_id,
            _env_file=None,
        )
        session_id = uuid4()
        human_id = uuid4()
        source_response_id = uuid4()
        regenerated_response_id = uuid4()
        regenerate_run_id = uuid4()
        request_id = uuid4()
        task_id = uuid4()
        original_invocation_id = uuid4()
        now = datetime.now(UTC)
        manifest = _manifest()
        sessions, runs, events, tasks, contexts, _operations, permissions = (
            await _setup_repositories(settings)
        )
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=user_id,
                title="S3-11 合法测试",
                created_at=now,
                updated_at=now,
            )
        )
        await permissions.set_allowed(
            UserCapabilityPermission(
                user_id=user_id,
                capability_id="general_chat",
                allowed=True,
                created_at=now,
                updated_at=now,
            )
        )
        original_task = await tasks.create_and_set_current(
            CapabilityTask(
                task_id=task_id,
                session_id=session_id,
                capability_id="general_chat",
                state_schema_version="1",
                status="active",
                last_run_id=None,
                last_invocation_id=original_invocation_id,
                created_at=now,
                updated_at=now,
                ended_at=None,
            )
        )
        route_calls = 0

        async def route(_state, _config):
            nonlocal route_calls
            route_calls += 1
            return {
                "resolved_capability_id": "general_chat",
                "task_action": "continue",
            }

        async def invoke_capability(_state, config):
            return CapabilityInvocation(
                result=ChildResult(status="completed", control_signal=None),
                message=AIMessage(
                    content="原回答",
                    id=config["configurable"]["message_id"],
                    additional_kwargs={
                        "runtime_status": "completed",
                        "capability_id": "general_chat",
                    },
                ),
            )

        coordinator = _Coordinator()
        async with AsyncPostgresSaver.from_conn_string(
            settings.database_connection_string
        ) as checkpointer:
            await checkpointer.setup()
            graph = build_parent_graph(
                route=route,
                invoke_capability=invoke_capability,
                checkpointer=checkpointer,
            )
            history = MessageHistoryAdapter(
                settings=settings,
                session_repository=sessions,
                parent_state_store=_GraphParentStateStore(graph),
            )
            validator = RegenerateSubmissionValidator(
                registry=_Registry(manifest),
                permission_service=CapabilityPermissionService(permissions),
                task_context_repository=contexts,
                task_repository=tasks,
            )
            service = ChatService(
                settings=settings,
                session_repository=sessions,
                session_service=object(),
                parent_graph=graph,
                history_adapter=history,
                checkpoint_forker=CheckpointForker(
                    history_adapter=history,
                    parent_graph=graph,
                ),
                persistent_run_repository=runs,
                run_coordinator=coordinator,
                regenerate_submission_validator=validator,
                response_message_id_factory=lambda: regenerated_response_id,
                run_id_factory=lambda: regenerate_run_id,
            )
            try:
                await graph.ainvoke(
                    {
                        "messages": [
                            HumanMessage(content="请回答", id=str(human_id))
                        ]
                    },
                    parent_thread_config(
                        session_id,
                        message_id=source_response_id,
                    ),
                )
                run = await service.submit_regeneration_run(
                    request_id=request_id,
                    session_id=session_id,
                    message_id=source_response_id,
                )
                turn = await service._turn_from_run(run)
                sequencer = RunSequencer.for_run(
                    run_id=run.run_id,
                    response_message_id=run.response_message_id,
                    repository=events,
                )
                invocation_service = CapabilityInvocationService(
                    sequencer=sequencer
                )
                invocation = await invocation_service.start(
                    capability_id="general_chat",
                    task_action="continue",
                    state_scope="invocation",
                    created_at=now + timedelta(seconds=1),
                )
                task_service = CapabilityTaskService(
                    repository=tasks,
                    sequencer=sequencer,
                    invocation_service=invocation_service,
                )
                resolution = await task_service.resolve(
                    manifest=manifest,
                    session_id=session_id,
                    run_id=run.run_id,
                    invocation_id=invocation.invocation_id,
                    task_action=invocation.task_action,
                    updated_at=now + timedelta(seconds=2),
                )
                thread_factory = ChildThreadIdFactory()
                original_thread_id = thread_factory.build_for_manifest(
                    manifest=manifest,
                    task=original_task,
                    run_id=run.run_id,
                    invocation_id=original_invocation_id,
                )
                regenerated_thread_id = thread_factory.build_for_manifest(
                    manifest=manifest,
                    task=resolution.task,
                    run_id=run.run_id,
                    invocation_id=invocation.invocation_id,
                )

                assert run.input_payload == {
                    "source_message_id": str(source_response_id),
                    "capability_id": "general_chat",
                    "task_action": "continue",
                }
                assert run.start_checkpoint_id is not None
                assert turn.config["configurable"]["checkpoint_id"] != (
                    run.start_checkpoint_id
                )
                assert turn.config["configurable"]["message_id"] == str(
                    regenerated_response_id
                )
                assert route_calls == 1
                assert resolution.task.task_id == task_id
                assert resolution.requested_action == "continue"
                assert resolution.resolved_action == "continue"
                assert invocation.invocation_id != original_invocation_id
                assert regenerated_thread_id != original_thread_id
                assert regenerated_thread_id.endswith(
                    f"invocation:{invocation.invocation_id}"
                )
                assert coordinator.wakes == [run.run_id]
            finally:
                await checkpointer.adelete_thread(str(session_id))
                await _delete_case(
                    settings,
                    session_id=session_id,
                    user_id=user_id,
                )

    run_on_psycopg_compatible_loop(exercise())
