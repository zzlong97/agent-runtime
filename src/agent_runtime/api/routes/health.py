"""进程健康检查接口。"""

from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """稳定的进程存活响应。"""

    status: Literal["ok"] = Field(
        description=(
            "当前 HTTP 进程的存活状态；Stage 3 仅返回 ok，不代表 PostgreSQL、Redis "
            "或各 Capability 的健康状态。"
        )
    )


@router.get("/health", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health() -> HealthResponse:
    """返回不依赖外部服务的进程存活状态。"""

    return HealthResponse(status="ok")
