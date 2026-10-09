"""Stage 3 启动期静态 Capability Registry。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from agent_runtime.capabilities.contracts import (
    AsyncCleanup,
    Capability,
    CapabilityBootstrapContext,
    CapabilityFactory,
    HealthResult,
)
from agent_runtime.capabilities.manifest import (
    CapabilityManifest,
    ManifestCapabilityId,
    validate_capability_id,
)
from agent_runtime.capabilities.health import (
    CapabilityHealthService,
    HealthClock,
)
from agent_runtime.capabilities.source import (
    CapabilityManifestDocument,
    CapabilitySource,
)
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)

type CapabilityRegistryStatus = Literal[
    "active",
    "disabled",
    "invalid_manifest",
    "duplicate_id",
    "entrypoint_failed",
    "initialize_failed",
]
type ChildCheckpointerFactory = Callable[[CapabilityManifest], object]


class RouterProjection(BaseModel):
    """Registry 向 Router 暴露的唯一四字段能力视图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: ManifestCapabilityId = Field(
        description=(
            "符合 Manifest ID 规则的稳定 Capability 标识；Router 只能返回该标识，"
            "不得读取 entrypoint、运行策略或私有 State。"
        ),
    )
    name: StrictStr = Field(
        description=(
            "来自已校验 Manifest 的人类可读能力名称，仅用于 Router 理解候选能力。"
        ),
    )
    description: StrictStr = Field(
        description=(
            "来自已校验 Manifest 的公开能力边界说明，不包含 Prompt、工具详情、"
            "Graph 拓扑或健康诊断。"
        ),
    )
    enabled: StrictBool = Field(
        description=(
            "Manifest 的静态启用标记；S3 Registry Snapshot 不支持运行期修改。"
        ),
    )


@dataclass(frozen=True, slots=True)
class CapabilityRegistryEntry:
    """记录一个 Manifest 来源的不可变静态加载结果。"""

    source: Path
    status: CapabilityRegistryStatus
    manifest: CapabilityManifest | None
    capability: Capability | None
    health: HealthResult | None
    diagnostic_code: str | None

    @property
    def capability_id(self) -> str | None:
        """返回已通过 Manifest 校验的 ID，非法来源返回空。"""

        return None if self.manifest is None else self.manifest.capability_id


@dataclass(frozen=True, slots=True)
class _ValidatedDocument:
    """保存来源及其独立 Manifest 校验结果。"""

    document: CapabilityManifestDocument
    manifest: CapabilityManifest | None
    declared_capability_id: str | None
    diagnostic_code: str | None


@dataclass(slots=True)
class _CleanupRegistration:
    """保存一个清理函数及其仅成功消费一次的状态。"""

    callback: AsyncCleanup
    completed: bool = False


class _CleanupGroup:
    """管理单个 Capability 注册的幂等异步清理函数。"""

    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id
        self._callbacks: list[_CleanupRegistration] = []
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        """返回组内全部清理是否已经完成或被受控消费。"""

        return self._closed

    def register(self, cleanup: AsyncCleanup) -> None:
        """按注册顺序保存异步清理函数，关闭后拒绝新增资源。"""

        if self._closed:
            raise RuntimeError("Capability 清理组已关闭，不能继续注册资源")
        if not callable(cleanup):
            raise TypeError("Capability cleanup 必须是可调用对象")
        self._callbacks.append(_CleanupRegistration(callback=cleanup))

    async def close(self) -> None:
        """仅执行一次逆序清理；单个失败不阻止剩余资源释放。"""

        async with self._close_lock:
            if self._closed:
                return
            pending_cancellation: asyncio.CancelledError | None = None
            for registration in reversed(self._callbacks):
                if registration.completed:
                    continue
                try:
                    result = registration.callback()
                    if not inspect.isawaitable(result):
                        raise TypeError("Capability cleanup 必须返回 Awaitable")
                    await result
                except asyncio.CancelledError as error:
                    if pending_cancellation is None:
                        pending_cancellation = error
                    continue
                except Exception as error:
                    log_business_event(
                        logger,
                        "Capability资源清理失败",
                        level=logging.ERROR,
                        capability_id=self.capability_id,
                        error_code="CAPABILITY_CLEANUP_FAILED",
                        error_type=type(error).__name__,
                    )
                registration.completed = True
            self._closed = all(
                registration.completed for registration in self._callbacks
            )
            if pending_cancellation is not None:
                raise pending_cancellation


