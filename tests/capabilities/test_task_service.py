import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest


def _manifest(*, state_schema_version="v2", compatible=("v1",)):
    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "general_chat",
            "name": "普通聊天",
            "description": "处理普通聊天，不执行翻译。",
            "enabled": True,
            "entrypoint": "agent_runtime.capabilities.general_chat.adapter:factory",
            "version": "1.0.0",
            "state_scope": "session",
            "state_schema_version": state_schema_version,
            "compatible_state_schema_versions": list(compatible),
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
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        }
    )


class FakeBatchSequencer:
    def __init__(self, run_id):
        self.run_id = run_id
        self.seq = 0

    async def commit_durable_internal_batch(self, *, drafts, commit):
        from agent_runtime.runtime.event_models import RuntimeEvent
        from agent_runtime.runtime.event_schemas import (
            serialize_event_draft_payload,
        )

        events = []
        for draft in drafts:
            self.seq += 1
            events.append(
                RuntimeEvent(
                    event_id=uuid4(),
                    run_id=self.run_id,
                    seq=self.seq,
                    event_type=draft.event_type,
                    source=draft.source,
                    visibility=draft.visibility,
                    payload=serialize_event_draft_payload(draft),
                    schema_version=1,
                    durability=draft.durability,
                    created_at=draft.created_at,
                )
            )
        return await commit(tuple(events))


class FakeTaskRepository:
    def __init__(self):
        self.resolve_arguments = None
        self.transition_arguments = None
        self.rollback_arguments = None
        self.operation_exists = False

    async def resolve_for_invocation(self, **arguments):
        from agent_runtime.capabilities.persistence_models import (
            CapabilityTask,
            CapabilityTaskResolution,
        )

        self.resolve_arguments = arguments
        task = CapabilityTask(
            task_id=arguments["candidate_task_id"],
            session_id=arguments["session_id"],
            capability_id=arguments["capability_id"],
            state_schema_version=arguments["state_schema_version"],
            status="active",
            last_run_id=arguments["run_id"],
            last_invocation_id=arguments["invocation_id"],
            created_at=arguments["updated_at"],
            updated_at=arguments["updated_at"],
            ended_at=None,
        )
        return CapabilityTaskResolution(
            task=task,
            requested_action=arguments["requested_action"],
            resolved_action="new",
            previous_task_id=None,
            provisional=True,
            state_scope="session",
        )

    async def transition_for_invocation(self, **arguments):
        from dataclasses import replace

        self.transition_arguments = arguments
        task = self.last_resolution.task
        return replace(
            task,
            status=arguments["status"],
            updated_at=arguments["updated_at"],
            ended_at=arguments["updated_at"],
        )

    async def rollback_rejected_new(self, **arguments):
        from agent_runtime.capabilities.persistence import (
            CapabilityTaskContractViolationError,
        )

        self.rollback_arguments = arguments
        if self.operation_exists:
            raise CapabilityTaskContractViolationError(
                code="CAPABILITY_EXECUTION_FAILED",
                message="存在 Operation",
                status_code=409,
            )
        return arguments["task_event"], arguments["invocation_event"]


class FakeInvocationService:
    def __init__(self):
        self.failures = []

    async def fail(self, **arguments):
        self.failures.append(arguments)


class FakeCheckpointStore:
    def __init__(self, *, exists=False):
        self.exists = exists
        self.deleted = []

    async def has_checkpoint(self, thread_id):
        return self.exists

    async def delete_thread(self, thread_id):
        self.deleted.append(thread_id)


def test_task_service_resolve_builds_fixed_events_and_compatibility_set() -> None:
    from agent_runtime.capabilities.task_service import CapabilityTaskService

    run_id = uuid4()
    repository = FakeTaskRepository()
    service = CapabilityTaskService(
        repository=repository,
        sequencer=FakeBatchSequencer(run_id),
        invocation_service=FakeInvocationService(),
    )

    async def exercise() -> None:
        resolution = await service.resolve(
            manifest=_manifest(),
            session_id=uuid4(),
            run_id=run_id,
            invocation_id=uuid4(),
            task_action="continue",
            updated_at=datetime.now(UTC),
        )
        repository.last_resolution = resolution
        assert resolution.provisional is True
        assert resolution.state_scope == "session"
        assert repository.resolve_arguments[
            "compatible_state_schema_versions"
        ] == frozenset({"v1", "v2"})
        assert [
            event.event_type for event in repository.resolve_arguments["events"]
        ] == [
            "internal.capability.task.created",
            "internal.capability.task.current_changed",
            "internal.capability.task.continue_degraded_to_new",
        ]

    asyncio.run(exercise())


