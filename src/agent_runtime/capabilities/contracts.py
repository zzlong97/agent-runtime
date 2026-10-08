"""Stage 3 Capability 的统一协议、受控 Bootstrap 与结果契约。"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.runtime.agent_contract import AgentContext, RunContext, TaskInput

type AsyncCleanup = Callable[[], Awaitable[None]]
type CleanupRegistrar = Callable[[AsyncCleanup], None]

_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SUMMARY_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_INTERNAL_DETAILS_DEPTH = 64


class _ClosedContractModel(BaseModel):
    """为 Stage 3 公共契约提供关闭、只读且严格的模型基线。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentResultMetadata(_ClosedContractModel):
    """承载 Runtime 可解释的关闭控制字段，不允许透传业务私有状态。"""

    control_signal: Literal["OUT_OF_SCOPE"] | None = Field(
        default=None,
        description=(
            "Capability 的标准控制信号；Stage 3 只允许 OUT_OF_SCOPE 或 null，"
            "不得承载业务 State、任意 JSON 或用户可见内容。"
        ),
    )
    task_transition: Literal[
        "keep_active",
        "completed",
        "failed",
        "cancelled",
    ] = Field(
        default="keep_active",
        description=(
            "Capability 请求 Runtime 应用的 Task 生命周期意图；默认 keep_active，"
            "其他值仅表达请求，Capability 不得直接修改 Task 持久化状态。"
        ),
    )


class AgentResult(_ClosedContractModel):
    """Capability 成功调用或合法范围拒绝时返回的标准关闭结果。"""

    status: Literal["completed", "rejected"] = Field(
        description=(
            "调用结果类型；completed 表示产生规范最终全文，rejected 仅表示合法的"
            "能力范围拒绝，执行失败必须抛出 CapabilityError。"
        ),
    )
    content: StrictStr = Field(
        description=(
            "本次成功调用的最终规范全文；completed 去除空白后必须非空，rejected"
            "必须为空字符串，Runtime 后续据此构造唯一最终公共 AIMessage。"
        ),
    )
    metadata: AgentResultMetadata = Field(
        default_factory=AgentResultMetadata,
        description=(
            "Runtime 可解释的关闭控制元数据，只包含 control_signal 与"
            "task_transition，不得包含 Capability 私有 State。"
        ),
    )

    @model_validator(mode="after")
    def validate_status_combination(self) -> AgentResult:
        """拒绝状态、正文和控制字段之间不合法的组合。"""

        if self.status == "completed":
            if not self.content.strip():
                raise ValueError("completed AgentResult 的 content 必须非空")
            if self.metadata.control_signal is not None:
                raise ValueError("completed AgentResult 不允许 control_signal")
            return self

        if self.content != "":
            raise ValueError("rejected AgentResult 的 content 必须为空字符串")
        if self.metadata.control_signal != "OUT_OF_SCOPE":
            raise ValueError("rejected AgentResult 必须携带 OUT_OF_SCOPE")
        if self.metadata.task_transition != "keep_active":
            raise ValueError("rejected AgentResult 不得请求 Task 终态转换")
        return self


class HealthResult(_ClosedContractModel):
    """Capability 自治健康检查向 Runtime 返回的标准结果。"""

    status: Literal["healthy", "degraded", "unhealthy"] = Field(
        description=(
            "Capability 当前健康状态，只允许 healthy、degraded 或 unhealthy；"
            "是否可服务由 Runtime 结合 Manifest 的 allow_degraded 决定。"
        ),
    )
    summary_code: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=_SUMMARY_CODE_PATTERN,
        description=(
            "不含凭据和业务正文的稳定内部健康摘要码，使用大写字母、数字和下划线，"
            "只供 Runtime 诊断与中文业务日志使用，不直接进入公开 SSE。"
        ),
    )


class CapabilityError(Exception):
    """表示 Capability 可识别失败，并保存只读的内部受控 JSON 详情。"""

    __slots__ = ("_code", "_message", "_retryable", "_details")

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if type(code) is not str or not _ERROR_CODE_PATTERN.fullmatch(code):
            raise ValueError("CapabilityError code 必须是稳定的大写下划线标识")
        if type(message) is not str or not message.strip():
            raise ValueError("CapabilityError message 必须是非空字符串")
        if type(retryable) is not bool:
            raise ValueError("CapabilityError retryable 必须是布尔值")
        if details is None:
            details_root: Mapping[str, object] = {}
        elif isinstance(details, Mapping):
            details_root = details
        else:
            raise ValueError("CapabilityError details 根节点必须是 JSON 对象")
        try:
            frozen_details = _freeze_json_mapping(details_root)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("CapabilityError details 必须是受控 JSON 对象") from error

        super().__init__(message)
        object.__setattr__(self, "_code", code)
        object.__setattr__(self, "_message", message)
        object.__setattr__(self, "_retryable", retryable)
        object.__setattr__(self, "_details", frozen_details)

    @property
    def code(self) -> str:
        """返回构造时已校验且不可修改的稳定错误码。"""

        return self._code

    @property
    def message(self) -> str:
        """返回构造时已校验且不可修改的内部错误消息。"""

        return self._message

    @property
    def retryable(self) -> bool:
        """返回 Runtime 是否可按白名单把该错误标记为可重试。"""

        return self._retryable

    @property
    def details(self) -> Mapping[str, object]:
        """返回深度只读且仅含标准 JSON 值的内部详情对象。"""

        return self._details

    def __setattr__(self, name: str, value: object) -> None:
        """拒绝修改既有字段或添加任意私有状态。"""

        raise AttributeError("CapabilityError 是关闭且不可变的错误契约")

    def __delattr__(self, name: str) -> None:
        """拒绝删除已校验字段。"""

        raise AttributeError("CapabilityError 是关闭且不可变的错误契约")

    def __getattribute__(self, name: str) -> object:
        """把 Exception 基类保留的实例字典收敛为只读视图。"""

        if name == "__dict__":
            instance_dict = object.__getattribute__(self, "__dict__")
            return MappingProxyType(instance_dict)
        return object.__getattribute__(self, name)


