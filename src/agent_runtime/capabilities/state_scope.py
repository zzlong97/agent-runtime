"""Capability 私有 State 的线程命名与版本兼容边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_runtime.capabilities.manifest import (
    STATE_SCHEMA_VERSION_PATTERN,
    CapabilityManifest,
    validate_capability_id,
)
from agent_runtime.capabilities.persistence_models import CapabilityTask
from agent_runtime.core.errors import ApplicationError

StateScope = Literal["invocation", "run", "session"]


class CapabilityStateVersionIncompatibleError(ApplicationError):
    """当前实现不能读取 Task 已固化的私有 State Schema。"""


@dataclass(frozen=True, slots=True)
class StateCompatibilityPolicy:
    """保存一个 Capability 当前可读取的 State Schema 版本集合。"""

    current_version: str
    compatible_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝空版本、非法版本和重复的兼容版本声明。"""

        versions = (self.current_version, *self.compatible_versions)
        if any(
            type(version) is not str
            or STATE_SCHEMA_VERSION_PATTERN.fullmatch(version) is None
            for version in versions
        ):
            raise ValueError("State Schema 版本格式非法")
        if len(self.compatible_versions) != len(set(self.compatible_versions)):
            raise ValueError("兼容 State Schema 版本不得重复")

    @classmethod
    def from_manifest(
        cls,
        manifest: CapabilityManifest,
    ) -> StateCompatibilityPolicy:
        """从已校验 Manifest 构造不可变兼容策略。"""

        return cls(
            current_version=manifest.state_schema_version,
            compatible_versions=manifest.compatible_state_schema_versions,
        )

    @property
    def allowed_versions(self) -> frozenset[str]:
        """返回当前实现允许继续读取的完整版本集合。"""

        return frozenset((self.current_version, *self.compatible_versions))

    def is_compatible(self, task_state_schema_version: str) -> bool:
        """判断 Task 固化版本是否可由当前实现读取。"""

        return task_state_schema_version in self.allowed_versions

    def ensure_compatible(self, task_state_schema_version: str) -> None:
        """不兼容时以稳定错误拒绝 continue，不修改 Task。"""

        if self.is_compatible(task_state_schema_version):
            return
        raise CapabilityStateVersionIncompatibleError(
            code="CAPABILITY_STATE_VERSION_INCOMPATIBLE",
            message="当前 Capability 无法继续读取该 Task 的状态版本",
            status_code=409,
            retryable=False,
        )


class ChildThreadIdFactory:
    """按 Stage 3 固定 namespace 生成确定性 Child thread_id。"""

    _NAMESPACE_VERSION = "v1"

    def build(
        self,
        *,
        state_scope: StateScope,
        session_id: UUID,
        capability_id: str,
        state_schema_version: str,
        run_id: UUID | None = None,
        invocation_id: UUID | None = None,
        task_id: UUID | None = None,
    ) -> str:
        """仅使用所选 scope 的共享键构造单行线程标识。"""

        if not isinstance(session_id, UUID):
            raise TypeError("session_id 必须是 UUID")
        normalized_capability_id = validate_capability_id(capability_id)
        if (
            type(state_schema_version) is not str
            or STATE_SCHEMA_VERSION_PATTERN.fullmatch(state_schema_version)
            is None
        ):
            raise ValueError("state_schema_version 格式非法")

        if state_scope == "invocation":
            scope_name = "invocation"
            scope_id = self._require_uuid(invocation_id, "invocation_id")
        elif state_scope == "run":
            scope_name = "run"
            scope_id = self._require_uuid(run_id, "run_id")
        elif state_scope == "session":
            scope_name = "task"
            scope_id = self._require_uuid(task_id, "task_id")
        else:
            raise ValueError("state_scope 只允许 invocation、run 或 session")

        return (
            f"capability:{self._NAMESPACE_VERSION}:{session_id}:"
            f"{normalized_capability_id}:schema:{state_schema_version}:"
            f"{scope_name}:{scope_id}"
        )

    def build_for_manifest(
        self,
        *,
        manifest: CapabilityManifest,
        task: CapabilityTask,
        run_id: UUID,
        invocation_id: UUID,
    ) -> str:
        """使用 Manifest scope 和 Task 固化版本生成兼容的线程标识。"""

        if task.capability_id != manifest.capability_id:
            raise ValueError("Task 与 Manifest 的 Capability ID 不一致")
        StateCompatibilityPolicy.from_manifest(manifest).ensure_compatible(
            task.state_schema_version
        )

        return self.build(
            state_scope=manifest.state_scope,
            session_id=task.session_id,
            capability_id=manifest.capability_id,
            state_schema_version=task.state_schema_version,
            run_id=run_id,
            invocation_id=invocation_id,
            task_id=task.task_id,
        )

    @staticmethod
    def _require_uuid(value: UUID | None, field_name: str) -> UUID:
        """保证选中 scope 的共享键存在且类型明确。"""

        if not isinstance(value, UUID):
            raise ValueError(f"{field_name} 是当前 state_scope 的必填 UUID")
        return value
