"""仅供离线协议互操作使用的 A2A 1.0 HTTP 替身。"""

from copy import deepcopy
import json
from uuid import uuid4

import httpx


class FakeA2A:
    """保存远程任务与 messageId 指纹，模拟受理后断连和非阻塞任务。"""

    def __init__(self, *, state="TASK_STATE_COMPLETED", bearer=False, tenant=""):
        self.card = {
            "name": "离线测试 Agent",
            "description": "验证远程文本协议",
            "version": "1.0.0",
            "supportedInterfaces": [
                {
                    "url": "https://agent.example/api",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                    "tenant": tenant,
                }
            ],
            "capabilities": {},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
        }
        if bearer:
            self.card["securitySchemes"] = {
                "token": {"httpAuthSecurityScheme": {"scheme": "bearer"}}
            }
            self.card["securityRequirements"] = [{"schemes": {"token": {}}}]
        self.state = state
        self.requests = []
        self.tasks = {}
        self.messages = {}
        self.disconnect_after_accept = False
        self.disconnect_get = False
        self.disconnect_cancel = False
        self.cancel_state = "TASK_STATE_CANCELED"
        self.out_of_scope = False
        self.business_creations = 0
        self.input_prompts = None
        self.answers = {}
        self.status_code = None
        self.raw_response = None
        self.redirect = None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        if self.redirect:
            return httpx.Response(307, headers={"location": self.redirect})
        if self.status_code:
            return httpx.Response(
                self.status_code,
                json={"error": {"message": "远程内部正文与 Token 不得透传"}},
            )
        if self.raw_response is not None:
            return httpx.Response(200, json=self.raw_response)
        if request.url.path.endswith("agent-card.json"):
            return httpx.Response(200, json=deepcopy(self.card))
        if request.url.path.endswith("/message:send"):
            payload = json.loads(request.content)
            message = payload["message"]
            key = message["messageId"]
            fingerprint = json.dumps(payload, sort_keys=True)
            if key in self.messages:
                previous_fingerprint, task_id = self.messages[key]
                if previous_fingerprint != fingerprint:
                    return httpx.Response(
                        409, json={"error": {"message": "messageId 冲突"}}
                    )
            else:
                task_id = message.get("taskId") or str(uuid4())
                context_id = message.get("contextId") or str(uuid4())
                task = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {"state": self.state},
                }
                if self.state == "TASK_STATE_REJECTED" and self.out_of_scope:
                    task["metadata"] = {"control_signal": "OUT_OF_SCOPE"}
                elif not message.get("taskId"):
                    self.business_creations += 1
                if self.input_prompts is not None and message.get("taskId"):
                    self.answers[key] = "".join(part["text"] for part in message["parts"])
                    answered = sum(1 for _, stored_id in self.messages.values() if stored_id == task_id)  # 初始消息也占一次。
                    if answered >= len(self.input_prompts):
                        task["status"] = {"state": "TASK_STATE_COMPLETED"}
                        task["artifacts"] = [{"artifactId": "answer", "parts": [{"text": "参数已采用：" + "；".join(self.answers.values())}]}]
                    else:
                        task["status"] = {"state": "TASK_STATE_INPUT_REQUIRED", "message": {
                            "messageId": f"input-{answered + 1}", "role": "ROLE_AGENT", "parts": [{"text": self.input_prompts[answered]}]}}
                if self.state == "TASK_STATE_COMPLETED":
                    task["artifacts"] = [
                        {
                            "artifactId": "answer",
                            "parts": [{"text": "第一段"}, {"text": "第二段"}],
                        }
                    ]
                elif self.state == "TASK_STATE_INPUT_REQUIRED" and "message" not in task["status"] and task["status"]["state"] != "TASK_STATE_COMPLETED":
                    task["status"]["message"] = {
                        "messageId": "input-1",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": self.input_prompts[0] if self.input_prompts else "请补充日期"}],
                    }
                self.tasks[task_id] = task
                self.messages[key] = (fingerprint, task_id)
            if self.disconnect_after_accept:
                self.disconnect_after_accept = False
                raise httpx.ReadError("模拟远程受理后断连", request=request)
            return httpx.Response(200, json={"task": deepcopy(self.tasks[task_id])})
        if "/tasks/" in request.url.path:
            task_id = request.url.path.split("/tasks/", 1)[1].removesuffix(":cancel")
            if task_id not in self.tasks:
                return httpx.Response(404, json={"error": {"message": "未知任务"}})
            if request.url.path.endswith(":cancel"):
                self.tasks[task_id]["status"] = {"state": self.cancel_state}
                if self.disconnect_cancel:
                    raise httpx.ReadError("模拟取消响应未知", request=request)
            elif self.disconnect_get:
                raise httpx.ReadError("模拟查询不可达", request=request)
            return httpx.Response(200, json=deepcopy(self.tasks[task_id]))
        return httpx.Response(404)


def remote_transport(
    *, auth=None, url="https://agent.example/.well-known/agent-card.json"
):
    return {
        "type": "remote_a2a",
        "agent_card_url": url,
        "auth": auth or {"type": "none"},
    }
