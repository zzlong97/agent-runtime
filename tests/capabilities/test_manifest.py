"""Stage 3 Capability Manifest 严格 Schema 测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from agent_runtime.capabilities.manifest import (
    CapabilityManifest,
    ConcurrencyPolicy,
    ExecutionPolicy,
)


def _valid_manifest() -> dict[str, Any]:
    """返回包含全部必填字段的最小合法 Manifest。"""

    return {
        "manifest_schema_version": 1,
        "capability_id": "general_chat",
        "name": "通用对话",
        "description": "处理普通聊天和知识问答，不处理翻译任务。",
        "enabled": True,
        "entrypoint": (
            "agent_runtime.capabilities.general_chat.entrypoint:create_capability"
        ),
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
            "timeout_seconds": 120.0,
            "cancel_grace_seconds": 2.0,
        },
        "recovery_policy": "automatic",
        "side_effect_policy": "none",
    }


def test_valid_manifest_is_closed_and_immutable() -> None:
    manifest = CapabilityManifest.model_validate(_valid_manifest())

    assert manifest.capability_id == "general_chat"
    assert manifest.concurrency.mode == "unlimited"
    assert manifest.compatible_state_schema_versions == ()
    with pytest.raises(ValidationError):
        manifest.enabled = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "expected_location"),
    [
        (lambda data: data.pop("state_scope"), "state_scope"),
        (lambda data: data.update({"unknown": "value"}), "unknown"),
        (
            lambda data: data["concurrency"].update({"unknown": "value"}),
            "concurrency.unknown",
        ),
        (lambda data: data.update({"manifest_schema_version": 2}), "manifest_schema_version"),
        (lambda data: data.update({"capability_id": "General-Chat"}), "capability_id"),
        (lambda data: data.update({"capability_id": "a" * 65}), "capability_id"),
        (lambda data: data.update({"entrypoint": "../agent.py:create"}), "entrypoint"),
        (lambda data: data.update({"entrypoint": "module:factory()"}), "entrypoint"),
        (lambda data: data.update({"version": "1.0"}), "version"),
        (lambda data: data.update({"version": "01.0.0"}), "version"),
        (lambda data: data.update({"state_schema_version": ""}), "state_schema_version"),
        (
            lambda data: data.update({"state_schema_version": "a" * 65}),
            "state_schema_version",
        ),
        (
            lambda data: data.update(
                {"compatible_state_schema_versions": ["v1", "v1"]}
            ),
            "compatible_state_schema_versions",
        ),
    ],
    ids=[
        "缺少-state-scope",
        "顶层未知字段",
        "嵌套未知字段",
        "Manifest-版本非法",
        "能力-ID-格式非法",
        "能力-ID-过长",
        "entrypoint-文件路径",
        "entrypoint-表达式",
        "实现版本非-SemVer",
        "实现版本前导零",
        "State-Schema-版本为空",
        "State-Schema-版本过长",
        "兼容版本重复",
    ],
)
def test_manifest_rejects_invalid_fields(
    mutate: Any,
    expected_location: str,
) -> None:
    data = deepcopy(_valid_manifest())
    mutate(data)

    with pytest.raises(ValidationError) as caught:
        CapabilityManifest.model_validate(data)

    locations = {".".join(map(str, error["loc"])) for error in caught.value.errors()}
    assert expected_location in locations


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("manifest_schema_version",), "1"),
        (("manifest_schema_version",), True),
        (("manifest_schema_version",), 1.0),
        (("capability_id",), 1),
        (("enabled",), "true"),
        (("allow_degraded",), 0),
        (("concurrency", "max_concurrency"), "2"),
        (("concurrency", "acquire_timeout_seconds"), "1.0"),
        (("execution", "timeout_seconds"), "30.0"),
    ],
)
def test_manifest_does_not_coerce_control_field_types(
    field_path: tuple[str, ...],
    value: object,
) -> None:
    data = _valid_manifest()
    target: dict[str, Any] = data
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = value

    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(data)


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0-1a",
        "1.0.0-123abc",
        "1.0.0-alpha.1",
        "1.0.0-x-y-z.--",
        "1.0.0+build.20261003",
        "1.0.0-beta+exp.sha.5114f85",
    ],
)
def test_manifest_accepts_valid_semver_vectors(version: str) -> None:
    data = _valid_manifest()
    data["version"] = version

    assert CapabilityManifest.model_validate(data).version == version


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0-01",
        "1.0.0-alpha..1",
        "1.0.0-",
        "1.0.0+",
        "1.0.0+build..1",
    ],
)
def test_manifest_rejects_invalid_semver_vectors(version: str) -> None:
    data = _valid_manifest()
    data["version"] = version

    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(data)


@pytest.mark.parametrize(
    ("recovery_policy", "side_effect_policy", "is_valid"),
    [
        ("automatic", "none", True),
        ("automatic", "idempotent", True),
        ("automatic", "unsafe", False),
        ("manual", "none", True),
        ("manual", "idempotent", True),
        ("manual", "unsafe", True),
    ],
)
def test_recovery_and_side_effect_policy_matrix(
    recovery_policy: str,
    side_effect_policy: str,
    is_valid: bool,
) -> None:
    data = _valid_manifest()
    data["recovery_policy"] = recovery_policy
    data["side_effect_policy"] = side_effect_policy

    if is_valid:
        CapabilityManifest.model_validate(data)
    else:
        with pytest.raises(ValidationError, match="automatic.*unsafe"):
            CapabilityManifest.model_validate(data)


@pytest.mark.parametrize(
    ("mode", "max_concurrency", "is_valid"),
    [
        ("unlimited", None, True),
        ("unlimited", 1, False),
        ("bounded", 1, True),
        ("bounded", 8, True),
        ("bounded", None, False),
        ("bounded", 0, False),
        ("bounded", -1, False),
    ],
)
def test_concurrency_policy_matrix(
    mode: str,
    max_concurrency: int | None,
    is_valid: bool,
) -> None:
    data = _valid_manifest()
    data["concurrency"] = {
        "mode": mode,
        "max_concurrency": max_concurrency,
        "acquire_timeout_seconds": 1.0,
    }

    if is_valid:
        CapabilityManifest.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            CapabilityManifest.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("acquire_timeout_seconds", 0),
        ("acquire_timeout_seconds", -0.1),
        ("timeout_seconds", 0),
        ("timeout_seconds", -0.1),
        ("cancel_grace_seconds", -0.1),
    ],
)
def test_time_policy_boundaries(field: str, value: float) -> None:
    data = _valid_manifest()
    policy_name = "concurrency" if field == "acquire_timeout_seconds" else "execution"
    data[policy_name][field] = value

    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(data)


@pytest.mark.parametrize(
    ("policy_name", "field", "value"),
    [
        ("concurrency", "acquire_timeout_seconds", float("inf")),
        ("execution", "timeout_seconds", float("inf")),
        ("execution", "cancel_grace_seconds", float("inf")),
        ("execution", "cancel_grace_seconds", float("nan")),
    ],
)
def test_time_policy_rejects_non_finite_values(
    policy_name: str,
    field: str,
    value: float,
) -> None:
    data = _valid_manifest()
    data[policy_name][field] = value

    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(data)


def test_compatible_versions_must_be_a_yaml_array() -> None:
    data = _valid_manifest()
    data["compatible_state_schema_versions"] = {"v1", "v2"}

    with pytest.raises(ValidationError, match="YAML 数组"):
        CapabilityManifest.model_validate(data)


def test_every_schema_field_has_detailed_chinese_description() -> None:
    for model_type in (CapabilityManifest, ConcurrencyPolicy, ExecutionPolicy):
        for field_name, field in model_type.model_fields.items():
            description = field.description or ""
            assert len(description) >= 12, f"{model_type.__name__}.{field_name} 描述过短"
            assert any("\u4e00" <= char <= "\u9fff" for char in description), (
                f"{model_type.__name__}.{field_name} 缺少中文描述"
            )
