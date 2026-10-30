"""固定一请求的普通Router探针：默认只查询，显式批准后仅提交一次。"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import httpx

from tests.e2e.real_router_runtime import BASE, BATCH, CAPABILITIES, DATABASE, REQUEST_ID, USER

QUESTION = "请使用语义模式挖掘能力，提取2026-10-01至2026-10-20授权隔离数据中的共性条款。"
RECEIPT = Path(".dev-team/tasks/RA-INT-02/evidence/普通Router受控复测回执.json")


def _save(path: Path, value: dict):
    """回执只存身份、状态和布尔结论，不写输入、模型输出或内部State。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def execute(action: str, client: httpx.Client, receipt_path: Path, *, approved: bool):
    """先查固定请求；意图存在而结果未知时，只查询，绝不自动重试POST。"""
    health = client.get(BASE + "/__ra_router__/health")
    health.raise_for_status()
    info = health.json()
    if (info.get("batch_id"), info.get("database"), info.get("user_id"), info.get("router_mode")) != (
            BATCH, DATABASE, USER, "production"):
        raise RuntimeError("隔离Runtime身份或普通Router模式不符合本批范围")
    recorded = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
    if recorded and (recorded.get("batch_id"), recorded.get("request_id")) != (BATCH, REQUEST_ID):
        raise RuntimeError("回执不是本批固定请求，禁止覆盖或提交")
    existing = client.get(BASE + "/__ra_router__/requests/" + REQUEST_ID)
    if existing.status_code == 200:
        from agent_runtime.api.schemas.runs import RunSummaryResponse
        run = existing.json()
        if run.pop("request_id") != REQUEST_ID:
            raise RuntimeError("查询返回的请求身份不匹配")
        run = RunSummaryResponse.model_validate(run).model_dump(mode="json")
        result = {"batch_id": BATCH, "request_id": REQUEST_ID, "status": "accepted", "run": run}
        if action == "verify":
            result["verification"] = verify(client, run)
        _save(receipt_path, result)
        return result
    if existing.status_code != 404:
        existing.raise_for_status()
    if action != "submit":
        return {"batch_id": BATCH, "request_id": REQUEST_ID, "run": None,
                "receipt_status": recorded.get("status") if recorded else None}
    if not approved:
        raise RuntimeError("新增在线模型/远程任务必须先取得本批批准")
    if recorded:
        raise RuntimeError("已有提交意图或原Run已删除，禁止重送；先由执行者核对实际结果")
    if set(info.get("capability_ids", [])) != CAPABILITIES:
        raise RuntimeError("三个候选尚未全部健康可用，禁止创建新任务")
    intent = {"batch_id": BATCH, "request_id": REQUEST_ID, "status": "sending",
              "intent_at": datetime.now(UTC).isoformat()}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建保证两个探针不能同时消费同一个新Task额度。
    with receipt_path.open("x", encoding="utf-8") as handle:
        json.dump(intent, handle, ensure_ascii=False)
    try:
        response = client.post(BASE + "/api/v1/chat/completions", json={
            "request_id": REQUEST_ID, "message": {"content": QUESTION}})
        if response.status_code != 202:
            raise RuntimeError("提交未返回202；保留意图并核对原请求，禁止自动重试")
        from agent_runtime.api.schemas.runs import RunSummaryResponse
        run = RunSummaryResponse.model_validate(response.json()).model_dump(mode="json")
    except Exception:
        _save(receipt_path, {**intent, "status": "uncertain"})
        raise
    result = {**intent, "status": "accepted", "run": run}
    _save(receipt_path, result)
    return result


def verify(client: httpx.Client, run: dict):
    """只在已完成Run读取公共历史/SSE，输出唯一性、能力及文本非空布尔结果。"""
    if run["status"] != "completed":
        raise RuntimeError("Run尚未completed；只查询原Run，不创建替代任务")
    response = client.get(BASE + f"/api/v1/chat/sessions/{run['session_id']}/messages")
    response.raise_for_status()
    from agent_runtime.api.schemas.messages import MessageHistoryResponse
    items = MessageHistoryResponse.model_validate(response.json()).items
    users = [m for m in items if m.role == "user"]
    assistants = [m for m in items if m.role == "assistant"]
    if len(users) != 1 or len(assistants) != 1:
        raise RuntimeError("新会话公共消息不是唯一Human+AI")
    reply = assistants[0]
    if (str(reply.message_id) != run["response_message_id"] or reply.runtime_status != "completed"
            or reply.capability_id != "semantic_pattern_mining" or not reply.content.strip()):
        raise RuntimeError("普通Router未完成预期远程能力或公开结果不符合约定")
    response = client.get(BASE + f"/api/v1/chat/runs/{run['run_id']}/events")
    response.raise_for_status()
    from agent_runtime.runtime.event_schemas import parse_public_event_json
    events = [parse_public_event_json(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    seqs = [event.seq for event in events]
    if not events or seqs != sorted(set(seqs)):
        raise RuntimeError("公开事件序号缺失、重复或倒退")
    if any(str(event.run_id) != run["run_id"] for event in events):
        raise RuntimeError("公开事件不属于本批同一Run")
    terminal = [event for event in events if event.event_type in {"run.completed", "run.failed", "run.cancelled"}]
    finalized = [event for event in events if event.event_type == "message.finalized"]
    if len(finalized) != 1 or len(terminal) != 1 or terminal[0].event_type != "run.completed":
        raise RuntimeError("缺少唯一公共消息完成和Run终态事件")
    if (str(finalized[0].payload.response_message_id) != run["response_message_id"]
            or finalized[0].payload.capability_id != "semantic_pattern_mining"
            or finalized[0].payload.runtime_status != "completed"):
        raise RuntimeError("公开完成事件与已持久公共回答不一致")
    return {"unique_public_turn": True, "remote_capability": True, "nonempty_reply": True,
            "unique_terminal": True, "seq_unique_increasing": True}


def main():
    """无参数只做只读查询；submit必须先取得新的批准额度。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=["status", "submit", "verify"], default="status")
    args = parser.parse_args()
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        result = execute(args.action, client, RECEIPT, approved=os.getenv("RA_INT02_APPROVED") == BATCH)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
