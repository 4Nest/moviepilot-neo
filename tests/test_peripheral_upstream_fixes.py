"""验证 115、Jellyfin、入库查询、TMDB 与 Telegram 的上游缺陷修复。"""

from types import SimpleNamespace
from threading import Lock
from unittest.mock import AsyncMock, Mock

import pytest
import requests
from telebot import apihelper

from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.modules.filemanager.storages import u115 as u115_module
from app.modules.filemanager.storages.u115 import U115Pan
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.telegram.telegram import Telegram
from app.schemas.tmdb import TmdbEpisode


def _build_u115() -> U115Pan:
    """构造不访问真实网络的 115 存储实例。"""
    storage = object.__new__(U115Pan)
    storage._auth_state = {"access_token": "offline-test"}
    storage.get_conf = lambda: {"refresh_token": "offline-test", "access_token": "offline-test"}
    storage._download_limiter = Mock()
    storage._api_limiter = Mock()
    storage._limit_until = 0.0
    storage._limit_lock = Lock()
    storage._rate_stats = Mock()
    storage._usage_cache = None
    storage._usage_cache_until = 0.0
    storage.session = Mock()
    return storage


def test_u115_cooldown_skips_request_without_sleep(monkeypatch) -> None:
    """冷却期内应记录剩余秒数并立即返回，不能休眠或调用 115 接口。"""
    storage = _build_u115()
    storage._limit_until = 1360.0
    monkeypatch.setattr(u115_module.time, "time", lambda: 1000.0)
    sleep = Mock()
    warning = Mock()
    monkeypatch.setattr(u115_module.time, "sleep", sleep)
    monkeypatch.setattr(u115_module.logger, "warning", warning)
    assert storage._request_api("GET", "/open/user/info") is None
    storage.session.request.assert_not_called()
    sleep.assert_not_called()
    assert "剩余 360 秒" in warning.call_args.args[0]


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(status_code=429),
        SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"code": 1000, "message": "已达到当前访问上限"},
        ),
    ],
)
def test_u115_rate_limit_starts_cooldown_without_retry_or_sleep(monkeypatch, response) -> None:
    """HTTP 429 和访问上限应记录冷却截止时间并直接失败，不再递归重试。"""
    storage = _build_u115()
    storage.session.request.return_value = response
    monkeypatch.setattr(u115_module.time, "time", lambda: 1000.0)
    sleep = Mock()
    monkeypatch.setattr(u115_module.time, "sleep", sleep)
    assert storage._request_api("GET", "/open/user/info", retry_limit=0) is None
    assert storage._limit_until == 4600.0
    storage.session.request.assert_called_once()
    sleep.assert_not_called()
    assert storage._request_api("GET", "/open/user/info") is None
    storage.session.request.assert_called_once()


def test_u115_usage_uses_short_cache_and_cooldown_fallback(monkeypatch) -> None:
    """容量查询应缓存 300 秒，缓存过期后的冷却期仍返回最后已知容量。"""
    storage = _build_u115()
    now = Mock(side_effect=[1000.0, 1001.0, 1302.0])
    monkeypatch.setattr(u115_module.time, "time", now)
    request = Mock(return_value={
        "rt_space_info": {"all_total": {"size": 100}, "all_remain": {"size": 40}},
    })
    monkeypatch.setattr(storage, "_request_api", request)
    first = storage.usage()
    assert (first.total, first.available) == (100, 40)
    assert storage.usage() is first
    assert request.call_count == 1
    storage._limit_until = 2000.0
    assert storage.usage() is first
    assert request.call_count == 1


def _build_jellyfin(monkeypatch, views: list) -> list:
    """用预设视图调用媒体库转换，禁止真实数量和图片请求。"""
    client = object.__new__(Jellyfin)
    client._host = "http://jellyfin.local:8096/"
    client._playhost = None
    client._apikey = "offline-test"
    client._sync_libraries = []
    monkeypatch.setattr(client, "_Jellyfin__get_jellyfin_librarys", lambda _username=None: views)
    monkeypatch.setattr(client, "_Jellyfin__get_local_image_by_id", lambda _item_id: None)
    monkeypatch.setattr(client, "get_items_count", lambda _item_id: 0)
    return client.get_librarys()


