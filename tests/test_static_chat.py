"""S2-09 聊天演示页静态资源边界测试。"""

import asyncio
import re

import httpx


def _get(app, path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_chat_page_and_hashed_assets_are_served_by_fastapi() -> None:
    from agent_runtime.main import create_app

    app = create_app(chat_service=object())

    page = _get(app, "/chat")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in page.text

    asset_paths = re.findall(r'["\'](/assets/[^"\']+)["\']', page.text)
    javascript_paths = [path for path in asset_paths if path.endswith(".js")]
    stylesheet_paths = [path for path in asset_paths if path.endswith(".css")]

    assert javascript_paths
    assert stylesheet_paths
    for path in javascript_paths + stylesheet_paths:
        assert re.search(r"-[A-Za-z0-9_-]{8,}\.(?:js|css)$", path)
        response = _get(app, path)
        assert response.status_code == 200
        assert "langgraph" not in response.text.lower()
        assert "statesnapshot" not in response.text.lower()
        assert "checkpoint_metadata" not in response.text.lower()


def test_chat_page_unknown_asset_returns_404() -> None:
    from agent_runtime.main import create_app

    app = create_app(chat_service=object())

    response = _get(app, "/assets/not-found.js")

    assert response.status_code == 404
