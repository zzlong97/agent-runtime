"""Stage 3 Capability 统一协议与结果 Schema 测试。"""

from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    CapabilityBootstrapContext,
    CapabilityError,
    HealthResult,
)
from agent_runtime.capabilities.manifest import CapabilityManifest


def _manifest() -> CapabilityManifest:
    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "test_capability",
            "name": "测试能力",
            "description": "用于验证 Capability 统一协议。",
            "enabled": True,
            "entrypoint": "tests.fake_capability:create_capability",
            "version": "1.0.0",
            "state_scope": "invocation",
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
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        }
    )


def test_agent_result_accepts_only_closed_valid_combinations() -> None:
    completed = AgentResult(
        status="completed",
        content="最终结果",
        metadata=AgentResultMetadata(task_transition="completed"),
    )
    rejected = AgentResult(
        status="rejected",
        content="",
        metadata=AgentResultMetadata(control_signal="OUT_OF_SCOPE"),
    )

    assert completed.content == "最终结果"
    assert completed.metadata.control_signal is None
    assert rejected.metadata.task_transition == "keep_active"

    invalid_values = [
        {
            "status": "completed",
            "content": "   ",
            "metadata": {},
        },
        {
            "status": "completed",
            "content": "结果",
            "metadata": {"control_signal": "OUT_OF_SCOPE"},
        },
        {
            "status": "rejected",
            "content": "不应可见",
            "metadata": {"control_signal": "OUT_OF_SCOPE"},
        },
        {
            "status": "rejected",
            "content": "",
            "metadata": {"control_signal": None},
        },
        {
            "status": "rejected",
            "content": "",
            "metadata": {
                "control_signal": "OUT_OF_SCOPE",
                "task_transition": "failed",
            },
        },
    ]
    for value in invalid_values:
        with pytest.raises(ValidationError):
            AgentResult.model_validate(value)

    with pytest.raises(ValidationError):
        AgentResult.model_validate(
            {
                "status": "completed",
                "content": "结果",
                "metadata": {},
                "private_state": {},
            }
        )


def test_health_result_is_closed_and_has_chinese_field_descriptions() -> None:
    result = HealthResult(status="healthy", summary_code="READY")

    assert result.status == "healthy"
    assert result.summary_code == "READY"
    with pytest.raises(ValidationError):
        HealthResult.model_validate(
            {"status": "healthy", "summary_code": "READY", "details": {}}
        )

    for model in (AgentResultMetadata, AgentResult, HealthResult):
        for field in model.model_fields.values():
            assert field.description is not None
            assert any("\u4e00" <= char <= "\u9fff" for char in field.description)


def test_capability_error_validates_and_freezes_internal_details() -> None:
    source_details = {"dependency": {"name": "fake", "attempts": [1, 2]}}

    error = CapabilityError(
        code="DEPENDENCY_UNAVAILABLE",
        message="依赖暂时不可用",
        retryable=True,
        details=source_details,
    )
    source_details["dependency"]["attempts"].append(3)

    assert str(error) == "依赖暂时不可用"
    assert error.code == "DEPENDENCY_UNAVAILABLE"
    assert error.retryable is True
    assert error.details["dependency"]["attempts"] == (1, 2)
    assert isinstance(error.details, MappingProxyType)
    with pytest.raises(TypeError):
        error.details["new"] = "value"  # type: ignore[index]
    with pytest.raises(ValueError, match="code"):
        CapabilityError(
            code="bad-code",
            message="错误",
            retryable=False,
        )
    with pytest.raises(ValueError, match="details"):
        CapabilityError(
            code="BAD_DETAILS",
            message="错误",
            retryable=False,
            details={"value": object()},
        )


@pytest.mark.parametrize("invalid_details", [[], "", 0, False])
def test_capability_error_rejects_non_object_details_root(
    invalid_details: object,
) -> None:
    with pytest.raises(ValueError, match="details"):
        CapabilityError(
            code="INVALID_DETAILS_ROOT",
            message="详情根节点非法",
            retryable=False,
            details=invalid_details,  # type: ignore[arg-type]
        )


def test_capability_error_is_closed_and_immutable_after_validation() -> None:
    error = CapabilityError(
        code="STABLE_ERROR",
        message="稳定错误",
        retryable=False,
        details={"reason": "stable"},
    )

    for field_name, new_value in (
        ("code", "MUTATED"),
        ("message", "已修改"),
        ("retryable", True),
        ("details", {"leaked": True}),
        ("private_state", {"secret": "leaked"}),
    ):
        with pytest.raises(AttributeError):
            setattr(error, field_name, new_value)

    with pytest.raises(AttributeError):
        del error.code
    assert error.code == "STABLE_ERROR"
    assert error.message == "稳定错误"
    assert error.retryable is False
    assert error.details == {"reason": "stable"}
    assert not hasattr(error, "private_state")
    with pytest.raises(TypeError):
        error.__dict__["private_state"] = "leaked"
    with pytest.raises(CapabilityError) as captured:
        raise error
    assert captured.value is error


def test_capability_error_rejects_stateful_json_scalar_subclasses() -> None:
    class MutableInt(int):
        pass

    mutable_value = MutableInt(1)
    mutable_value.private_state = "不允许"  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="details"):
        CapabilityError(
            code="STATEFUL_DETAILS",
            message="详情包含非标准标量",
            retryable=False,
            details={"value": mutable_value},
        )


def test_bootstrap_context_exposes_only_controlled_readonly_dependencies() -> None:
    config = {"nested": {"values": ["a", "b"]}}
    registered = []

    context = CapabilityBootstrapContext(
        manifest=_manifest(),
        child_checkpointer=object(),
        model_factory=object(),
        capability_config=config,
        register_cleanup=registered.append,
    )
    config["nested"]["values"].append("c")

    assert context.capability_config["nested"]["values"] == ("a", "b")
    assert not hasattr(context, "redis")
    assert not hasattr(context, "session_repository")
    assert not hasattr(context, "run_repository")
    with pytest.raises(TypeError):
        context.capability_config["new"] = True  # type: ignore[index]


def test_bootstrap_context_rejects_unknown_mutable_values_and_non_string_keys() -> None:
    class MutableConfiguration:
        def __init__(self) -> None:
            self.value = "原值"

    with pytest.raises(TypeError, match="capability_config"):
        CapabilityBootstrapContext(
            manifest=_manifest(),
            child_checkpointer=object(),
            model_factory=object(),
            capability_config={"mutable": MutableConfiguration()},
            register_cleanup=lambda cleanup: None,
        )

    with pytest.raises(TypeError, match="字符串"):
        CapabilityBootstrapContext(
            manifest=_manifest(),
            child_checkpointer=object(),
            model_factory=object(),
            capability_config={1: "非法键"},  # type: ignore[dict-item]
            register_cleanup=lambda cleanup: None,
        )

    class MutableInt(int):
        pass

    mutable_scalar = MutableInt(1)
    mutable_scalar.runtime_state = "可变"  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="capability_config"):
        CapabilityBootstrapContext(
            manifest=_manifest(),
            child_checkpointer=object(),
            model_factory=object(),
            capability_config={"mutable_scalar": mutable_scalar},
            register_cleanup=lambda cleanup: None,
        )

    class MutableString(str):
        pass

    mutable_key = MutableString("key")
    mutable_key.runtime_state = "可变"  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="字符串"):
        CapabilityBootstrapContext(
            manifest=_manifest(),
            child_checkpointer=object(),
            model_factory=object(),
            capability_config={mutable_key: "value"},
            register_cleanup=lambda cleanup: None,
        )
