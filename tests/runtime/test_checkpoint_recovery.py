"""S2.5-08 Run Checkpoint 精确对账与恢复安全测试。"""

import asyncio
import os
from types import SimpleNamespace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest


def _run(run_number: int, *, session_id: UUID):
    from agent_runtime.runtime.models import Run

    now = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
    return Run(
        run_id=UUID(f"20000000-0000-0000-0000-{run_number:012d}"),
        request_id=UUID(f"30000000-0000-0000-0000-{run_number:012d}"),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=UUID(
            f"40000000-0000-0000-0000-{run_number:012d}"
        ),
        response_message_id=UUID(
            f"50000000-0000-0000-0000-{run_number:012d}"
        ),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "恢复测试"}},
        request_fingerprint="a" * 64,
        status="running",
        recovery_attempts=0,
        seq_high_watermark=0,
        error_code=None,
        error_message=None,
        created_at=now,
        started_at=now,
        finished_at=None,
        updated_at=now,
    )


def test_checkpoint_inspector_finds_exact_latest_checkpoint_for_run() -> None:
    """较新的其他 Run 快照不能覆盖目标 Run 的精确恢复起点。"""

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.checkpoint_recovery import (
        RunCheckpointInspector,
    )

    session_id = UUID("10000000-0000-0000-0000-000000000801")
    first_run = _run(801, session_id=session_id)
    second_run = _run(802, session_id=session_id)

    def finish(_state):
        return {"completion_status": "completed"}

    builder = StateGraph(ParentState)
    builder.add_node("finish", finish)
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        for run in (first_run, second_run):
            await graph.ainvoke(
                {
                    "messages": [
                        HumanMessage(
                            content="恢复测试",
                            id=str(run.input_message_id),
                        )
                    ]
                },
                parent_thread_config(
                    session_id,
                    message_id=run.response_message_id,
                    run_id=run.run_id,
                    response_message_id=run.response_message_id,
                ),
            )

        inspector = RunCheckpointInspector(parent_graph=graph)
        snapshot = await inspector.latest_for_run(first_run)

        assert snapshot is not None
        assert snapshot.metadata["run_id"] == str(first_run.run_id)
        assert snapshot.metadata["response_message_id"] == str(
            first_run.response_message_id
        )
        assert snapshot.metadata["run_id"] != str(second_run.run_id)

        recovery_config = inspector.recovery_config(first_run, snapshot)
        assert recovery_config["configurable"]["checkpoint_id"] == (
            snapshot.config["configurable"]["checkpoint_id"]
        )
        assert recovery_config["configurable"]["message_id"] == str(
            first_run.response_message_id
        )
        assert recovery_config["metadata"] == {
            "run_id": str(first_run.run_id),
            "response_message_id": str(first_run.response_message_id),
        }

    asyncio.run(exercise())


def test_checkpoint_inspector_projects_stable_final_message() -> None:
    """只有稳定回复标识匹配的非空公共终态消息可直接补投影。"""

    from langchain_core.messages import AIMessage

    from agent_runtime.runtime.checkpoint_recovery import (
        recovered_final_message,
    )

    session_id = UUID("10000000-0000-0000-0000-000000000803")
    run = _run(803, session_id=session_id)
    snapshot = SimpleNamespace(
        values={
            "messages": [
                AIMessage(
                    content="已完成回复",
                    id=str(run.response_message_id),
                    additional_kwargs={
                        "runtime_status": "completed",
                        "capability_id": "general_chat",
                    },
                )
            ]
        }
    )

    recovered = recovered_final_message(run, snapshot)

    assert recovered is not None
    assert recovered.runtime_status == "completed"
    assert recovered.capability_id == "general_chat"


