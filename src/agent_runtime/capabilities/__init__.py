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
from agent_runtime.capabilities.manifest import ManifestCapabilityId
from agent_runtime.capabilities.health import (
    CapabilityHealthService,
    HealthSnapshot,
    ServiceabilityDecision,
)
from agent_runtime.capabilities.execution import (
    CapabilityConcurrencyController,
    CapabilityErrorMapper,
    CapabilityExecutionController,
)
from agent_runtime.capabilities.operation_ledger import (
    CapabilityOperationContext,
    CapabilityOperationGateway,
    CapabilityOperationHandle,
    build_operation_idempotency_key,
)
from agent_runtime.capabilities.registry import (
    CapabilityRegistry,
    CapabilityRegistryEntry,
    RouterProjection,
)
from agent_runtime.capabilities.routing import (
    CapabilityPermissionDeniedError,
    CapabilityPermissionService,
    CapabilityUnavailableError,
    RouterCandidateProvider,
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
    "CapabilityConcurrencyController",
    "CapabilityError",
    "CapabilityErrorMapper",
    "CapabilityExecutionController",
    "CapabilityFactory",
    "CapabilityHealthService",
    "CapabilityInvocation",
    "CapabilityInvocationService",
    "CapabilityOperation",
    "CapabilityOperationContext",
    "CapabilityOperationGateway",
    "CapabilityOperationHandle",
    "CapabilityOperationCreateResult",
    "CapabilityOperationRepository",
    "CapabilityPermissionDeniedError",
    "CapabilityPermissionService",
    "CapabilityRegistry",
    "CapabilityRegistryEntry",
    "CapabilityTask",
    "CapabilityTaskContext",
    "CapabilityTaskContextRepository",
    "CapabilityTaskContractViolationError",
    "CapabilityTaskRepository",
    "CapabilityTaskResolution",
    "CapabilityTaskService",
    "CapabilityUnavailableError",
    "CapabilityStateVersionIncompatibleError",
    "CheckpointerThreadStore",
    "ChildCheckpointStore",
    "ChildThreadIdFactory",
    "HealthResult",
    "HealthSnapshot",
    "ManifestCapabilityId",
    "RouterProjection",
    "RouterCandidateProvider",
    "ServiceabilityDecision",
    "StateCompatibilityPolicy",
    "UserCapabilityPermission",
    "UserCapabilityPermissionRepository",
    "build_operation_idempotency_key",
]
