import base64
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _fake_gemini(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    cfg = body["generationConfig"]
    assert cfg["responseModalities"] == ["TEXT", "IMAGE"]
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"text": "here you go"},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}},
    ]}}], "_echo": body})


@pytest.fixture(scope="module")
def server():
    # FastMCP's session manager can only be started once per process, so one server per module.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from app import gemini, server as srv

    transport = httpx.MockTransport(_fake_gemini)
    monkeypatch.setattr(gemini, "_new_client", lambda: httpx.AsyncClient(transport=transport))
    srv._api_key.cache_clear()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv.mcp.settings.host, srv.mcp.settings.port = "127.0.0.1", port
    app = srv.mcp.streamable_http_app()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=uv.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not uv.started:
        assert time.time() < deadline, "server failed to start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    uv.should_exit = True
    t.join(5)
    monkeypatch.undo()


async def _call(url, tool, args):
    async with streamablehttp_client(f"{url}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            return tools, await s.call_tool(tool, args)


@pytest.mark.asyncio
async def test_generate_image_returns_image(server):
    tools, res = await _call(server, "generate_image", {"prompt": "a banana", "aspect_ratio": "16:9"})
    assert tools == {"generate_image", "edit_image"}
    assert not res.isError
    img = next(c for c in res.content if c.type == "image")
    assert base64.b64decode(img.data) == PNG
    assert img.mimeType == "image/png"


@pytest.mark.asyncio
async def test_edit_image_rejects_bad_base64(server):
    _, res = await _call(server, "edit_image", {"prompt": "x", "images_base64": ["!!notbase64!!"]})
    assert res.isError


@pytest.mark.asyncio
async def test_edit_image_ok(server):
    _, res = await _call(server, "edit_image", {"prompt": "blue", "images_base64": [base64.b64encode(PNG).decode()]})
    assert not res.isError


@pytest.mark.asyncio
async def test_url_delivery_without_bucket_errors(server):
    _, res = await _call(server, "generate_image", {"prompt": "x", "delivery": "url"})
    assert res.isError


@pytest.mark.asyncio
async def test_invalid_aspect_ratio_rejected(server):
    _, res = await _call(server, "generate_image", {"prompt": "x", "aspect_ratio": "7:3"})
    assert res.isError
