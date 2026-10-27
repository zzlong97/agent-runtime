"""校验官方 Agent Card，并固化启动期身份与安全接口。"""

from dataclasses import dataclass
import hashlib
import json
import re

import httpx
from a2a.types.a2a_pb2 import AgentCard
from google.protobuf.json_format import MessageToDict, ParseDict

from agent_runtime.capabilities.manifest import RemoteA2ATransport
from agent_runtime.capabilities.remote_a2a.errors import auth_error, protocol_error


def validated_url(value: str, *, development_mode: bool) -> httpx.URL:
    """只接受可确定来源的 HTTP(S) 地址，生产要求 HTTPS。"""
    try:
        RemoteA2ATransport.validate_card_url(value)
        url = httpx.URL(value)
        if not url.host or (url.scheme != "https" and not development_mode):
            raise ValueError
        return url
    except (TypeError, ValueError, httpx.InvalidURL):
        raise protocol_error() from None


def origin(url: httpx.URL) -> tuple[str, str, int]:
    """按规范化主机和有效端口比较来源；路径不参与比较。"""
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


@dataclass(frozen=True, slots=True)
class AgentCardSnapshot:
    """仅供 Runtime 使用的启动身份，不保存凭据或 Card 全文。"""

    interface_url: str
    tenant: str
    fingerprint: str


def validate_card(
    payload: object, *, transport: RemoteA2ATransport, development_mode: bool
) -> tuple[AgentCard, AgentCardSnapshot]:
    """选择官方 1.0 REST 接口，并验证文本、强制扩展与可满足认证。"""
    try:
        if not isinstance(payload, dict):
            raise ValueError
        card = ParseDict(payload, AgentCard())
        if not card.name.strip() or not card.version.strip():
            raise ValueError
        if (
            "text/plain" not in card.default_input_modes
            or "text/plain" not in card.default_output_modes
        ):
            raise ValueError
        if any(extension.required for extension in card.capabilities.extensions):
            raise ValueError
        source = validated_url(
            transport.agent_card_url, development_mode=development_mode
        )
        selected = None
        for interface in card.supported_interfaces:
            if (
                interface.protocol_binding == "HTTP+JSON"
                and interface.protocol_version == "1.0"
            ):
                target = validated_url(interface.url, development_mode=development_mode)
                if origin(target) != origin(source) or target.query:
                    raise ValueError
                if interface.tenant and not re.fullmatch(
                    r"[A-Za-z0-9_-]+", interface.tenant
                ):
                    raise ValueError
                if selected is None:
                    selected = (str(target).rstrip("/"), interface.tenant)
        if selected is None:
            raise ValueError
    except Exception:
        raise protocol_error() from None

    # securityRequirements 是 OR 列表，每项 schemes 是 AND；不能只满足其中一个名称。
    def supported_requirement(requirement):
        if not requirement.schemes:
            return True
        if transport.auth.type != "bearer":
            return False
        return all(
            name in card.security_schemes
            and card.security_schemes[name].WhichOneof("scheme")
            == "http_auth_security_scheme"
            and card.security_schemes[name].http_auth_security_scheme.scheme.lower()
            == "bearer"
            and not scopes.list
            for name, scopes in requirement.schemes.items()
        )

    if card.security_requirements and not any(
        supported_requirement(r) for r in card.security_requirements
    ):
        raise auth_error()
    canonical = MessageToDict(card)
    signature = {
        key: canonical.get(key)
        for key in (
            "name",
            "version",
            "provider",
            "supportedInterfaces",
            "defaultInputModes",
            "defaultOutputModes",
            "securitySchemes",
            "securityRequirements",
        )
    }
    fingerprint = hashlib.sha256(
        json.dumps({"source": str(source), "interface": selected[0], "tenant": selected[1], "identity": signature}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return card, AgentCardSnapshot(
        interface_url=selected[0], tenant=selected[1], fingerprint=fingerprint
    )