class CapabilityRegistry:
    """从全部 Manifest 来源一次性构建且不可热变更的 Registry Snapshot。"""

    def __init__(
        self,
        entries: tuple[CapabilityRegistryEntry, ...],
        cleanup_groups: tuple[_CleanupGroup, ...],
        health_service: CapabilityHealthService,
    ) -> None:
        self._entries = entries
        self._active_entries = MappingProxyType(
            {
                entry.manifest.capability_id: entry
                for entry in entries
                if entry.status == "active" and entry.manifest is not None
            }
        )
        self._cleanup_groups = cleanup_groups
        self._health_service = health_service
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def entries(self) -> tuple[CapabilityRegistryEntry, ...]:
        """返回按规范化来源路径稳定排序的完整静态加载记录。"""

        return self._entries

    @classmethod
    async def build(
        cls,
        source: CapabilitySource,
        *,
        child_checkpointer_factory: ChildCheckpointerFactory,
        model_factory: object,
        capability_configs: Mapping[str, Mapping[str, object]] | None = None,
        health_ttl_seconds: float = 30.0,
        health_clock: HealthClock | None = None,
    ) -> CapabilityRegistry:
        """收集全部来源、隔离冲突并顺序初始化可启用 Capability。"""

        log_business_event(logger, "Capability Registry构建开始")
        documents = sorted(source.load(), key=_document_sort_key)
        validated_documents = tuple(
            _validate_document(document) for document in documents
        )
        duplicate_counts = Counter(
            item.declared_capability_id
            for item in validated_documents
            if item.declared_capability_id is not None
        )
        configurations = capability_configs or {}
        entries: list[CapabilityRegistryEntry] = []
        cleanup_groups: list[_CleanupGroup] = []

        try:
            for item in validated_documents:
                manifest = item.manifest
                declared_capability_id = item.declared_capability_id
                if (
                    declared_capability_id is not None
                    and duplicate_counts[declared_capability_id] > 1
                ):
                    entries.append(
                        _entry_without_instance(
                            item.document.source,
                            status="duplicate_id",
                            manifest=manifest,
                            diagnostic_code="DUPLICATE_CAPABILITY_ID",
                        )
                    )
                    _log_isolation(
                        item.document.source,
                        capability_id=declared_capability_id,
                        status="duplicate_id",
                        diagnostic_code="DUPLICATE_CAPABILITY_ID",
                    )
                    continue
                if manifest is None:
                    entries.append(
                        _entry_without_instance(
                            item.document.source,
                            status="invalid_manifest",
                            diagnostic_code=item.diagnostic_code,
                        )
                    )
                    _log_isolation(
                        item.document.source,
                        capability_id=None,
                        status="invalid_manifest",
                        diagnostic_code=item.diagnostic_code,
                    )
                    continue
                if not manifest.enabled:
                    entries.append(
                        _entry_without_instance(
                            item.document.source,
                            status="disabled",
                            manifest=manifest,
                            diagnostic_code="CAPABILITY_DISABLED",
                        )
                    )
                    log_business_event(
                        logger,
                        "Capability静态禁用",
                        capability_id=manifest.capability_id,
                        source_path=str(item.document.source),
                        status="disabled",
                    )
                    continue

                cleanup_group = _CleanupGroup(manifest.capability_id)
                cleanup_groups.append(cleanup_group)
                entry, retained_cleanup_group = await _load_entry(
                    item.document.source,
                    manifest,
                    cleanup_group=cleanup_group,
                    child_checkpointer_factory=child_checkpointer_factory,
                    model_factory=model_factory,
                    capability_config=configurations.get(
                        manifest.capability_id,
                        {},
                    ),
                )
                entries.append(
                    entry
                )
                if retained_cleanup_group is None:
                    removed_group = cleanup_groups.pop()
                    assert removed_group is cleanup_group

            health_service = await CapabilityHealthService.start(
                capabilities={
                    entry.manifest.capability_id: (
                        entry.manifest.allow_degraded,
                        entry.capability.health_check,
                    )
                    for entry in entries
                    if entry.status == "active"
                    and entry.manifest is not None
                    and entry.capability is not None
                },
                ttl_seconds=health_ttl_seconds,
                clock=health_clock,
            )
            entries = [
                _attach_startup_health(entry, health_service)
                for entry in entries
            ]
        except BaseException:
            await _close_cleanup_groups(cleanup_groups)
            raise

        registry = cls(
            tuple(entries),
            tuple(cleanup_groups),
            health_service,
        )
        log_business_event(
            logger,
            "Capability Registry构建完成",
            source_count=len(entries),
            active_count=sum(entry.status == "active" for entry in entries),
            isolated_count=sum(
                entry.status not in {"active", "disabled"} for entry in entries
            ),
        )
        return registry

    def get(self, capability_id: str) -> CapabilityRegistryEntry | None:
        """按稳定 ID 查询静态 active Entry，不返回隔离或禁用来源。"""

        return self._active_entries.get(capability_id)

    def router_projections(self) -> tuple[RouterProjection, ...]:
        """返回基于启动快照的兼容视图；动态调用应使用异步准入接口。"""

        projections: list[RouterProjection] = []
        for entry in self._entries:
            if not _is_serviceable(entry):
                continue
            assert entry.manifest is not None
            projections.append(
                RouterProjection(
                    capability_id=entry.manifest.capability_id,
                    name=entry.manifest.name,
                    description=entry.manifest.description,
                    enabled=entry.manifest.enabled,
                )
            )
        return tuple(projections)

    async def serviceable_router_projections(self) -> tuple[RouterProjection, ...]:
        """按需刷新过期健康快照，并仅返回当前可服务的四字段视图。"""

        serviceable_ids = set(
            await self._health_service.serviceable_capability_ids()
        )
        return tuple(
            _router_projection(entry)
            for entry in self._entries
            if entry.status == "active"
            and entry.manifest is not None
            and entry.manifest.capability_id in serviceable_ids
        )

    def active_router_projections(self) -> tuple[RouterProjection, ...]:
        """返回忽略健康结果的 active 最小视图，用于区分权限与服务不可用。"""

        return tuple(
            _router_projection(entry)
            for entry in self._entries
            if entry.status == "active" and entry.manifest is not None
        )

    async def close(self) -> None:
        """仅执行一次 Registry 关机，并按 Capability 初始化逆序清理资源。"""

        async with self._close_lock:
            if self._closed:
                return
            log_business_event(
                logger,
                "Capability Registry关闭开始",
                active_count=len(self._cleanup_groups),
            )
            pending_cancellation = await _close_cleanup_groups(
                self._cleanup_groups
            )
            self._closed = all(
                cleanup_group.closed
                for cleanup_group in self._cleanup_groups
            )
            if pending_cancellation is not None:
                log_business_event(
                    logger,
                    "Capability Registry关闭被取消",
                    level=logging.WARNING,
                    active_count=len(self._cleanup_groups),
                    status="cancelled",
                )
                raise pending_cancellation
            log_business_event(
                logger,
                "Capability Registry关闭完成",
                active_count=len(self._cleanup_groups),
            )


