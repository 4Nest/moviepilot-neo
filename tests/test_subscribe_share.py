"""订阅分享取消及列表刷新回归测试。"""

import asyncio
import warnings
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.cache import MemoryBackend
from app.core.config import settings
from app.helper.server import MoviePilotServerHelper


@pytest.fixture(autouse=True)
def isolate_share_state(monkeypatch):
    """隔离共享缓存、开关和安装身份，外部请求由各用例拦截。"""
    monkeypatch.setattr(MemoryBackend, "_region_caches", {})
    monkeypatch.setattr(settings, "SUBSCRIBE_STATISTIC_SHARE", True)
    monkeypatch.setattr(MoviePilotServerHelper, "get_user_uid", lambda: "installed-owner")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_cancel_share_uses_owner_identity_even_after_sharing_disabled(monkeypatch, asynchronous, enabled):
    """关闭后续数据共享不应阻止撤回已有分享，仍由服务端校验当前安装身份。"""
    monkeypatch.setattr(settings, "SUBSCRIBE_STATISTIC_SHARE", enabled)
    response = Mock(status_code=200)
    response.json.return_value = {"success": True}
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    method = "_async_delete" if asynchronous else "_delete"
    monkeypatch.setattr(MoviePilotServerHelper, method, request)

    with warnings.catch_warnings(record=True):
        if asynchronous:
            result = asyncio.run(MoviePilotServerHelper.async_share_delete(6201))
        else:
            result = MoviePilotServerHelper.share_delete(6201)

    assert result == (True, "")
    assert request.call_args.args[0].endswith("/subscribe/share/6201")
    assert request.call_args.kwargs["params"] == {"share_uid": "installed-owner"}


@pytest.mark.parametrize("status, payload, message", [
    (200, {"success": False, "message": "没有删除权限"}, "没有删除权限"),
    (200, {"success": False, "detail": "分享不属于当前用户"}, "分享不属于当前用户"),
    (200, {"success": False}, "操作失败"),
    (403, {"detail": "分享不属于当前用户"}, "分享不属于当前用户"),
])
def test_cancel_share_preserves_business_failure_in_api_response(monkeypatch, status, payload, message):
    """HTTP 200 内的业务失败不能变成取消成功，错误原因应原样返回前端。"""
    from app.api.endpoints.subscribe import subscribe_share_delete

    response = Mock(status_code=status)
    response.json.return_value = payload
    monkeypatch.setattr(MoviePilotServerHelper, "_async_delete", AsyncMock(return_value=response))
    clear_cache = Mock()
    monkeypatch.setattr(MoviePilotServerHelper, "_clear_subscribe_share_cache", clear_cache)

    result = asyncio.run(subscribe_share_delete(6201, _=None))

    assert result.success is False
    assert result.message == message
    clear_cache.assert_not_called()


def test_cancel_share_refreshes_cached_list_without_unawaited_coroutines(monkeypatch):
    """取消成功后刷新列表必须重新查询服务端，且不能留下未执行的清缓存协程。"""
    before = Mock(status_code=200)
    before.json.return_value = [{"id": 6201, "share_title": "已取消的分享"}]
    after = Mock(status_code=200)
    after.json.return_value = []
    deleted = Mock(status_code=200)
    deleted.json.return_value = {"success": True}
    query = AsyncMock(side_effect=[before, after])
    monkeypatch.setattr(MoviePilotServerHelper, "async_subscribe_shares", query)
    monkeypatch.setattr(MoviePilotServerHelper, "_async_delete", AsyncMock(return_value=deleted))

    async def cancel_and_refresh():
        """复放先读列表、取消分享、刷新页面的完整顺序。"""
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == before.json()
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == before.json()
        assert query.await_count == 1
        assert await MoviePilotServerHelper.async_share_delete(6201) == (True, "")
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        asyncio.run(cancel_and_refresh())

    assert query.await_count == 2
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.parametrize("payload", [{"success": True}, {"message": "删除成功"}, None])
def test_server_response_preserves_legacy_success_contract(payload):
    """保留旧服务端未返回 success 字段或仅返回文本的成功响应。"""
    response = Mock(status_code=200)
    if payload is None:
        response.json.side_effect = ValueError("旧版纯文本响应")
    else:
        response.json.return_value = payload
    clear_cache = Mock()

    assert MoviePilotServerHelper._handle_response(response, clear_cache) == (True, "")
    clear_cache.assert_called_once_with()
