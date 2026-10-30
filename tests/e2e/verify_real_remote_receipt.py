"""离线复核真实联调回执；不发业务请求、不接数据库、不消耗Task额度。"""

import json
from collections import Counter
from pathlib import Path


def verify():
    """以最初固定输入与最终公开/内部身份摘要检查六场景的证据闭环。"""
    root = Path(".dev-team/tasks/RA-INT-01/evidence")
    inputs = json.loads((root / "真实联调执行输入.json").read_text(encoding="utf-8"))
    receipt = json.loads((root / "真实联调运行回执.json").read_text(encoding="utf-8"))
    assert receipt["batch_id"] == inputs["batch_id"]
    cases = receipt["cases"]
    assert [(c["case"], c["request_id"]) for c in cases] == [(c["case"], c["request_id"]) for c in inputs["cases"]]
    assert receipt["new_remote_tasks"] == receipt["maximum_new_remote_tasks"] == len(cases) == 6
    assert len({c["remote_task_id"] for c in cases}) == 6
    assert Counter(c["final_status"] for c in cases) == {"completed": 4, "cancelled": 2}
    for case in cases:
        facts = case["facts"]
        run = facts["run"]
        assert run["run_id"] == case["run_id"] and run["request_id"] == case["request_id"]
        assert run["recovery_attempts"] == case["final_recovery_attempts"]
        assert case["initial_submit_response"]["status"] == "queued"
        assert [m["role"] for m in facts["history"]] == ["user", "assistant"]
        assert all(m["nonempty"] for m in facts["history"])
        assert facts["history"][1]["message_id"] == case["response_message_id"]
        if case["final_status"] == "completed":
            assert facts["history"][1]["contains_start"] and facts["history"][1]["contains_end"]
            assert facts["history"][1]["capability_id"] == "semantic_pattern_mining"
        counts = facts["event_counts"]
        assert counts["internal.capability.invocation.started"] == 1
        assert counts["internal.remote_agent.task_bound"] == counts["internal.remote_agent.task_terminal"] == 1
        assert counts["message.finalized"] == 1
        assert counts["run.completed" if case["final_status"] == "completed" else "run.cancelled"] == 1
        assert facts["seq_unique_increasing"] and case["sse"]["unique_terminal"]
        remote = case["remote_database"]
        assert remote["task_id"] == case["remote_task_id"] and remote["context_id"] == case["remote_context_id"]
        assert remote["business_run_count"] == 1
        assert remote["distinct_messages"] == (3 if case["case"] == "two_round_input" else 1)
        assert remote["state"] == ("TASK_STATE_COMPLETED" if case["final_status"] == "completed" else "TASK_STATE_CANCELED")
        for business in remote["business_runs"]:
            assert business["common_clauses"] == business["unique_common_clause_ids"]
    for index in (3, 4):
        case = cases[index]
        assert [item["attempt"] for item in case["restarts"]] == list(range(6))
        assert case["final_recovery_attempts"] == 5
        for item in case["restarts"]:
            assert item["facts"]["tasks"][0]["last_invocation_id"] == case["invocation_id"]
            assert item["facts"]["tasks"][0]["task_id"] == case["local_task_id"]
            if index == 3 and item["attempt"] < 5:
                assert not item["facts"]["event_counts"].get("internal.remote_agent.task_bound")
                assert item["facts"]["operations"][0]["status"] == "pending"
            if index == 4 and item["attempt"] > 0:
                assert not any(o["operation"] == "send" for o in item["health"]["observations"])
    barrier = cases[5]
    assert barrier["delete_unknown"]["http_status"] == 409 and barrier["delete_unknown"]["retryable"]
    assert barrier["delete_unknown"]["facts"]["run"]["status"] == "cancel_requested"
    assert barrier["delete_confirmed"]["http_status"] == 204
    assert all(count == 0 for count in barrier["delete_confirmed"]["local_rows_remaining"].values())
    assert receipt["final_remote_metadata"]["all_counts"]["tasks"] - receipt["initial_remote_counts"]["tasks"] == 6
    print(json.dumps({"cases": 6, "completed": 4, "cancelled": 2,
        "recovery_attempts_each_window": 5, "new_remote_tasks": 6,
        "unique_identity_and_public_result": True, "session_delete_barrier": True}))


if __name__ == "__main__":
    verify()
