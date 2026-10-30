"""显式批准后启动普通真实Router的独立Runtime；导入本模块没有在线副作用。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

from agent_runtime.capabilities.source import LocalYamlCapabilitySource

ROOT = Path(__file__).parents[2] / "src/agent_runtime/capabilities"
CAPABILITIES = frozenset({"general_chat", "en_to_zh", "semantic_pattern_mining"})
BATCH = str(uuid5(NAMESPACE_URL, "agent-runtime/RA-INT-02/normal-router-v1"))
REQUEST_ID = str(uuid5(UUID(BATCH), "full-parameters"))
DATABASE = "agent_runtime_ra_int02_" + UUID(BATCH).hex[:8]
USER = "ra-int02-" + BATCH
BASE = "http://127.0.0.1:18782"


def prepare_manifest_directory(directory: Path) -> LocalYamlCapabilitySource:
    """只启用隔离目录中的副本，保留原两本地候选和正式配置原始字节。"""
    directory = directory.resolve()
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("联调目录必须与主源码目录隔离")
    documents = LocalYamlCapabilitySource(ROOT).load()
    if {item.raw_document["capability_id"] for item in documents} != CAPABILITIES:
        raise RuntimeError("正式能力清单变化，需重新核对联调范围")
    for item in documents:
        raw = yaml.safe_load(item.source.read_text(encoding="utf-8"))
        target = directory / raw["capability_id"] / "manifest.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        if raw["capability_id"] == "semantic_pattern_mining":
            raw["enabled"] = True
            target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        else:
            target.write_bytes(item.source.read_bytes())
    return LocalYamlCapabilitySource(directory)


@asynccontextmanager
async def open_real_service(settings, directory: Path):
    """仅替换测试目录来源；普通模型构造、Router、权限、Graph和执行链路沿用正式入口。"""
    from agent_runtime import chat

    if settings.runtime_demo_mode:
        raise ValueError("普通Router验收不能使用演示模式")
    with patch.object(chat, "LocalYamlCapabilitySource", lambda _root: LocalYamlCapabilitySource(directory)):
        async with chat.open_chat_service(settings) as service:
            yield service


async def serve():
    """新批次批准后才加载受控配置、初始化专用库、授权测试用户并启动回环服务。"""
    if os.getenv("RA_INT02_APPROVED") != BATCH or os.getenv("RA_INT02_ALLOW_TEST_SETUP") != "true":
        raise RuntimeError("启动前必须记录新批次批准，并显式允许专用测试库初始化及权限写入")

    import uvicorn
    from fastapi import HTTPException
    from agent_runtime.api.schemas.runs import RunSummaryResponse
    from agent_runtime.capabilities.persistence import UserCapabilityPermissionRepository
    from agent_runtime.capabilities.persistence_models import UserCapabilityPermission
    from agent_runtime.core.config import Settings
    from agent_runtime.main import create_app
    from agent_runtime.runtime.repository import RunNotFoundError

    base = Settings()
    address = urlsplit(str(base.database_url))
    if address.hostname not in {"localhost", "127.0.0.1"} or address.port not in {None, 5432}:
        raise RuntimeError("只允许已确认的本机Compose PostgreSQL；请核对配置，不自动换环境")
    if not all((base.dashscope_api_key, base.llm_base_url, base.llm_model)):
        raise RuntimeError("真实Router模型配置未齐备；不得回退固定选路模型")
    values = base.model_dump()
    values.update(database_url=urlunsplit(address._replace(path="/" + DATABASE)),
                  redis_url="redis://127.0.0.1:6379/13", local_user_id=USER,
                  runtime_demo_mode=False, remote_a2a_development_mode=True,
                  log_dir=Path("logs") / ("ra-int02-" + UUID(BATCH).hex[:8]))
    settings = Settings(_env_file=None, **values)
    # 仅给固定的专用测试用户写三条显式权限，不触碰主用户或主业务库。
    repository = UserCapabilityPermissionRepository(settings)
    await repository.setup()
    for capability_id in sorted(CAPABILITIES):
        now = datetime.now(UTC)
        await repository.set_allowed(UserCapabilityPermission(
            user_id=USER, capability_id=capability_id, allowed=True,
            created_at=now, updated_at=now,
        ))
    with TemporaryDirectory(prefix="ra-int02-manifests-") as temporary:
        directory = Path(temporary)
        prepare_manifest_directory(directory)
        async with open_real_service(settings, directory) as service:
            app = create_app(settings, chat_service=service)

            @app.get("/__ra_router__/health")
            async def health():
                projections = await service._remote_gateway._registry.serviceable_router_projections()
                return {"batch_id": BATCH, "database": DATABASE, "user_id": USER,
                        "router_mode": "production",
                        "capability_ids": sorted(p.capability_id for p in projections)}

            @app.get("/__ra_router__/requests/{request_id}")
            async def request_status(request_id: UUID):
                if str(request_id) != REQUEST_ID:
                    raise HTTPException(status_code=404, detail="仅允许查询本批固定请求")
                try:
                    run = await service._persistent_run_repository.get_by_request_id(request_id)
                except RunNotFoundError:
                    raise HTTPException(status_code=404, detail="本批请求尚无Run") from None
                return {"request_id": str(run.request_id),
                        **RunSummaryResponse.model_validate(run).model_dump(mode="json")}

            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18782,
                                               log_level="warning", access_log=False)).serve()


if __name__ == "__main__":
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(serve())
