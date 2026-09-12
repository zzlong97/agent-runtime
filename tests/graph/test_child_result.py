import pytest
from pydantic import ValidationError


def test_child_result_schema_describes_control_plane_fields() -> None:
    from agent_runtime.graph.child_result import ChildResult

    schema = ChildResult.model_json_schema()

    assert set(schema["properties"]) == {"status", "control_signal"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["status"]["description"] == (
        "Child Agent 本轮执行的控制状态；Stage 1 只允许 completed、rejected "
        "或 failed，分别表示成功完成、能力边界拒绝或实际执行失败。"
    )
    assert schema["properties"]["control_signal"]["description"] == (
        "Child Agent 交给 Parent Graph 的控制信号；Stage 1 只允许 "
        "OUT_OF_SCOPE 或 null，rejected 必须搭配 OUT_OF_SCOPE，completed 和 "
        "failed 必须搭配 null。"
    )


def test_child_result_accepts_only_valid_status_signal_combinations() -> None:
    from agent_runtime.graph.child_result import ChildResult

    assert ChildResult(status="completed", control_signal=None).status == "completed"
    assert ChildResult(
        status="rejected",
        control_signal="OUT_OF_SCOPE",
    ).control_signal == "OUT_OF_SCOPE"
    assert ChildResult(status="failed", control_signal=None).status == "failed"

    invalid_results = [
        {"status": "completed", "control_signal": "OUT_OF_SCOPE"},
        {"status": "rejected", "control_signal": None},
        {"status": "failed", "control_signal": "OUT_OF_SCOPE"},
    ]
    for invalid_result in invalid_results:
        with pytest.raises(ValidationError):
            ChildResult.model_validate(invalid_result)


def test_child_result_does_not_contain_user_visible_content() -> None:
    from agent_runtime.graph.child_result import ChildResult

    with pytest.raises(ValidationError):
        ChildResult.model_validate(
            {
                "status": "completed",
                "control_signal": None,
                "content": "不应进入控制面",
            }
        )
