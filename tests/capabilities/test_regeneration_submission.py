"""S3-11 Regenerate 提交边界与幂等顺序测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_runtime.capabilities.regeneration import (
    CapabilityRegenerateUnsupportedError,
    RegenerateSubmissionDecision,
)
from agent_runtime.history import ProductMessage
from agent_runtime.runtime.models import Run, RunCreateResult, RunSubmission
from agent_runtime.runtime.repository import RunNotFoundError
from agent_runtime.sessions.models import Session


SESSION_ID = UUID("00000000-0000-0000-0000-000000003120")
HUMAN_ID = UUID("00000000-0000-0000-0000-000000003121")
SOURCE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000003122")
RESPONSE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000003123")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000003124")
RUN_ID = UUID("00000000-0000-0000-0000-000000003125")
TASK_ID = UUID("00000000-0000-0000-0000-000000003126")
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


class _SessionRepository:
    async def get(self, session_id: UUID) -> Session:
        assert session_id == SESSION_ID
        return Session(
            session_id=session_id,
            user_id="runtime-user",
            title="会话",
            created_at=NOW,
            updated_at=NOW,
        )


class _History:
    async def get_active_messages(self, *, session_id: UUID):
        assert session_id == SESSION_ID
        return (
            ProductMessage(
                message_id=HUMAN_ID,
                role="user",
                content="消息",
                runtime_status=None,
                capability_id=None,
                feedback=None,
            ),
            ProductMessage(
                message_id=SOURCE_MESSAGE_ID,
                role="assistant",
                content="回答",
                runtime_status="completed",
                capability_id="general_chat",
                feedback=None,
            ),
        )


class _CheckpointForker:
    def __init__(self) -> None:
        self.calls = 0

    async def find_start_checkpoint(self, *, session_id: UUID, message_id: UUID):
        self.calls += 1
        assert session_id == SESSION_ID
        assert message_id == SOURCE_MESSAGE_ID
        return {
            "configurable": {
                "thread_id": str(SESSION_ID),
                "checkpoint_ns": "",
                "checkpoint_id": "before-answer",
            }
        }


class _Repository:
    def __init__(self, *, existing: Run | None = None) -> None:
        self.existing = existing
        self.submissions: list[RunSubmission] = []

    async def get_by_request_id(self, request_id: UUID) -> Run:
        assert request_id == REQUEST_ID
        if self.existing is None:
            raise RunNotFoundError(
                code="RUN_NOT_FOUND",
                message="Run 不存在",
                status_code=404,
            )
        return self.existing

    async def find_by_response_message_id(
        self,
        *,
        session_id: UUID,
        response_message_id: UUID,
    ) -> Run | None:
        assert session_id == SESSION_ID
        assert response_message_id == SOURCE_MESSAGE_ID
        return None

    async def create_or_get(self, submission: RunSubmission) -> RunCreateResult:
        self.submissions.append(submission)
        run = Run(
            run_id=submission.run_id,
            request_id=submission.request_id,
            session_id=submission.session_id,
            thread_id=submission.thread_id,
            parent_run_id=submission.parent_run_id,
            run_type=submission.run_type,
            input_message_id=submission.input_message_id,
            response_message_id=submission.response_message_id,
            start_checkpoint_id=submission.start_checkpoint_id,
            input_payload=submission.input_payload,
            request_fingerprint=submission.request_fingerprint,
            status="queued",
            recovery_attempts=0,
            seq_high_watermark=0,
            error_code=None,
            error_message=None,
            created_at=submission.created_at,
            started_at=None,
            finished_at=None,
            updated_at=submission.created_at,
        )
        return RunCreateResult(run=run, created=True)


class _Coordinator:
    def __init__(self) -> None:
        self.wakes: list[UUID] = []

    async def wake(self, run_id: UUID) -> None:
        self.wakes.append(run_id)


class _AllowingValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID, str]] = []

    async def validate(
        self,
        *,
        user_id: str,
        session_id: UUID,
        capability_id: str,
    ) -> RegenerateSubmissionDecision:
        self.calls.append((user_id, session_id, capability_id))
        return RegenerateSubmissionDecision(
            capability_id=capability_id,
            task_action="continue",
            current_task_id=TASK_ID,
        )


class _RejectingValidator(_AllowingValidator):
    async def validate(self, **kwargs):
        await super().validate(**kwargs)
        raise CapabilityRegenerateUnsupportedError(
            code="CAPABILITY_REGENERATE_UNSUPPORTED",
            message="该能力不支持重新生成",
            status_code=409,
            retryable=False,
        )


def _service(*, repository, coordinator, forker, validator):
    from agent_runtime.chat import ChatService

    return ChatService(
        settings=SimpleNamespace(local_user_id="runtime-user"),
        session_repository=_SessionRepository(),
        session_service=object(),
        parent_graph=object(),
        history_adapter=_History(),
        checkpoint_forker=forker,
        persistent_run_repository=repository,
        run_coordinator=coordinator,
        regenerate_submission_validator=validator,
        response_message_id_factory=lambda: RESPONSE_MESSAGE_ID,
        run_id_factory=lambda: RUN_ID,
    )


def test_unsupported_capability_is_rejected_before_parent_fork_and_run_creation() -> None:
    """策略拒绝路径不得创建 Run，也不得写入 Parent Fork。"""

    repository = _Repository()
    coordinator = _Coordinator()
    forker = _CheckpointForker()
    validator = _RejectingValidator()
    service = _service(
        repository=repository,
        coordinator=coordinator,
        forker=forker,
        validator=validator,
    )

    with pytest.raises(CapabilityRegenerateUnsupportedError) as captured:
        asyncio.run(
            service.submit_regeneration_run(
                request_id=REQUEST_ID,
                session_id=SESSION_ID,
                message_id=SOURCE_MESSAGE_ID,
            )
        )

    assert captured.value.status_code == 409
    assert validator.calls == [("runtime-user", SESSION_ID, "general_chat")]
    assert forker.calls == 0
    assert repository.submissions == []
    assert coordinator.wakes == []


def test_remote_manifest_uses_real_policy_before_parent_fork_and_run_creation():
    from agent_runtime.capabilities.manifest import CapabilityManifest
    from agent_runtime.capabilities.regeneration import RegenerateSubmissionValidator
    from tests.capabilities.test_manifest import _valid_manifest
    from tests.fake_a2a import remote_transport
    raw = _valid_manifest()
    raw["transport"] = remote_transport()
    manifest = CapabilityManifest.model_validate(raw)
    registry = SimpleNamespace(get=lambda capability_id: SimpleNamespace(manifest=manifest))
    validator = RegenerateSubmissionValidator(registry=registry, permission_service=object(), task_context_repository=object(), task_repository=object())
    repository, coordinator, forker = _Repository(), _Coordinator(), _CheckpointForker()
    service = _service(repository=repository, coordinator=coordinator, forker=forker, validator=validator)
    with pytest.raises(CapabilityRegenerateUnsupportedError) as caught:
        asyncio.run(service.submit_regeneration_run(request_id=REQUEST_ID, session_id=SESSION_ID, message_id=SOURCE_MESSAGE_ID))
    assert caught.value.code == "CAPABILITY_REGENERATE_UNSUPPORTED"
    assert caught.value.status_code == 409
    assert forker.calls == 0
    assert repository.submissions == []
    assert coordinator.wakes == []


def test_allowed_submission_persists_validated_capability_without_router() -> None:
    """合法提交保存原回答能力与 continue 动作，不调用 Router。"""

    repository = _Repository()
    coordinator = _Coordinator()
    forker = _CheckpointForker()
    validator = _AllowingValidator()
    service = _service(
        repository=repository,
        coordinator=coordinator,
        forker=forker,
        validator=validator,
    )

    run = asyncio.run(
        service.submit_regeneration_run(
            request_id=REQUEST_ID,
            session_id=SESSION_ID,
            message_id=SOURCE_MESSAGE_ID,
        )
    )

    assert run.run_id == RUN_ID
    assert forker.calls == 1
    assert coordinator.wakes == [RUN_ID]
    assert len(repository.submissions) == 1
    assert repository.submissions[0].input_payload == {
        "source_message_id": str(SOURCE_MESSAGE_ID),
        "capability_id": "general_chat",
        "task_action": "continue",
    }


def test_idempotent_retry_returns_existing_run_before_dynamic_rechecks() -> None:
    """已提交 request_id 的重试复用原 Run，不受后来权限或健康变化影响。"""

    from agent_runtime.runtime.fingerprints import build_request_fingerprint

    payload = {"source_message_id": str(SOURCE_MESSAGE_ID)}
    existing = Run(
        run_id=RUN_ID,
        request_id=REQUEST_ID,
        session_id=SESSION_ID,
        thread_id=str(SESSION_ID),
        parent_run_id=None,
        run_type="regenerate",
        input_message_id=HUMAN_ID,
        response_message_id=RESPONSE_MESSAGE_ID,
        start_checkpoint_id="before-answer",
        input_payload={
            **payload,
            "capability_id": "general_chat",
            "task_action": "continue",
        },
        request_fingerprint=build_request_fingerprint(
            run_type="regenerate",
            session_id=SESSION_ID,
            request_payload=payload,
        ),
        status="queued",
        recovery_attempts=0,
        seq_high_watermark=0,
        error_code=None,
        error_message=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        updated_at=NOW,
    )
    repository = _Repository(existing=existing)
    coordinator = _Coordinator()
    forker = _CheckpointForker()
    validator = _RejectingValidator()
    service = _service(
        repository=repository,
        coordinator=coordinator,
        forker=forker,
        validator=validator,
    )

    repeated = asyncio.run(
        service.submit_regeneration_run(
            request_id=REQUEST_ID,
            session_id=SESSION_ID,
            message_id=SOURCE_MESSAGE_ID,
        )
    )

    assert repeated == existing
    assert validator.calls == []
    assert forker.calls == 0
    assert repository.submissions == []
    assert coordinator.wakes == [RUN_ID]