@dataclass(frozen=True, slots=True)
class CapabilityBootstrapContext:
    """只向 Capability 工厂暴露经架构允许的五项基础设施能力。"""

    manifest: CapabilityManifest
    child_checkpointer: object
    model_factory: object
    capability_config: Mapping[str, object]
    register_cleanup: CleanupRegistrar

    def __post_init__(self) -> None:
        """复制并冻结非敏感配置，避免 Capability 修改 Runtime 配置。"""

        if not isinstance(self.manifest, CapabilityManifest):
            raise TypeError("Bootstrap manifest 必须是已校验 CapabilityManifest")
        if not callable(self.register_cleanup):
            raise TypeError("Bootstrap register_cleanup 必须可调用")
        object.__setattr__(
            self,
            "capability_config",
            _freeze_configuration_mapping(self.capability_config),
        )


@runtime_checkable
class Capability(Protocol):
    """所有 Stage 3 Capability 实例必须实现的异步生命周期与调用协议。"""

    async def initialize(self) -> None:
        """初始化异步资源；失败时由 Registry 隔离并清理本实例。"""

        ...

    async def health_check(self) -> HealthResult:
        """返回标准健康结果，不得修改 Registry 静态快照。"""

        ...

    async def invoke(
        self,
        run_context: RunContext,
        agent_context: AgentContext,
        task_input: TaskInput,
    ) -> AgentResult:
        """通过三上下文执行一次调用并返回标准 AgentResult。"""

        ...


class CapabilityFactory(Protocol):
    """Manifest entrypoint 指向的同步统一工厂协议。"""

    def __call__(
        self,
        bootstrap_context: CapabilityBootstrapContext,
    ) -> Capability:
        """同步创建 Capability；异步资源必须延后到 initialize。"""

        ...


def _freeze_configuration_mapping(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    """深复制并冻结 Capability 的非敏感配置视图。"""

    if not isinstance(value, Mapping):
        raise TypeError("capability_config 必须是映射")
    frozen = _freeze_configuration_value(
        value,
        active_ids=set(),
        depth=0,
    )
    if not isinstance(frozen, Mapping):
        raise TypeError("capability_config 必须是映射")
    return frozen


def _freeze_configuration_value(
    value: object,
    *,
    active_ids: set[int],
    depth: int,
) -> object:
    """递归冻结关闭配置值，并拒绝未知可变对象与循环引用。"""

    if depth > _MAX_INTERNAL_DETAILS_DEPTH:
        raise ValueError("capability_config 嵌套过深")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("capability_config 不允许非有限浮点数")
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("capability_config 不允许循环引用")
        if any(type(key) is not str for key in value):
            raise TypeError("capability_config 的映射键必须是字符串")
        active_ids.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _freeze_configuration_value(
                        nested,
                        active_ids=active_ids,
                        depth=depth + 1,
                    )
                    for key, nested in value.items()
                }
            )
        finally:
            active_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("capability_config 不允许循环引用")
        active_ids.add(identity)
        try:
            return tuple(
                _freeze_configuration_value(
                    item,
                    active_ids=active_ids,
                    depth=depth + 1,
                )
                for item in value
            )
        finally:
            active_ids.remove(identity)
    raise TypeError("capability_config 包含不支持的值类型")


def _freeze_json_mapping(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    """校验并冻结 CapabilityError 的内部 JSON 详情。"""

    frozen = _freeze_json_value(
        value,
        active_ids=set(),
        depth=0,
    )
    if not isinstance(frozen, Mapping):
        raise TypeError("details 根节点必须是 JSON 对象")
    return frozen


def _freeze_json_value(
    value: object,
    *,
    active_ids: set[int],
    depth: int,
) -> object:
    """仅接受有限深度、有限数值且键为字符串的 JSON 值。"""

    if depth > _MAX_INTERNAL_DETAILS_DEPTH:
        raise ValueError("details 嵌套过深")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("details 不允许非有限浮点数")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("details 不允许循环引用")
        if any(type(key) is not str for key in value):
            raise TypeError("details 的对象键必须是字符串")
        active_ids.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _freeze_json_value(
                        nested,
                        active_ids=active_ids,
                        depth=depth + 1,
                    )
                    for key, nested in value.items()
                }
            )
        finally:
            active_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("details 不允许循环引用")
        active_ids.add(identity)
        try:
            return tuple(
                _freeze_json_value(
                    item,
                    active_ids=active_ids,
                    depth=depth + 1,
                )
                for item in value
            )
        finally:
            active_ids.remove(identity)
    raise TypeError("details 包含非 JSON 值")
