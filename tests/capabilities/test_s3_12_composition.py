"""S3-12 正式链路移除固定能力分发的静态回归。"""

from pathlib import Path


def test_formal_runtime_has_no_builtin_adapter_dispatch() -> None:
    """Composition Root、Parent、Router 与恢复器不得按两个测试 ID 分支。"""

    source_root = Path(__file__).resolve().parents[2] / "src" / "agent_runtime"
    sources = {
        relative: (source_root / relative).read_text(encoding="utf-8")
        for relative in (
            "chat.py",
            "graph/parent.py",
            "graph/router.py",
            "runtime/checkpoint_recovery.py",
        )
    }

    assert "GeneralChatAdapter" not in sources["chat.py"]
    assert "EnglishToChineseAdapter" not in sources["chat.py"]
    assert '== "general_chat"' not in sources["chat.py"]
    assert '== "en_to_zh"' not in sources["chat.py"]
    assert "_LEGACY_PROJECTIONS" not in sources["graph/router.py"]
    assert "_CAPABILITY_IDS" not in sources["graph/parent.py"]
    assert (
        "_AUTOMATIC_RECOVERY_CAPABILITIES"
        not in sources["runtime/checkpoint_recovery.py"]
    )


def test_formal_composition_uses_registry_and_generic_gateway() -> None:
    """聊天服务正式组装必须由本地 Manifest 驱动统一 Gateway。"""

    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agent_runtime"
        / "chat.py"
    ).read_text(encoding="utf-8")

    assert "LocalYamlCapabilitySource" in source
    assert "CapabilityRegistry.build" in source
    assert "RouterCandidateProvider" in source
    assert "CapabilityInvocationGateway" in source
    assert "capability_gateway.invoke" in source
    assert "child_checkpointer_by_capability" in source
    assert "checkpointers.children" in source
