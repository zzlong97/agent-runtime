"""真实远程能力的默认禁用与启用后普通Router候选边界。"""

import asyncio
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.registry import CapabilityRegistry
from agent_runtime.capabilities.source import LocalYamlCapabilitySource
from tests.capabilities.test_builtin_capability_entrypoints import _BuiltinCapabilityFakeModel


ROOT = Path(__file__).parents[2] / "src/agent_runtime/capabilities"
REMOTE = ROOT / "semantic_pattern_mining/manifest.yaml"


def test_formal_remote_manifest_is_disabled_and_keeps_reviewed_policy():
    assert REMOTE.is_file(), "正式远程Manifest尚未交付"
    manifest = CapabilityManifest.model_validate(yaml.safe_load(REMOTE.read_text(encoding="utf-8")))
    assert manifest.capability_id == "semantic_pattern_mining"
    assert manifest.enabled is False
    assert manifest.transport.agent_card_url == "http://127.0.0.1:18080/.well-known/agent-card.json"
    assert manifest.state_scope == "invocation"
    assert manifest.recovery_policy == "automatic"
    assert manifest.side_effect_policy == "idempotent"
    assert manifest.concurrency.max_concurrency == 1
    assert manifest.execution.timeout_seconds == 900


def test_default_scan_keeps_two_local_capabilities_without_remote_construction():
    async def run():
        def forbid_remote(_manifest):
            raise AssertionError("默认禁用能力不得构造客户端或读取Card")

        registry = await CapabilityRegistry.build(
            LocalYamlCapabilitySource(ROOT),
            child_checkpointer_factory=lambda _manifest: MemorySaver(),
            model_factory=_BuiltinCapabilityFakeModel,
            remote_capability_factory=forbid_remote,
        )
        try:
            assert len(registry.entries) == 3
            assert {p.capability_id for p in registry.router_projections()} == {"general_chat", "en_to_zh"}
            remote = next(e for e in registry.entries if e.capability_id == "semantic_pattern_mining")
            assert remote.status == "disabled" and remote.capability is None
        finally:
            await registry.close()

    asyncio.run(run())


def test_enabled_isolated_manifest_uses_permission_checked_four_field_candidates(tmp_path):
    from tests.e2e.real_router_runtime import prepare_manifest_directory
    from tests.fake_a2a import FakeA2A
    from agent_runtime.capabilities.remote_a2a.capability import RemoteA2ACapability
    from agent_runtime.capabilities.routing import CapabilityPermissionService, RouterCandidateProvider
    from tests.capabilities.test_permission_routing import FakePermissionRepository

    async def run():
        source = prepare_manifest_directory(tmp_path)
        fake = FakeA2A()
        fake.card["supportedInterfaces"][0]["url"] = "http://127.0.0.1:18080/api"
        registry = await CapabilityRegistry.build(
            source,
            child_checkpointer_factory=lambda _manifest: MemorySaver(),
            model_factory=_BuiltinCapabilityFakeModel,
            remote_capability_factory=lambda manifest: RemoteA2ACapability(
                manifest=manifest, development_mode=True, http_transport=fake.transport,
            ),
        )
        permissions = FakePermissionRepository({"general_chat": True, "en_to_zh": True})
        provider = RouterCandidateProvider(registry=registry, permission_service=CapabilityPermissionService(permissions))
        try:
            first = await provider.get_candidates(user_id="ra-int02-test", rejected_capability_ids=())
            assert {p.capability_id for p in first} == {"general_chat", "en_to_zh"}
            permissions.values["semantic_pattern_mining"] = True
            second = await provider.get_candidates(user_id="ra-int02-test", rejected_capability_ids=())
            assert {p.capability_id for p in second} == {"general_chat", "en_to_zh", "semantic_pattern_mining"}
            for projection in second:
                assert set(projection.model_dump()) == {"capability_id", "name", "description", "enabled"}
            from agent_runtime.graph.router import StageOneRouter
            from langchain_core.messages import HumanMessage

            class CandidateAwareFakeModel(_BuiltinCapabilityFakeModel):
                def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                    prompt = str(messages[-1].content)
                    assert all(capability_id in prompt for capability_id in (
                        "general_chat", "en_to_zh", "semantic_pattern_mining"))
                    return self._tool_result("RouterDecision", {
                        "capability_id": "semantic_pattern_mining", "task_action": "new", "confidence": 1.0})

            router = StageOneRouter(CandidateAwareFakeModel(), candidate_provider=provider, user_id="ra-int02-test")
            route = await router.route({"messages": [HumanMessage(content="提取授权日期的共性条款")],
                                        "rejected_capability_ids": []}, {})
            assert route["resolved_capability_id"] == "semantic_pattern_mining"
            assert yaml.safe_load(REMOTE.read_text(encoding="utf-8"))["enabled"] is False
        finally:
            await registry.close()

    asyncio.run(run())
