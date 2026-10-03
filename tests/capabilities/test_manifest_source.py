"""Stage 3 本地 Capability Manifest Source 测试。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.source import (
    CapabilityManifestDocument,
    CapabilitySource,
    LocalYamlCapabilitySource,
)


def _write_manifest(root: Path, capability_id: str, content: str) -> Path:
    directory = root / capability_id
    directory.mkdir(parents=True)
    path = directory / "manifest.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _valid_yaml(capability_id: str) -> str:
    return f"""\
manifest_schema_version: 1
capability_id: {capability_id}
name: 测试能力
description: 用于验证本地 Manifest Source 的测试能力。
enabled: true
entrypoint: agent_runtime.capabilities.{capability_id}.entrypoint:create_capability
version: 1.0.0
state_scope: invocation
state_schema_version: "1"
compatible_state_schema_versions: []
allow_degraded: false
concurrency:
  mode: unlimited
  max_concurrency: null
  acquire_timeout_seconds: 1.0
execution:
  timeout_seconds: 30.0
  cancel_grace_seconds: 1.0
recovery_policy: automatic
side_effect_policy: none
"""


def test_source_protocol_exposes_synchronous_load_contract() -> None:
    assert getattr(CapabilitySource, "load") is not None


def test_local_source_returns_all_documents_in_normalized_path_order(
    tmp_path: Path,
) -> None:
    z_path = _write_manifest(tmp_path, "z_capability", _valid_yaml("z_capability"))
    invalid_path = _write_manifest(
        tmp_path,
        "m_invalid",
        "capability_id: [unterminated\n",
    )
    a_path = _write_manifest(tmp_path, "a_capability", _valid_yaml("a_capability"))
    (tmp_path / "ignored.yaml").write_text("not: scanned", encoding="utf-8")

    documents = LocalYamlCapabilitySource(tmp_path).load()

    assert [document.source for document in documents] == [
        a_path.resolve(),
        invalid_path.resolve(),
        z_path.resolve(),
    ]
    assert documents[0].error is None
    assert documents[0].raw_document is not None
    assert documents[0].raw_document["capability_id"] == "a_capability"
    assert documents[1].raw_document is None
    assert documents[1].error is not None
    assert documents[1].error.code == "YAML_SYNTAX_ERROR"
    assert documents[2].error is None


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        ("", "YAML_ROOT_TYPE_ERROR"),
        ("- general_chat\n- en_to_zh\n", "YAML_ROOT_TYPE_ERROR"),
        ("---\na: 1\n---\nb: 2\n", "YAML_SYNTAX_ERROR"),
    ],
)
def test_source_records_yaml_errors_without_raising(
    tmp_path: Path,
    content: str,
    error_code: str,
) -> None:
    _write_manifest(tmp_path, "broken", content)

    [document] = LocalYamlCapabilitySource(tmp_path).load()

    assert document.raw_document is None
    assert document.error is not None
    assert document.error.code == error_code
    assert document.error.message


def test_recursive_yaml_alias_isolated_without_stopping_other_files(
    tmp_path: Path,
) -> None:
    recursive_path = _write_manifest(
        tmp_path,
        "a_recursive",
        "root: &root\n  child: *root\n",
    )
    valid_path = _write_manifest(tmp_path, "z_valid", _valid_yaml("z_valid"))

    documents = LocalYamlCapabilitySource(tmp_path).load()

    assert [document.source for document in documents] == [
        recursive_path.resolve(),
        valid_path.resolve(),
    ]
    assert documents[0].raw_document is None
    assert documents[0].error is not None
    assert documents[0].error.code == "YAML_STRUCTURE_ERROR"
    assert documents[1].error is None
    assert documents[1].raw_document is not None


def test_yaml_parser_recursion_error_does_not_stop_other_files(
    tmp_path: Path,
) -> None:
    too_deep_path = _write_manifest(
        tmp_path,
        "a_too_deep",
        "value: " + "[" * 500 + "0" + "]" * 500,
    )
    valid_path = _write_manifest(tmp_path, "z_valid", _valid_yaml("z_valid"))

    documents = LocalYamlCapabilitySource(tmp_path).load()

    assert [document.source for document in documents] == [
        too_deep_path.resolve(),
        valid_path.resolve(),
    ]
    assert documents[0].raw_document is None
    assert documents[0].error is not None
    assert documents[0].error.code == "YAML_STRUCTURE_ERROR"
    assert documents[1].error is None
    assert documents[1].raw_document is not None


def test_yaml_set_is_rejected_as_unsupported_structure(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "yaml_set",
        "compatible_state_schema_versions: !!set\n  v1: null\n",
    )

    [document] = LocalYamlCapabilitySource(tmp_path).load()

    assert document.raw_document is None
    assert document.error is not None
    assert document.error.code == "YAML_STRUCTURE_ERROR"


def test_source_document_is_deeply_read_only(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "immutable", _valid_yaml("immutable"))
    [document] = LocalYamlCapabilitySource(tmp_path).load()
    assert document.raw_document is not None

    with pytest.raises(TypeError):
        document.raw_document["enabled"] = False  # type: ignore[index]
    concurrency = document.raw_document["concurrency"]
    assert isinstance(concurrency, Mapping)
    with pytest.raises(TypeError):
        concurrency["mode"] = "bounded"  # type: ignore[index]


def test_missing_source_directory_is_an_empty_snapshot(tmp_path: Path) -> None:
    source = LocalYamlCapabilitySource(tmp_path / "not-created")

    assert source.load() == []


def test_repository_test_manifests_are_valid_and_not_loaded_implicitly() -> None:
    capability_root = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agent_runtime"
        / "capabilities"
    )
    source = LocalYamlCapabilitySource(capability_root)

    documents = source.load()

    assert [document.raw_document["capability_id"] for document in documents] == [
        "en_to_zh",
        "general_chat",
    ]
    manifests = [
        CapabilityManifest.model_validate(document.raw_document)
        for document in documents
    ]
    assert all(manifest.state_scope == "invocation" for manifest in manifests)
    assert all(manifest.side_effect_policy == "none" for manifest in manifests)


def test_document_record_has_only_source_payload_and_load_error() -> None:
    assert set(CapabilityManifestDocument.__dataclass_fields__) == {
        "source",
        "raw_document",
        "error",
    }
