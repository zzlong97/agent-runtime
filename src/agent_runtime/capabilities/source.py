"""Stage 3 Capability Manifest 的本地只读来源。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

import yaml

from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)

type ManifestLoadErrorCode = Literal[
    "YAML_SYNTAX_ERROR",
    "YAML_ROOT_TYPE_ERROR",
    "YAML_STRUCTURE_ERROR",
    "MANIFEST_READ_ERROR",
]

_MAX_YAML_NESTING_DEPTH = 128


class _UnsupportedYamlStructureError(ValueError):
    """表示 YAML 虽可解析，但对象结构不适合作为确定性 Manifest。"""


@dataclass(frozen=True, slots=True)
class ManifestDocumentError:
    """记录单个 Manifest 文件的受控加载错误，不携带文件正文。"""

    code: ManifestLoadErrorCode
    message: str
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class CapabilityManifestDocument:
    """保存 Manifest 来源、深度只读原始映射或对应加载错误。"""

    source: Path
    raw_document: Mapping[Any, Any] | None
    error: ManifestDocumentError | None


class CapabilitySource(Protocol):
    """Manifest 来源只负责收集原始文档，不导入或实例化 Capability。"""

    def load(self) -> list[CapabilityManifestDocument]:
        """返回全部来源记录；单文件错误不得中断其他文件收集。"""

        ...


class LocalYamlCapabilitySource:
    """从一个受控根目录确定性读取所有 ``manifest.yaml`` 文件。"""

    def __init__(self, root_directory: str | Path) -> None:
        self._root_directory = Path(root_directory).resolve()

    def load(self) -> list[CapabilityManifestDocument]:
        """按规范化相对路径排序并收集全部本地 YAML 文档。"""

        log_business_event(
            logger,
            "Capability Manifest 本地扫描开始",
            source_directory=str(self._root_directory),
        )
        if not self._root_directory.exists():
            log_business_event(
                logger,
                "Capability Manifest 本地扫描完成",
                source_directory=str(self._root_directory),
                document_count=0,
                error_count=0,
            )
            return []
        if not self._root_directory.is_dir():
            log_business_event(
                logger,
                "Capability Manifest 本地扫描拒绝",
                level=logging.ERROR,
                source_directory=str(self._root_directory),
                error_code="MANIFEST_SOURCE_NOT_DIRECTORY",
            )
            raise NotADirectoryError("Capability Manifest 来源路径不是目录")

        paths = sorted(
            (
                path.resolve()
                for path in self._root_directory.rglob("manifest.yaml")
                if path.is_file()
            ),
            key=self._normalized_relative_path,
        )
        documents = [self._load_document(path) for path in paths]
        error_count = sum(document.error is not None for document in documents)
        log_business_event(
            logger,
            "Capability Manifest 本地扫描完成",
            source_directory=str(self._root_directory),
            document_count=len(documents),
            error_count=error_count,
        )
        return documents

    def _normalized_relative_path(self, path: Path) -> tuple[str, str]:
        """生成与平台大小写规则无关的稳定排序键。"""

        relative = path.relative_to(self._root_directory).as_posix()
        return relative.casefold(), relative

    def _load_document(self, path: Path) -> CapabilityManifestDocument:
        """读取一个 YAML 文件，并把失败转换为来源记录。"""

        try:
            text = path.read_text(encoding="utf-8")
            raw_document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            return self._error_document(
                path,
                ManifestDocumentError(
                    code="YAML_SYNTAX_ERROR",
                    message="Manifest YAML 语法无效",
                    line=None if mark is None else mark.line + 1,
                    column=None if mark is None else mark.column + 1,
                ),
            )
        except RecursionError:
            return self._error_document(
                path,
                ManifestDocumentError(
                    code="YAML_STRUCTURE_ERROR",
                    message="Manifest YAML 嵌套过深，解析已安全终止",
                ),
            )
        except (OSError, UnicodeError):
            return self._error_document(
                path,
                ManifestDocumentError(
                    code="MANIFEST_READ_ERROR",
                    message="Manifest 文件无法按 UTF-8 读取",
                ),
            )

        if not isinstance(raw_document, Mapping):
            return self._error_document(
                path,
                ManifestDocumentError(
                    code="YAML_ROOT_TYPE_ERROR",
                    message="Manifest YAML 根节点必须是映射对象",
                ),
            )
        try:
            frozen_document = _deep_freeze_mapping(raw_document)
        except (_UnsupportedYamlStructureError, RecursionError):
            return self._error_document(
                path,
                ManifestDocumentError(
                    code="YAML_STRUCTURE_ERROR",
                    message="Manifest YAML 包含循环、集合或过深嵌套等不支持结构",
                ),
            )
        return CapabilityManifestDocument(
            source=path,
            raw_document=frozen_document,
            error=None,
        )

    def _error_document(
        self,
        path: Path,
        error: ManifestDocumentError,
    ) -> CapabilityManifestDocument:
        """记录不含文件正文的中文失败日志并返回错误来源记录。"""

        log_business_event(
            logger,
            "Capability Manifest 文件加载失败",
            level=logging.WARNING,
            source_path=str(path),
            error_code=error.code,
            error_line=error.line,
            error_column=error.column,
        )
        return CapabilityManifestDocument(
            source=path,
            raw_document=None,
            error=error,
        )


def _deep_freeze_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """把 YAML 映射及其嵌套容器转换为深度只读视图。"""

    frozen = _deep_freeze_value(
        value,
        active_container_ids=set(),
        depth=0,
    )
    if not isinstance(frozen, Mapping):
        raise _UnsupportedYamlStructureError("Manifest YAML 根节点必须是映射对象")
    return frozen


def _deep_freeze_value(
    value: Any,
    *,
    active_container_ids: set[int],
    depth: int,
) -> Any:
    """递归冻结 YAML 产生的映射和列表，标量保持原值。"""

    if depth > _MAX_YAML_NESTING_DEPTH:
        raise _UnsupportedYamlStructureError("Manifest YAML 嵌套层级过深")
    if isinstance(value, (set, frozenset)):
        raise _UnsupportedYamlStructureError("Manifest YAML 不允许集合类型")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_container_ids:
            raise _UnsupportedYamlStructureError("Manifest YAML 不允许循环别名")
        active_container_ids.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _deep_freeze_value(
                        nested_value,
                        active_container_ids=active_container_ids,
                        depth=depth + 1,
                    )
                    for key, nested_value in value.items()
                }
            )
        finally:
            active_container_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_container_ids:
            raise _UnsupportedYamlStructureError("Manifest YAML 不允许循环别名")
        active_container_ids.add(identity)
        try:
            return tuple(
                _deep_freeze_value(
                    item,
                    active_container_ids=active_container_ids,
                    depth=depth + 1,
                )
                for item in value
            )
        finally:
            active_container_ids.remove(identity)
    return value