def _document_sort_key(document: CapabilityManifestDocument) -> tuple[str, str]:
    """以跨平台规范化来源文本生成确定性排序键。"""

    normalized = document.source.as_posix()
    return normalized.casefold(), normalized


def _validate_document(document: CapabilityManifestDocument) -> _ValidatedDocument:
    """把单个来源错误或严格 Schema 错误转换为隔离记录。"""

    declared_capability_id = _extract_declared_capability_id(
        document.raw_document
    )
    if document.error is not None or document.raw_document is None:
        return _ValidatedDocument(
            document=document,
            manifest=None,
            declared_capability_id=declared_capability_id,
            diagnostic_code=(
                "MANIFEST_SOURCE_ERROR"
                if document.error is None
                else document.error.code
            ),
        )
    try:
        manifest = CapabilityManifest.model_validate(document.raw_document)
    except (ValidationError, TypeError, ValueError, RecursionError):
        return _ValidatedDocument(
            document=document,
            manifest=None,
            declared_capability_id=declared_capability_id,
            diagnostic_code="MANIFEST_VALIDATION_FAILED",
        )
    return _ValidatedDocument(
        document=document,
        manifest=manifest,
        declared_capability_id=manifest.capability_id,
        diagnostic_code=None,
    )


def _extract_declared_capability_id(
    raw_document: Mapping[object, object] | None,
) -> str | None:
    """按正式 Manifest 的字符串去空白语义提取 ID 并提前收集冲突。"""

    if raw_document is None:
        return None
    try:
        return validate_capability_id(raw_document.get("capability_id"))
    except (ValidationError, TypeError, ValueError):
        return None


