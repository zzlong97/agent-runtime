"""Run 幂等请求的规范化指纹。"""

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from agent_runtime.runtime.models import JsonValue, RunType


def build_request_fingerprint(
    *,
    run_type: RunType,
    session_id: UUID | None,
    request_payload: Mapping[str, JsonValue],
) -> str:
    """对产品请求的规范化 JSON 计算不含明文的 SHA-256 指纹。"""

    canonical_request = json.dumps(
        {
            "run_type": run_type,
            "session_id": str(session_id) if session_id is not None else None,
            "request": dict(request_payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
