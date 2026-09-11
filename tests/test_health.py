import asyncio

import httpx


def test_application_metadata() -> None:
    from agent_runtime.main import app

    assert app.title == "AgentRuntime"
    assert app.version == "0.1.0"


def test_health_returns_200_without_external_services() -> None:
    from agent_runtime.main import app

    async def request_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
