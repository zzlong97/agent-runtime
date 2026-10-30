"""固定六请求的真实联调探针；输出仅身份、状态、计数和布尔断言。"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

import httpx

INPUT = Path(".dev-team/tasks/RA-INT-01/evidence/真实联调执行输入.json")
BASE = "http://127.0.0.1:18781"


def facts(client, run_id):
    """事实端点已在服务端剔除正文；进一步汇总避免复制重复事件。"""
    response = client.get(f"{BASE}/__ra_int__/facts/{run_id}")
    response.raise_for_status()
    data = response.json()
    events = data.pop("events")
    data["event_counts"] = {kind: sum(e["event_type"] == kind for e in events)
                            for kind in dict.fromkeys(e["event_type"] for e in events)}
    data["identity_events"] = [{k: v for k, v in e.items() if v is not None}
                               for e in events if e["visibility"] == "internal"]
    seqs = [e["seq"] for e in events]
    data["seq_unique_increasing"] = seqs == sorted(set(seqs))
    data["observations"] = [item for item in data["observations"] if item["task_id"] in {
        e["remote_task_id"] for e in events if e["remote_task_id"]}]
    return data


def verify_sse(client, run_id):
    """仅在终态读取SSE全文到内存，保留类型、游标和安全字段断言。"""
    import agent_runtime.chat
    from agent_runtime.runtime.event_schemas import parse_public_event_json

    response = client.get(f"{BASE}/api/v1/chat/runs/{run_id}/events")
    response.raise_for_status()
    events = []
    for block in response.text.split("\n\n"):
        kind = next((line[7:] for line in block.splitlines() if line.startswith("event: ")), None)
        payload = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
        if kind and payload:
            projected = parse_public_event_json(payload)
            assert projected.event_type == kind
            value = json.loads(payload)
            assert not kind.startswith("internal.")
            encoded = json.dumps(value)
            assert all(secret not in encoded for secret in (
                '"remote_context_id"', '"remote_task_id"', '"checkpoint"',
                '"system_prompt"', '"input_payload"', '"authorization"'))
            events.append({"type": kind, "seq": value.get("seq")})
    assert events
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs))
    final = [e for e in events if e["type"] in {"run.completed", "run.cancelled", "run.failed"}]
    assert len(final) == 1
    assert sum(e["type"] == "message.finalized" for e in events) == 1
    reconnect = client.get(f"{BASE}/api/v1/chat/runs/{run_id}/events", headers={"Last-Event-ID": str(seqs[-1])})
    reconnect.raise_for_status()
    assert "event: " not in reconnect.text
    return {"events": events, "unique_terminal": True, "unique_finalized": True,
            "privacy_whitelist": True, "reconnect_after_terminal_empty": True}


def main():
    """每个业务动作必须先由执行者保存意图；本脚本不自动重试POST。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["submit", "status", "resume", "resume_duplicate", "resume_conflict", "cancel", "delete", "sse", "duplicate", "conflict"])
    parser.add_argument("case")
    parser.add_argument("--round", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    inputs = json.loads(INPUT.read_text(encoding="utf-8"))
    case = next(item for item in inputs["cases"] if item["case"] == args.case)
    receipt = json.loads(Path(".dev-team/tasks/RA-INT-01/evidence/真实联调运行回执.json").read_text(encoding="utf-8"))
    recorded = next(item for item in receipt["cases"] if item["case"] == args.case)
    with httpx.Client(trust_env=False, timeout=15) as client:
        if args.action in {"submit", "duplicate", "conflict"}:
            if args.action == "submit":
                assert not recorded.get("run_id"), "已有Run请查询原身份，禁止重新创建"
            else:
                # Session删除会清理本地幂等事实；禁止把额度已消耗的旧ID当新任务重做。
                existing = client.get(BASE + f"/__ra_int__/facts/{recorded['run_id']}")
                assert existing.status_code == 200, "原Run已不存在，禁止再次提交远程业务"
            text = "请提取共性条款" if args.case == "two_round_input" else "提取 2026-10-01 至 2026-10-20 的共性条款。"
            if args.action == "conflict":
                text += "同ID冲突验证"
            response = client.post(BASE + "/api/v1/chat/completions", json={
                "request_id": case["request_id"], "message": {"content": text}})
            if args.action == "conflict":
                assert response.status_code == 409
                result = {"http_status": 409, "same_request_changed_body_conflict": True}
            else:
                assert response.status_code == 202
                result = {"http_status": response.status_code, **response.json()}
        elif args.action == "status":
            result = facts(client, recorded["run_id"])
        elif args.action == "sse":
            result = verify_sse(client, recorded["run_id"])
        elif args.action in {"resume", "resume_duplicate", "resume_conflict"}:
            active = client.get(BASE + f"/api/v1/chat/sessions/{recorded['session_id']}/active-run").json()
            pending = active["pending_interrupt"] if args.action == "resume" else recorded["resumes"][args.round - 1]
            request_id = str(uuid5(UUID(inputs["batch_id"]), f"two_round_input:resume:{args.round}"))
            answer = "2026-10-01" if args.round == 1 else "2026-10-20"
            if args.action == "resume_conflict":
                answer += "同ID冲突验证"
            response = client.post(BASE + f"/api/v1/chat/runs/{recorded['run_id']}/resume", json={
                "interrupt_id": pending["interrupt_id"], "request_id": request_id,
                "resume_payload": {"answer": answer}})
            if args.action == "resume_conflict":
                assert response.status_code == 409
                result = {"http_status": 409, "changed_resume_body_conflict": True}
            else:
                assert response.status_code == 202
                result = {"http_status": 202, **response.json(), "interrupt_id": pending["interrupt_id"], "resume_request_id": request_id}
        elif args.action == "cancel":
            response = client.post(BASE + f"/api/v1/chat/runs/{recorded['run_id']}/cancel")
            assert response.status_code == 202
            result = {"http_status": 202, **response.json()}
        else:
            response = client.delete(BASE + f"/api/v1/chat/sessions/{recorded['session_id']}")
            result = {"http_status": response.status_code}
            if response.status_code == 409:
                result["retryable"] = response.json()["detail"].get("retryable")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
