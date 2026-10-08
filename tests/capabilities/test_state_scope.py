from uuid import UUID, uuid4

import pytest


def test_child_thread_id_factory_builds_three_exact_scope_vectors() -> None:
    from agent_runtime.capabilities.state_scope import ChildThreadIdFactory

    session_id = UUID("00000000-0000-4000-8000-000000000101")
    run_id = UUID("00000000-0000-4000-8000-000000000102")
    invocation_id = UUID("00000000-0000-4000-8000-000000000103")
    task_id = UUID("00000000-0000-4000-8000-000000000104")
    factory = ChildThreadIdFactory()

    assert factory.build(
        state_scope="invocation",
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        run_id=run_id,
        invocation_id=invocation_id,
        task_id=task_id,
    ) == (
        "capability:v1:00000000-0000-4000-8000-000000000101:general_chat:"
        "schema:v1:invocation:00000000-0000-4000-8000-000000000103"
    )
    assert factory.build(
        state_scope="run",
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        run_id=run_id,
        invocation_id=invocation_id,
        task_id=task_id,
    ) == (
        "capability:v1:00000000-0000-4000-8000-000000000101:general_chat:"
        "schema:v1:run:00000000-0000-4000-8000-000000000102"
    )
    assert factory.build(
        state_scope="session",
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        run_id=run_id,
        invocation_id=invocation_id,
        task_id=task_id,
    ) == (
        "capability:v1:00000000-0000-4000-8000-000000000101:general_chat:"
        "schema:v1:task:00000000-0000-4000-8000-000000000104"
    )


def test_child_thread_id_factory_requires_only_the_selected_scope_key() -> None:
    from agent_runtime.capabilities.state_scope import ChildThreadIdFactory

    factory = ChildThreadIdFactory()
    common = {
        "session_id": uuid4(),
        "capability_id": "en_to_zh",
        "state_schema_version": "2026.10",
    }

    invocation_thread = factory.build(
        state_scope="invocation",
        invocation_id=uuid4(),
        **common,
    )
    run_thread = factory.build(
        state_scope="run",
        run_id=uuid4(),
        **common,
    )
    task_thread = factory.build(
        state_scope="session",
        task_id=uuid4(),
        **common,
    )

    assert ":invocation:" in invocation_thread
    assert ":run:" in run_thread
    assert ":task:" in task_thread

    with pytest.raises(ValueError, match="invocation_id"):
        factory.build(state_scope="invocation", **common)
    with pytest.raises(ValueError, match="run_id"):
        factory.build(state_scope="run", **common)
    with pytest.raises(ValueError, match="task_id"):
        factory.build(state_scope="session", **common)


def test_state_compatibility_policy_accepts_current_and_declared_old_version() -> None:
    from agent_runtime.capabilities.state_scope import StateCompatibilityPolicy

    policy = StateCompatibilityPolicy(
        current_version="v3",
        compatible_versions=("v1", "v2"),
    )

    assert policy.is_compatible("v3") is True
    assert policy.is_compatible("v2") is True
    assert policy.allowed_versions == frozenset({"v1", "v2", "v3"})


def test_state_compatibility_policy_rejects_without_mutating_task() -> None:
    from agent_runtime.capabilities.state_scope import (
        CapabilityStateVersionIncompatibleError,
        StateCompatibilityPolicy,
    )

    policy = StateCompatibilityPolicy(
        current_version="v3",
        compatible_versions=("v2",),
    )

    with pytest.raises(CapabilityStateVersionIncompatibleError) as caught:
        policy.ensure_compatible("v1")

    assert caught.value.code == "CAPABILITY_STATE_VERSION_INCOMPATIBLE"
    assert caught.value.retryable is False


def test_build_for_manifest_uses_compatible_task_frozen_schema_version() -> None:
    from datetime import UTC, datetime

    from agent_runtime.capabilities.persistence_models import CapabilityTask
    from agent_runtime.capabilities.state_scope import ChildThreadIdFactory

    from tests.capabilities.test_task_service import _manifest

    session_id = uuid4()
    task = CapabilityTask(
        task_id=uuid4(),
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        status="active",
        last_run_id=uuid4(),
        last_invocation_id=uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        ended_at=None,
    )
    manifest = _manifest(state_schema_version="v2", compatible=("v1",))
    factory = ChildThreadIdFactory()

    first = factory.build_for_manifest(
        manifest=manifest,
        task=task,
        run_id=uuid4(),
        invocation_id=uuid4(),
    )
    second = factory.build_for_manifest(
        manifest=manifest,
        task=task,
        run_id=uuid4(),
        invocation_id=uuid4(),
    )

    assert first == second
    assert ":schema:v1:task:" in first
    assert ":schema:v2:" not in first
