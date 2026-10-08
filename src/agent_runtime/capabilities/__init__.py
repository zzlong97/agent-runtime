"""AgentRuntime 的标准 Capability 契约、静态 Registry 与能力实现。"""

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    Capability,
    CapabilityBootstrapContext,
    CapabilityError,
    CapabilityFactory,
    HealthResult,
)
from agent_runtime.capabilities.registry import (
    CapabilityRegistry,
    CapabilityRegistryEntry,
    RouterProjection,
)
from agent_runtime.capabilities.persistence import (
    CapabilityOperationCreateResult,
    CapabilityOperationRepository,
    CapabilityTaskContextRepository,
    CapabilityTaskContractViolationError,
    CapabilityTaskRepository,
    UserCapabilityPermissionRepository,
)
from agent_runtime.capabilities.persistence_models import (
    CapabilityOperation,
    CapabilityTask,
    CapabilityTaskContext,
    CapabilityTaskResolution,
    UserCapabilityPermission,
)
from agent_runtime.capabilities.invocation_service import (
    CapabilityInvocation,
    CapabilityInvocationService,
)
from agent_runtime.capabilities.state_scope import (
    CapabilityStateVersionIncompatibleError,
    ChildThreadIdFactory,
    StateCompatibilityPolicy,
)
from agent_runtime.capabilities.task_service import (
    CapabilityTaskService,
    CheckpointerThreadStore,
    ChildCheckpointStore,
)

__all__ = [
    "AgentResult",
    "AgentResultMetadata",
    "Capability",
    "CapabilityBootstrapContext",
    "CapabilityError",
    "CapabilityFactory",
    "CapabilityInvocation",
    "CapabilityInvocationService",
    "CapabilityOperation",
    "CapabilityOperationCreateResult",
    "CapabilityOperationRepository",
    "CapabilityRegistry",
    "CapabilityRegistryEntry",
    "CapabilityTask",
    "CapabilityTaskContext",
    "CapabilityTaskContextRepository",
    "CapabilityTaskContractViolationError",
    "CapabilityTaskRepository",
    "CapabilityTaskResolution",
    "CapabilityTaskService",
    "CapabilityStateVersionIncompatibleError",
    "CheckpointerThreadStore",
    "ChildCheckpointStore",
    "ChildThreadIdFactory",
    "HealthResult",
    "RouterProjection",
    "StateCompatibilityPolicy",
    "UserCapabilityPermission",
    "UserCapabilityPermissionRepository",
]