def test_jellyfin_mixed_library_uses_mixed_route(monkeypatch) -> None:
    """无 CollectionType 的 CollectionFolder 应使用 Jellyfin mixed 路由。"""
    libraries = _build_jellyfin(monkeypatch, [
        {"Id": "abc", "Name": "混合", "CollectionType": None, "Type": "CollectionFolder"},
    ])
    assert libraries[0].link == (
        "http://jellyfin.local:8096/web/index.html#!/mixed?topParentId=abc&collectionType=mixed"
    )


def test_jellyfin_other_unknown_view_keeps_library_route(monkeypatch) -> None:
    """非 CollectionFolder 的未知视图不应被误判为混合内容库。"""
    libraries = _build_jellyfin(monkeypatch, [
        {"Id": "u1", "Name": "视图", "CollectionType": None, "Type": "UserView"},
    ])
    assert libraries[0].link == "http://jellyfin.local:8096/web/index.html#!/library.html?topParentId=u1"


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["Emby 4K", None])
async def test_exists_response_returns_server_from_same_item(monkeypatch, server) -> None:
    """入库查询应返回命中行的服务器；旧缓存缺失服务器时省略该键。"""
    item = SimpleNamespace(item_id="100", server=server)
    oper = SimpleNamespace(async_exists=AsyncMock(return_value=item))
    monkeypatch.setattr(mediaserver_endpoint, "MediaServerOper", lambda _db: oper)
    response = await mediaserver_endpoint.exists_local(title="示例", tmdbid=1001, db=Mock())
    expected = {"id": "100"}
    if server:
        expected["server"] = server
    assert response.success is True
    assert response.data == {"item": expected}


def test_tmdb_episode_preserves_optional_id() -> None:
    """季集模型应保留 TMDB 单集标识，并兼容缺失标识的旧响应。"""
    assert TmdbEpisode(id=2001, episode_number=3).id == 2001
    assert TmdbEpisode(episode_number=3).id is None


def _build_telegram() -> Telegram:
    """构造仅替换 Bot API 的 Telegram 客户端。"""
    client = object.__new__(Telegram)
    client._bot = Mock()
    return client


def _sleep_mock(monkeypatch) -> Mock:
    """替换重试等待，避免测试真实等待。"""
    sleep = Mock()
    monkeypatch.setattr("app.utils.common.time.sleep", sleep)
    return sleep


def test_telegram_photo_retries_only_transient_network_errors(monkeypatch) -> None:
    """连接异常最多上传三次，耗尽后沿用现有纯文本回退。"""
    client = _build_telegram()
    sleep = _sleep_mock(monkeypatch)
    client._bot.send_photo.side_effect = requests.exceptions.ConnectionError("offline")
    client._bot.send_message.return_value = {"message_id": 18}
    result = client._Telegram__send_short_message(b"image", "正文", parse_mode="")
    assert result == {"message_id": 18}
    assert client._bot.send_photo.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [3, 6]
    client._bot.send_message.assert_called_once()


def test_telegram_photo_recovers_on_later_network_attempt(monkeypatch) -> None:
    """瞬态网络错误后的成功上传不应再发送重复文本。"""
    client = _build_telegram()
    sleep = _sleep_mock(monkeypatch)
    client._bot.send_photo.side_effect = [
        requests.exceptions.ReadTimeout("timeout"),
        {"message_id": 19},
    ]
    result = client._Telegram__send_short_message(b"image", "正文", parse_mode="")
    assert result == {"message_id": 19}
    assert client._bot.send_photo.call_count == 2
    sleep.assert_called_once_with(3)
    client._bot.send_message.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        apihelper.ApiException("invalid photo", "sendPhoto", SimpleNamespace(status_code=400)),
        apihelper.ApiHTTPException(
            "sendPhoto",
            SimpleNamespace(status_code=429, reason="Too Many Requests", text='{"retry_after":17}'),
        ),
        requests.exceptions.HTTPError(
            "429 Too Many Requests", response=SimpleNamespace(status_code=429),
        ),
    ],
)
def test_telegram_photo_rejection_and_rate_limit_fall_back_without_retry(monkeypatch, error) -> None:
    """明确参数错误和 429 只上传一次并立即回退文本，不能盲目重试加剧限流。"""
    client = _build_telegram()
    sleep = _sleep_mock(monkeypatch)
    client._bot.send_photo.side_effect = error
    client._bot.send_message.return_value = {"message_id": 20}
    result = client._Telegram__send_short_message(b"image", "正文", parse_mode="")
    assert result == {"message_id": 20}
    client._bot.send_photo.assert_called_once()
    sleep.assert_not_called()
    client._bot.send_message.assert_called_once()
