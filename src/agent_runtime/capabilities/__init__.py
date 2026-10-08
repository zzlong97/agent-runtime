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
    CapabilityTaskRepository,
    UserCapabilityPermissionRepository,
)
from agent_runtime.capabilities.persistence_models import (
    CapabilityOperation,
    CapabilityTask,
    CapabilityTaskContext,
    UserCapabilityPermission,
)

__all__ = [
    "AgentResult",
    "AgentResultMetadata",
    "Capability",
    "CapabilityBootstrapContext",
    "CapabilityError",
    "CapabilityFactory",
    "CapabilityOperation",
    "CapabilityOperationCreateResult",
    "CapabilityOperationRepository",
    "CapabilityRegistry",
    "CapabilityRegistryEntry",
    "CapabilityTask",
    "CapabilityTaskContext",
    "CapabilityTaskContextRepository",
    "CapabilityTaskRepository",
    "HealthResult",
    "RouterProjection",
    "UserCapabilityPermission",
    "UserCapabilityPermissionRepository",
]
