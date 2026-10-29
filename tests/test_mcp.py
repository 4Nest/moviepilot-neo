import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints import mcp as mcp_endpoint
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.schemas.transfer import DownloaderTorrent

pytestmark = pytest.mark.anyio

TEST_API_KEY = "mcp-test-api-key"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def mcp_app(monkeypatch):
    monkeypatch.setattr(settings, "API_TOKEN", TEST_API_KEY)

    app = FastAPI()
    app.include_router(mcp_endpoint.router, prefix="/mcp")
    return app


@pytest.fixture()
def auth_headers():
    return {"X-API-KEY": TEST_API_KEY}


def make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def make_rpc(method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        body["params"] = params
    return body


class _FakeColumn:
    def __init__(self, name):
        self.name = name


class _FakeSubscribe:
    """模拟订阅 ORM 实例，提供 _model_to_dict 所需的最小结构"""

    __table__ = SimpleNamespace(columns=[_FakeColumn("id"), _FakeColumn("name"), _FakeColumn("state")])

    def __init__(self, sid=1, name="测试剧集", state="R"):
        self.id = sid
        self.name = name
        self.state = state


async def test_initialize_negotiates_supported_version(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "test-client"},
            }),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "MoviePilot"
    assert result["capabilities"]["tools"]["listChanged"] is False


async def test_initialize_falls_back_to_default_version(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("initialize", {"protocolVersion": "1900-01-01"}),
        )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == mcp_endpoint.MCP_PROTOCOL_VERSION


async def test_initialized_notification_returns_204(mcp_app, auth_headers):
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    async with make_client(mcp_app) as client:
        response = await client.post("/mcp", headers=auth_headers, json=body)
    assert response.status_code == 204


async def test_tools_list_returns_registered_tools(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp", headers=auth_headers, json=make_rpc("tools/list")
        )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {tool.name for tool in mcp_endpoint.MCP_TOOLS}
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


async def test_tools_call_subscribe_list(mcp_app, auth_headers, monkeypatch):
    monkeypatch.setattr(
        SubscribeOper, "list",
        lambda self, state=None: [_FakeSubscribe(sid=7, state=state or "R")],
    )
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {"name": "subscribe.list", "arguments": {"state": "P"}}),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert "isError" not in result
    payload = json.loads(result["content"][0]["text"])
    assert payload == [{"id": 7, "name": "测试剧集", "state": "P"}]


async def test_tools_call_subscribe_add(mcp_app, auth_headers, monkeypatch):
    captured = {}

    async def fake_async_add(self, **kwargs):
        captured.update(kwargs)
        return 42, "添加订阅成功"

    monkeypatch.setattr(SubscribeChain, "async_add", fake_async_add)
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {
                "name": "subscribe.add",
                "arguments": {"title": "庆余年", "season": 2, "tmdbid": 106379},
            }),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"success": True, "id": 42, "message": "添加订阅成功"}
    assert captured["title"] == "庆余年"
    assert captured["season"] == 2
    assert captured["tmdbid"] == 106379
    assert captured["exist_ok"] is True
    assert captured["username"] == settings.SUPERUSER


async def test_tools_call_subscribe_delete_sends_event(mcp_app, auth_headers, monkeypatch):
    events = []
    monkeypatch.setattr(SubscribeOper, "get", lambda self, sid: _FakeSubscribe(sid=sid))
    monkeypatch.setattr(SubscribeOper, "delete", lambda self, sid: None)

    async def fake_send_event(etype, data):
        events.append((etype, data))

    monkeypatch.setattr(eventmanager, "async_send_event", fake_send_event)
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {"name": "subscribe.delete", "arguments": {"id": 9}}),
        )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload == {"success": True, "id": 9}
    assert len(events) == 1
    assert events[0][1]["subscribe_id"] == 9


async def test_tools_call_download_list(mcp_app, auth_headers, monkeypatch):
    monkeypatch.setattr(
        DownloadChain, "downloading",
        lambda self, name=None: [DownloaderTorrent(hash="abc", title="测试种子", progress=50.0)],
    )
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {"name": "download.list", "arguments": {}}),
        )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload[0]["hash"] == "abc"
    assert payload[0]["progress"] == 50.0


async def test_tools_call_download_control_validates_action(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {
                "name": "download.control",
                "arguments": {"hash": "abc", "action": "explode"},
            }),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


async def test_tools_call_unknown_tool_returns_invalid_params(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {"name": "no.such.tool", "arguments": {}}),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


async def test_tools_call_handler_error_returns_is_error(mcp_app, auth_headers, monkeypatch):
    def boom(self, name=None):
        raise RuntimeError("下载器连接失败")

    monkeypatch.setattr(DownloadChain, "downloading", boom)
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp",
            headers=auth_headers,
            json=make_rpc("tools/call", {"name": "download.list", "arguments": {}}),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "下载器连接失败" in result["content"][0]["text"]


async def test_unknown_method_returns_method_not_found(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp", headers=auth_headers, json=make_rpc("resources/list")
        )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32601


async def test_invalid_jsonrpc_envelope_rejected(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp", headers=auth_headers, json={"method": "tools/list"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


async def test_mcp_requires_api_key(mcp_app):
    async with make_client(mcp_app) as client:
        response = await client.post("/mcp", json=make_rpc("ping"))
    assert response.status_code == 401


async def test_ping(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post("/mcp", headers=auth_headers, json=make_rpc("ping"))
    assert response.status_code == 200
    assert response.json()["result"] == {}


async def test_rest_tools_and_call(mcp_app, auth_headers, monkeypatch):
    monkeypatch.setattr(SubscribeOper, "list", lambda self, state=None: [])
    async with make_client(mcp_app) as client:
        tools_response = await client.get("/mcp/tools", headers=auth_headers)
        assert tools_response.status_code == 200
        assert any(tool["name"] == "subscribe.list" for tool in tools_response.json())

        call_response = await client.post(
            "/mcp/tools/call",
            headers=auth_headers,
            json={"tool_name": "subscribe.list", "arguments": {}},
        )
    assert call_response.status_code == 200
    body = call_response.json()
    assert body["success"] is True
    assert json.loads(body["data"]["result"]) == []


async def test_rest_call_unknown_tool_fails_gracefully(mcp_app, auth_headers):
    async with make_client(mcp_app) as client:
        response = await client.post(
            "/mcp/tools/call",
            headers=auth_headers,
            json={"tool_name": "no.such.tool", "arguments": {}},
        )
    assert response.status_code == 200
    assert response.json()["success"] is False
