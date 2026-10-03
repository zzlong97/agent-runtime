"""Stage 3 Capability Manifest 的严格关闭 Schema。"""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CAPABILITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ENTRYPOINT_PATTERN = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)+"
    r"[A-Za-z_][A-Za-z0-9_]*:"
    r"[A-Za-z_][A-Za-z0-9_]*$"
)
SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
STATE_SCHEMA_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class _ClosedManifestModel(BaseModel):
    """为所有 Manifest Schema 提供一致的关闭与只读约束。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ConcurrencyPolicy(_ClosedManifestModel):
    """声明单个 Capability 在当前进程内的并发准入策略。"""

    mode: Literal["unlimited", "bounded"] = Field(
        description=(
            "Capability 的单进程并发模式；unlimited 不设置并发槽，bounded 必须"
            "配合正整数 max_concurrency，Stage 3 不提供跨进程并发限制。"
        ),
    )
    max_concurrency: StrictInt | None = Field(
        default=...,
        ge=1,
        description=(
            "bounded 模式允许同时执行的最大 Invocation 数量；unlimited 模式必须"
            "显式填写 null，不允许通过缺省值推断策略。"
        ),
    )
    acquire_timeout_seconds: StrictFloat = Field(
        gt=0,
        allow_inf_nan=False,
        description=(
            "等待当前 Capability 并发槽的最长秒数，必须大于零；超时后的错误映射"
            "由后续 Runtime 治理任务实现，本阶段仅校验声明。"
        ),
    )

    @model_validator(mode="after")
    def validate_mode_combination(self) -> Self:
        """拒绝并发模式与最大并发数不一致的组合。"""

        if self.mode == "bounded" and self.max_concurrency is None:
            raise ValueError("bounded 模式必须提供正整数 max_concurrency")
        if self.mode == "unlimited" and self.max_concurrency is not None:
            raise ValueError("unlimited 模式的 max_concurrency 必须为 null")
        return self


class ExecutionPolicy(_ClosedManifestModel):
    """声明单次 Capability Invocation 的执行和取消时限。"""

    timeout_seconds: StrictFloat = Field(
        gt=0,
        allow_inf_nan=False,
        description=(
            "单次 Invocation 的最长执行秒数，必须大于零；实际计时和超时错误处理"
            "属于后续 Runtime 治理任务。"
        ),
    )
    cancel_grace_seconds: StrictFloat = Field(
        ge=0,
        allow_inf_nan=False,
        description=(
            "执行超时或取消后等待 Capability 协作退出的宽限秒数，允许为零；超过"
            "宽限期后的本地任务取消由后续 Runtime 治理任务负责。"
        ),
    )


class CapabilityManifest(_ClosedManifestModel):
    """描述一个可由 Stage 3 Runtime 静态发现的 Capability。"""

    manifest_schema_version: StrictInt = Field(
        description=(
            "Manifest 契约版本；Stage 3 仅接受整数 1，其他版本必须被隔离，且不得"
            "由加载器自动降级或猜测。"
        ),
    )
    capability_id: StrictStr = Field(
        min_length=1,
        max_length=64,
        pattern=CAPABILITY_ID_PATTERN,
        description=(
            "稳定 Capability 标识，必须匹配 ^[a-z][a-z0-9_]{0,63}$；该值可进入"
            "Router、公共消息和 RuntimeEvent，不得使用展示名称替代。"
        ),
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=80,
        description=(
            "供 Router 和内部诊断使用的人类可读名称，去除首尾空白后长度为 1～80"
            "个字符，不承载 Prompt 或运行策略。"
        ),
    )
    description: StrictStr = Field(
        min_length=1,
        max_length=500,
        description=(
            "供 Router 判断能力边界的公开说明，去除首尾空白后长度为 1～500 个"
            "字符；不得包含 System Prompt、工具参数或私有 State。"
        ),
    )
    enabled: StrictBool = Field(
        description=(
            "静态启用开关；false 表示该 Manifest 可被发现但不能成为服务候选，"
            "Stage 3 不提供运行期热切换或动态 mount。"
        ),
    )
    entrypoint: StrictStr = Field(
        pattern=ENTRYPOINT_PATTERN,
        description=(
            "统一 Capability 同步工厂的 package.module:factory_name 导入位置；只"
            "允许 Python 模块和属性标识符，不接受文件路径、调用表达式或参数。"
        ),
    )
    version: StrictStr = Field(
        pattern=SEMVER_PATTERN,
        description=(
            "Capability 当前实现的 SemVer 版本字符串；用于实现自述和内部诊断，"
            "Stage 3 不允许同一 capability_id 的多版本实现并存。"
        ),
    )
    state_scope: Literal["invocation", "run", "session"] = Field(
        description=(
            "Capability 私有 State 的共享范围，必须显式为 invocation、run 或"
            "session；Runtime 不得根据实现类型或历史配置推断默认值。"
        ),
    )
    state_schema_version: StrictStr = Field(
        min_length=1,
        max_length=64,
        pattern=STATE_SCHEMA_VERSION_PATTERN,
        description=(
            "当前 Capability 私有 State Schema 的稳定版本标识，长度为 1～64，"
            "只允许字母、数字、点、下划线和连字符，并在 Task 创建时固化。"
        ),
    )
    compatible_state_schema_versions: tuple[StrictStr, ...] = Field(
        description=(
            "当前实现可读取的旧 State Schema 版本唯一列表；允许为空，元素遵守"
            "state_schema_version 的格式和长度，重复值必须拒绝而非静默去重。"
        ),
    )
    allow_degraded: StrictBool = Field(
        description=(
            "健康检查返回 degraded 时是否仍允许 Runtime 提供服务；该布尔值必须"
            "显式声明，健康检查和候选过滤由后续任务实现。"
        ),
    )
    concurrency: ConcurrencyPolicy = Field(
        description=(
            "Capability 的单进程并发准入声明，包含并发模式、最大并发数和获取"
            "超时；Stage 3 不把该策略扩展为多实例 Lease。"
        ),
    )
    execution: ExecutionPolicy = Field(
        description=(
            "Capability 的 Invocation 执行时限声明，包含执行超时和协作取消宽限；"
            "本阶段只校验配置，不启动执行控制器。"
        ),
    )
    recovery_policy: Literal["automatic", "manual"] = Field(
        description=(
            "崩溃后是否允许 Runtime 自动重放 Invocation；automatic 受副作用策略"
            "约束，manual 在后续恢复任务中安全失败。"
        ),
    )
    side_effect_policy: Literal["none", "idempotent", "unsafe"] = Field(
        description=(
            "Capability 外部副作用策略；none 表示无副作用，idempotent 要求后续"
            "使用 Operation Ledger，unsafe 不得与 automatic 恢复组合。"
        ),
    )

    @field_validator("manifest_schema_version")
    @classmethod
    def validate_manifest_schema_version(cls, value: int) -> int:
        """Stage 3 只接受精确整数值 1 的 Manifest 契约。"""

        if value != 1:
            raise ValueError("manifest_schema_version 在 Stage 3 必须为整数 1")
        return value

    @field_validator("compatible_state_schema_versions", mode="before")
    @classmethod
    def normalize_compatible_versions_input(cls, value: Any) -> Any:
        """只把 YAML 数组转换为只读元组，不修复其他非法输入。"""

        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise ValueError("compatible_state_schema_versions 必须是 YAML 数组")

    @field_validator("compatible_state_schema_versions")
    @classmethod
    def validate_compatible_versions(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """校验兼容版本元素格式并拒绝重复值。"""

        if len(value) != len(set(value)):
            raise ValueError("compatible_state_schema_versions 不允许重复值")
        for version in value:
            if not STATE_SCHEMA_VERSION_PATTERN.fullmatch(version):
                raise ValueError(
                    "compatible_state_schema_versions 包含非法 State Schema 版本"
                )
        return value

    @model_validator(mode="after")
    def validate_recovery_combination(self) -> Self:
        """禁止无法安全自动恢复的 automatic + unsafe 组合。"""

        if (
            self.recovery_policy == "automatic"
            and self.side_effect_policy == "unsafe"
        ):
            raise ValueError("automatic 恢复策略不能与 unsafe 副作用策略组合")
        return self
