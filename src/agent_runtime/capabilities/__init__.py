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

__all__ = [
    "AgentResult",
    "AgentResultMetadata",
    "Capability",
    "CapabilityBootstrapContext",
    "CapabilityError",
    "CapabilityFactory",
    "CapabilityRegistry",
    "CapabilityRegistryEntry",
    "HealthResult",
    "RouterProjection",
]
