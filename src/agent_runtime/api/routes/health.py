"""Service health endpoint."""

from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Stable health response payload."""

    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health() -> HealthResponse:
    """Report process liveness without requiring external services."""

    return HealthResponse(status="ok")