async def _load_entry(
    source: Path,
    manifest: CapabilityManifest,
    *,
    cleanup_group: _CleanupGroup,
    child_checkpointer_factory: ChildCheckpointerFactory,
    model_factory: object,
    capability_config: Mapping[str, object],
) -> tuple[CapabilityRegistryEntry, _CleanupGroup | None]:
    """导入、实例化并初始化一个 Capability，失败仅隔离当前来源。"""

    try:
        factory = _load_factory(manifest.entrypoint)
        child_checkpointer = child_checkpointer_factory(manifest)
        bootstrap_context = CapabilityBootstrapContext(
            manifest=manifest,
            child_checkpointer=child_checkpointer,
            model_factory=model_factory,
            capability_config=capability_config,
            register_cleanup=cleanup_group.register,
        )
        capability = factory(bootstrap_context)
        if inspect.isawaitable(capability):
            close = getattr(capability, "close", None)
            if callable(close):
                close()
            raise TypeError("Capability 工厂必须同步返回实例")
        if not _implements_capability_protocol(capability):
            raise TypeError("工厂返回对象未实现 Capability 异步协议")
    except asyncio.CancelledError:
        await cleanup_group.close()
        raise
    except Exception as error:
        await cleanup_group.close()
        _log_isolation(
            source,
            capability_id=manifest.capability_id,
            status="entrypoint_failed",
            diagnostic_code="CAPABILITY_ENTRYPOINT_FAILED",
            error=error,
        )
        return (
            _entry_without_instance(
                source,
                status="entrypoint_failed",
                manifest=manifest,
                diagnostic_code="CAPABILITY_ENTRYPOINT_FAILED",
            ),
            None,
        )

    try:
        initialize_result = await capability.initialize()
        if initialize_result is not None:
            raise TypeError("Capability initialize 必须返回 None")
    except asyncio.CancelledError:
        await cleanup_group.close()
        raise
    except Exception as error:
        await cleanup_group.close()
        _log_isolation(
            source,
            capability_id=manifest.capability_id,
            status="initialize_failed",
            diagnostic_code="CAPABILITY_INITIALIZE_FAILED",
            error=error,
        )
        return (
            _entry_without_instance(
                source,
                status="initialize_failed",
                manifest=manifest,
                diagnostic_code="CAPABILITY_INITIALIZE_FAILED",
            ),
            None,
        )

    log_business_event(
        logger,
        "Capability初始化完成",
        capability_id=manifest.capability_id,
        source_path=str(source),
        status="active",
    )
    return (
        CapabilityRegistryEntry(
            source=source,
            status="active",
            manifest=manifest,
            capability=capability,
            health=None,
            diagnostic_code=None,
        ),
        cleanup_group,
    )


def _load_factory(entrypoint: str) -> CapabilityFactory:
    """动态导入并校验统一同步工厂的精确单参数签名。"""

    module_name, separator, attribute_name = entrypoint.partition(":")
    if not separator or attribute_name != "create_capability":
        raise TypeError("Capability entrypoint 必须指向 create_capability")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise TypeError("Capability entrypoint 必须是同步可调用工厂")
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError) as error:
        raise TypeError("Capability 工厂签名不可检查") from error
    parameters = tuple(signature.parameters.values())
    if (
        len(parameters) != 1
        or parameters[0].name != "bootstrap_context"
        or parameters[0].kind
        not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
        or parameters[0].default is not inspect.Parameter.empty
    ):
        raise TypeError("Capability 工厂必须只接收 bootstrap_context")
    return factory