def test_task_service_only_applies_explicit_terminal_intent() -> None:
    from agent_runtime.capabilities.task_service import CapabilityTaskService

    run_id = uuid4()
    invocation_id = uuid4()
    repository = FakeTaskRepository()
    service = CapabilityTaskService(
        repository=repository,
        sequencer=FakeBatchSequencer(run_id),
        invocation_service=FakeInvocationService(),
    )

    async def exercise() -> None:
        resolution = await service.resolve(
            manifest=_manifest(),
            session_id=uuid4(),
            run_id=run_id,
            invocation_id=invocation_id,
            task_action="new",
            updated_at=datetime.now(UTC),
        )
        repository.last_resolution = resolution
        kept = await service.apply_transition(
            task=resolution.task,
            task_transition="keep_active",
            run_id=run_id,
            invocation_id=invocation_id,
            updated_at=datetime.now(UTC),
        )
        assert kept.status == "active"
        assert repository.transition_arguments is None

        completed = await service.apply_transition(
            task=resolution.task,
            task_transition="completed",
            run_id=run_id,
            invocation_id=invocation_id,
            updated_at=datetime.now(UTC),
        )
        assert completed.status == "completed"
        assert repository.transition_arguments["event"].event_type == (
            "internal.capability.task.completed"
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("public_output_emitted", "checkpoint_exists", "reason"),
    [
        (True, False, "PUBLIC_OUTPUT"),
        (False, True, "CHILD_CHECKPOINT"),
    ],
)
def test_rejected_new_with_business_fact_fails_without_task_rollback(
    public_output_emitted,
    checkpoint_exists,
    reason,
) -> None:
    from agent_runtime.capabilities.persistence import (
        CapabilityTaskContractViolationError,
    )
    from agent_runtime.capabilities.task_service import CapabilityTaskService

    run_id = uuid4()
    invocation_id = uuid4()
    repository = FakeTaskRepository()
    invocation_service = FakeInvocationService()
    service = CapabilityTaskService(
        repository=repository,
        sequencer=FakeBatchSequencer(run_id),
        invocation_service=invocation_service,
    )
    checkpoint_store = FakeCheckpointStore(exists=checkpoint_exists)

    async def exercise() -> None:
        resolution = await service.resolve(
            manifest=_manifest(),
            session_id=uuid4(),
            run_id=run_id,
            invocation_id=invocation_id,
            task_action="new",
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(CapabilityTaskContractViolationError):
            await service.rollback_rejected_new(
                resolution=resolution,
                run_id=run_id,
                invocation_id=invocation_id,
                thread_id="capability:v1:test",
                public_output_emitted=public_output_emitted,
                checkpoint_store=checkpoint_store,
                updated_at=datetime.now(UTC),
            )
        assert repository.rollback_arguments is None
        assert checkpoint_store.deleted == []
        assert invocation_service.failures[0]["error_code"] == (
            "CAPABILITY_EXECUTION_FAILED"
        )
        assert invocation_service.failures[0]["requested_task_action"] == "new"
        assert invocation_service.failures[0]["effective_task_action"] == "new"
        assert invocation_service.failures[0]["state_scope"] == "session"
        assert invocation_service.failures[0]["state_schema_version"] == "v2"

    asyncio.run(exercise())


def test_rejected_new_without_business_fact_rolls_back_and_deletes_thread() -> None:
    from agent_runtime.capabilities.task_service import CapabilityTaskService

    run_id = uuid4()
    invocation_id = uuid4()
    repository = FakeTaskRepository()
    invocation_service = FakeInvocationService()
    service = CapabilityTaskService(
        repository=repository,
        sequencer=FakeBatchSequencer(run_id),
        invocation_service=invocation_service,
    )
    checkpoint_store = FakeCheckpointStore()

    async def exercise() -> None:
        resolution = await service.resolve(
            manifest=_manifest(),
            session_id=uuid4(),
            run_id=run_id,
            invocation_id=invocation_id,
            task_action="new",
            updated_at=datetime.now(UTC),
        )
        events = await service.rollback_rejected_new(
            resolution=resolution,
            run_id=run_id,
            invocation_id=invocation_id,
            thread_id="capability:v1:test",
            public_output_emitted=False,
            checkpoint_store=checkpoint_store,
            updated_at=datetime.now(UTC),
        )
        assert [event.event_type for event in events] == [
            "internal.capability.task.rejected_rolled_back",
            "internal.capability.invocation.completed",
        ]
        invocation_payload = events[1].payload
        assert invocation_payload["requested_task_action"] == "new"
        assert invocation_payload["effective_task_action"] == "new"
        assert invocation_payload["state_scope"] == "session"
        assert invocation_payload["state_schema_version"] == "v2"
        assert checkpoint_store.deleted == ["capability:v1:test"]
        assert invocation_service.failures == []

    asyncio.run(exercise())