def test_checkpoint_recovery_rejects_unknown_side_effect_capability() -> None:
    """当前阶段未声明幂等保障的能力不得进入自动恢复。"""

    from agent_runtime.runtime.checkpoint_recovery import (
        RunCheckpointError,
        ensure_automatic_recovery_safe,
    )

    snapshot = SimpleNamespace(
        values={"resolved_capability_id": "external_side_effect_agent"}
    )

    with pytest.raises(
        RunCheckpointError,
        match="副作用幂等保障",
    ):
        ensure_automatic_recovery_safe(snapshot)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_recovery_claim_is_atomic_and_limited_to_three_attempts() -> None:
    """恢复接管的状态、计数与事件必须原子提交，第四次接管被拒绝。"""

    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import (
        RunRecoveryActivatedPayload,
        RunRecoveryClaimedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import (
        PostgresRunRepository,
        RunStateConflictError,
    )
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime.now(UTC)
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "恢复次数测试"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "恢复次数测试"}},
        ),
        created_at=now,
    )
    sessions = PostgresSessionRepository(settings)
    runs = PostgresRunRepository(settings)
    events = PostgresRuntimeEventRepository(settings)

    def draft(event_type, payload):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.recovery",
            visibility=(
                "public" if event_type == "run.started" else "internal"
            ),
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )

    async def exercise() -> None:
        await sessions.setup()
        await runs.setup()
        await events.setup()
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="恢复次数测试",
                created_at=now,
                updated_at=now,
            )
        )
        await runs.create_or_get(submission)
        sequencer = RunSequencer.for_run(
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
            repository=events,
        )
        await sequencer.transition_run(
            target_status="running",
            event=draft("run.started", RunStartedPayload(status="running")),
            updated_at=datetime.now(UTC),
        )
        try:
            for attempt in range(1, 4):
                claimed = await sequencer.claim_recovery(
                    event=draft(
                        "internal.run.recovery_claimed",
                        RunRecoveryClaimedPayload(
                            status="recovering",
                            recovery_attempt=attempt,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                )
                assert claimed.run.status == "recovering"
                assert claimed.run.recovery_attempts == attempt
                if attempt == 1:
                    with pytest.raises(
                        RunStateConflictError,
                        match="权威计数不一致",
                    ):
                        await sequencer.transition_run(
                            target_status="running",
                            event=draft(
                                "internal.run.recovery_activated",
                                RunRecoveryActivatedPayload(
                                    status="running",
                                    recovery_attempt=2,
                                ),
                            ),
                            updated_at=datetime.now(UTC),
                        )
                    unchanged = await runs.get(submission.run_id)
                    assert unchanged.status == "recovering"
                    assert unchanged.recovery_attempts == 1
                await sequencer.transition_run(
                    target_status="running",
                    event=draft(
                        "internal.run.recovery_activated",
                        RunRecoveryActivatedPayload(
                            status="running",
                            recovery_attempt=attempt,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                )

            with pytest.raises(
                RunStateConflictError,
                match="三次恢复上限",
            ):
                await sequencer.claim_recovery(
                    event=draft(
                        "internal.run.recovery_claimed",
                        RunRecoveryClaimedPayload(
                            status="recovering",
                            recovery_attempt=3,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                )

            current = await runs.get(submission.run_id)
            assert current.status == "running"
            assert current.recovery_attempts == 3
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM runtime_events
                    WHERE run_id = %s
                      AND event_type = 'internal.run.recovery_claimed'
                    """,
                    (submission.run_id,),
                )
                row = await cursor.fetchone()
                assert row["total"] == 3
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_postgres_checkpoint_restart_filters_exact_run_metadata() -> None:
    """重开 PostgreSQL Saver 后仍须按 run_id 找到目标 Run 的最新快照。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.checkpoint_recovery import (
        RunCheckpointInspector,
        recovered_final_message,
    )

    settings = Settings()
    session_id = uuid4()
    first_run = _run(804, session_id=session_id)
    second_run = _run(805, session_id=session_id)

    def finish_node(_state, config):
        return {
            "messages": [
                AIMessage(
                    content="PostgreSQL Checkpoint 已完成。",
                    id=config["configurable"]["message_id"],
                    additional_kwargs={
                        "runtime_status": "completed",
                        "capability_id": "general_chat",
                    },
                )
            ],
            "completion_status": "completed",
        }

    def build_graph(checkpointer):
        builder = StateGraph(ParentState)
        builder.add_node("finish", finish_node)
        builder.add_edge(START, "finish")
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=checkpointer)

    async def exercise() -> None:
        try:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as first_saver:
                await first_saver.setup()
                first_graph = build_graph(first_saver)
                for run in (first_run, second_run):
                    await first_graph.ainvoke(
                        {
                            "messages": [
                                HumanMessage(
                                    content="精确恢复测试",
                                    id=str(run.input_message_id),
                                )
                            ]
                        },
                        parent_thread_config(
                            session_id,
                            message_id=run.response_message_id,
                            run_id=run.run_id,
                            response_message_id=run.response_message_id,
                        ),
                    )

            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as restarted_saver:
                restarted_graph = build_graph(restarted_saver)
                inspector = RunCheckpointInspector(
                    parent_graph=restarted_graph
                )
                snapshot = await inspector.latest_for_run(first_run)
                assert snapshot is not None
                assert snapshot.metadata["run_id"] == str(first_run.run_id)
                assert snapshot.metadata["run_id"] != str(second_run.run_id)
                recovered = recovered_final_message(first_run, snapshot)
                assert recovered is not None
                assert recovered.runtime_status == "completed"
                await restarted_saver.adelete_thread(str(session_id))
        finally:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as cleanup_saver:
                await cleanup_saver.adelete_thread(str(session_id))

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