def _implements_capability_protocol(value: object) -> bool:
    """校验三个必需方法的异步属性及精确 bound method 签名。"""

    if not isinstance(value, Capability):
        return False
    return (
        _has_exact_async_signature(value.initialize, ())
        and _has_exact_async_signature(value.health_check, ())
        and _has_exact_async_signature(
            value.invoke,
            ("run_context", "agent_context", "task_input"),
        )
    )


def _has_exact_async_signature(
    method: object,
    parameter_names: tuple[str, ...],
) -> bool:
    """确认 bound method 是异步函数且只含指定的必填位置参数。"""

    if not inspect.iscoroutinefunction(method):
        return False
    try:
        parameters = tuple(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(parameters) == len(parameter_names) and all(
        parameter.name == expected_name
        and parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter, expected_name in zip(parameters, parameter_names, strict=True)
    )


async def _close_cleanup_groups(
    cleanup_groups: list[_CleanupGroup] | tuple[_CleanupGroup, ...],
) -> asyncio.CancelledError | None:
    """逆序尝试全部清理组，延后传播首个取消以避免资源泄漏。"""

    pending_cancellation: asyncio.CancelledError | None = None
    for cleanup_group in reversed(cleanup_groups):
        try:
            await cleanup_group.close()
        except asyncio.CancelledError as error:
            if pending_cancellation is None:
                pending_cancellation = error
    return pending_cancellation


def _attach_startup_health(
    entry: CapabilityRegistryEntry,
    health_service: CapabilityHealthService,
) -> CapabilityRegistryEntry:
    """把启动强检结果保存为静态诊断，不随动态 TTL 刷新修改 Entry。"""

    if entry.status != "active" or entry.manifest is None:
        return entry
    snapshot = health_service.startup_snapshot(entry.manifest.capability_id)
    return replace(
        entry,
        health=HealthResult(
            status=snapshot.status,
            summary_code=snapshot.summary_code,
        ),
        diagnostic_code=(
            "CAPABILITY_HEALTH_CHECK_FAILED"
            if snapshot.summary_code == "HEALTH_CHECK_FAILED"
            else None
        ),
    )


def _entry_without_instance(
    source: Path,
    *,
    status: CapabilityRegistryStatus,
    manifest: CapabilityManifest | None = None,
    diagnostic_code: str | None,
) -> CapabilityRegistryEntry:
    """构造未进入服务候选的静态来源记录。"""

    return CapabilityRegistryEntry(
        source=source,
        status=status,
        manifest=manifest,
        capability=None,
        health=None,
        diagnostic_code=diagnostic_code,
    )


def _is_serviceable(entry: CapabilityRegistryEntry) -> bool:
    """根据静态状态、启动健康结果和 allow_degraded 计算当前候选。"""

    if (
        entry.status != "active"
        or entry.manifest is None
        or entry.health is None
    ):
        return False
    if entry.health.status == "healthy":
        return True
    return (
        entry.health.status == "degraded" and entry.manifest.allow_degraded
    )


def _router_projection(entry: CapabilityRegistryEntry) -> RouterProjection:
    """从已验证的 active Entry 构造不含运行细节的 Router 最小投影。"""

    assert entry.manifest is not None
    return RouterProjection(
        capability_id=entry.manifest.capability_id,
        name=entry.manifest.name,
        description=entry.manifest.description,
        enabled=entry.manifest.enabled,
    )


def _log_isolation(
    source: Path,
    *,
    capability_id: str | None,
    status: CapabilityRegistryStatus,
    diagnostic_code: str | None,
    error: Exception | None = None,
) -> None:
    """记录不含 Manifest 正文、异常内容或堆栈的隔离事件。"""

    log_business_event(
        logger,
        "Capability来源隔离",
        level=logging.WARNING,
        source_path=str(source),
        capability_id=capability_id,
        status=status,
        error_code=diagnostic_code,
        error_type=None if error is None else type(error).__name__,
    )
