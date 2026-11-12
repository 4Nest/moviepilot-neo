"""猪猪实验室搜索、资源标签、换票、用户资料与连通性的离线测试。"""

import base64
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.core.cache import MemoryBackend
from app.core.context import TorrentInfo
from app.modules.indexer import IndexerModule, SPIDER_PARSER_CLASSES
from app.modules.indexer.parser.piggo import PiggoSiteUserInfo
from app.modules.indexer.spider import piggo
from app.modules.indexer.spider.piggo import PiggoSpider
from app.schemas import MediaType

INDEXER = {
    "id": 7274,
    "name": "猪猪",
    "domain": "https://piggo.me/",
    "url": "https://piggo.me/",
    "parser": "Piggo",
    "schema": "Piggo",
    "apikey": "synthetic-key",
    "ua": "Piggo-Test",
    "timeout": 20,
}
CATALOG = {
    "sections": [{"id": 11, "name": "电影"}, {"id": 22, "name": "电视剧"}],
    "categories": [
        {"id": 101, "section_id": 11, "name": "海外电影"},
        {"id": 202, "section_id": 22, "name": "海外剧集"},
    ],
    "tags": [{"id": 1, "name": "全站标签"}],
}
ITEM = {
    "id": 56354,
    "name": "Example.Series.2026.S01.1080p.WEB-DL",
    "small_descr": "示例剧集",
    "section_id": 22,
    "category_ids": [202],
    "tag_ids": [4, 5],
    "size": 1024,
    "seeders": 8,
    "leechers": 2,
    "times_completed": 12,
    "added_at": "2026-11-11T08:00:00+08:00",
    "page_url": "https://piggo.me/detail/56354",
    "media": {"imdb": "tt33265527", "tmdb_kind": "tv"},
}
PROFILE = {
    "user": {
        "id": 42,
        "username": "synthetic-user",
        "enabled": True,
        "class": {"id": 8, "name": "主管"},
        "stats": {"uploaded": "2.50 GB", "downloaded": "1.00 GB", "bonus": 12.5},
    }
}


class FakeResponse:
    """提供同步与异步 HTTP 响应的离线最小契约。"""

    def __init__(self, data=None, status_code: int = 200, code=0):
        """保存固定业务数据、HTTP 状态和业务码。"""
        self.status_code = status_code
        self._payload = {"code": code, "data": data, "msg": "ok"}
        self.closed = False

    def __bool__(self) -> bool:
        """按 requests 响应语义表示 HTTP 是否成功。"""
        return self.status_code < 400

    def json(self):
        """返回预设的 JSON 数据。"""
        return self._payload

    def close(self) -> None:
        """记录同步响应已释放。"""
        self.closed = True

    async def aclose(self) -> None:
        """记录异步响应已释放。"""
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_catalog_cache(monkeypatch):
    """为每个用例隔离目录缓存，避免凭据与分区标签串用。"""
    monkeypatch.setattr(PiggoSpider, "_catalog_cache", MemoryBackend(maxsize=128, ttl=3600))
    monkeypatch.setattr(PiggoSpider, "_catalog_region", f"piggo-test-{id(monkeypatch)}")


def _torrent_page(items=None, kind=None):
    """构造搜索或用户种子列表的标准数据对象。"""
    data = {"items": list(ITEM for _ in range(0)) if items is None else items}
    if kind:
        data.update(kind=kind, page=1, per_page=100, pages=1, total=len(data["items"]))
    return data


def _install_sync_responses(monkeypatch, responses):
    """按顺序回放同步 API 响应并返回传输 mock。"""
    request = Mock()
    request.post_res.side_effect = responses
    monkeypatch.setattr(piggo, "RequestUtils", Mock(return_value=request))
    return request


def _install_async_responses(monkeypatch, responses):
    """按顺序回放异步 API 响应并返回传输 mock。"""
    request = Mock()
    request.post_res.side_effect = responses

    async def post_res(**kwargs):
        """异步返回下一项固定响应。"""
        return request.post_res(**kwargs)

    client = Mock()
    client.post_res = post_res
    monkeypatch.setattr(piggo, "AsyncRequestUtils", Mock(return_value=client))
    return request


def test_piggo_search_categories_tags_and_ticket(monkeypatch):
    """搜索使用正式 JSON 字段，按分区字典映射真实标签并生成换票地址。"""
    responses = [
        FakeResponse(CATALOG),
        FakeResponse(_torrent_page([ITEM])),
        FakeResponse({"tags": [{"id": 4, "name": "国语"}, {"id": 5, "name": "双语"}]}),
    ]
    request = _install_sync_responses(monkeypatch, responses)
    spider = PiggoSpider(INDEXER)

    failed, result = spider.search("Example", MediaType.TV, page=1)

    assert failed is False
    assert request.post_res.call_args_list[0].kwargs == {
        "url": "https://api.piggo.me/v1/api/catalog", "json": {}, "allow_redirects": False,
    }
    assert request.post_res.call_args_list[1].kwargs["json"] == {
        "page": 2, "per_page": 100, "q": "Example", "section_id": 22,
    }
    assert request.post_res.call_args_list[2].kwargs["json"] == {"section_id": 22}
    item = result[0]
    assert item["labels"] == ["国语", "双语"]
    assert item["category"] == MediaType.TV.value
    assert item["imdbid"] == "tt33265527"
    assert all(response.closed for response in responses)
    encoded, url = item["enclosure"].split("]", 1)
    recipe = json.loads(base64.b64decode(encoded[1:]))
    assert url == "https://api.piggo.me/v1/api/torrents/56354/download-link"
    assert recipe["header"]["Authorization"] == "Bearer synthetic-key"
    assert recipe["json"] == {} and recipe["allow_redirects"] is False
    assert "result_base_url" not in recipe


@pytest.mark.asyncio
async def test_piggo_async_search_matches_sync_protocol(monkeypatch):
    """异步搜索复用目录缓存、分页、分类和标签语义。"""
    responses = [
        FakeResponse(CATALOG), FakeResponse(_torrent_page([ITEM])),
        FakeResponse({"tags": [{"id": 4, "name": "国语"}, {"id": 5, "name": "双语"}]}),
    ]
    request = _install_async_responses(monkeypatch, responses)

    failed, result = await PiggoSpider(INDEXER).async_search("tt33265527")

    assert failed is False and result[0]["labels"] == ["国语", "双语"]
    assert request.post_res.call_args_list[1].kwargs["json"]["imdb_id"] == "tt33265527"
    assert all(response.closed for response in responses)


def test_piggo_catalog_cache_and_explicit_category(monkeypatch):
    """目录按凭据缓存，显式分类优先并去重，不消耗额外目录请求。"""
    request = _install_sync_responses(monkeypatch, [FakeResponse(CATALOG), FakeResponse(_torrent_page([]))])
    spider = PiggoSpider(INDEXER)

    assert spider.search(None, cat="202, 101,202") == (False, [])
    assert spider._load_catalog() == spider._load_catalog()
    assert request.post_res.call_count == 2
    assert request.post_res.call_args_list[1].kwargs["json"]["category_id"] == [202, 101]


def test_piggo_ticket_preserves_returned_download_host(monkeypatch, tmp_path: Path):
    """真实两段式下载流程不得把前端下载主机替换为 API 主机。"""
    enclosure = PiggoSpider(INDEXER).get_download_url(56354)
    torrent = TorrentInfo(
        site=7274, site_name="猪猪", title="Example", enclosure=enclosure,
        site_cookie="unused-cookie", site_ua="Piggo-Test", site_proxy=False,
    )
    ticket_url = "https://download.piggo.me/torrent/56354?ticket=signed"
    post_calls = []
    downloads = []

    def post_res(self, url, **_kwargs):
        """回放换票接口并记录认证头与 Cookie 隔离。"""
        post_calls.append((url, self._headers, self._cookies))
        return FakeResponse({"url": ticket_url})

    def download_torrent(self, url, **kwargs):
        """记录最终种子 URL，不执行真实下载。"""
        downloads.append((url, kwargs))
        return tmp_path / "t.torrent", b"d8:announce4:test4:infod4:name4:testee", "folder", ["a.mkv"], ""

    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    monkeypatch.setattr("app.chain.download.RequestUtils.post_res", post_res)
    monkeypatch.setattr("app.chain.download.TorrentHelper.download_torrent", download_torrent)
    content, folder, files = DownloadChain().download_torrent(torrent)

    assert content.startswith(b"d8:announce") and folder == "folder" and files == ["a.mkv"]
    assert post_calls[0][0].endswith("/torrents/56354/download-link")
    assert post_calls[0][1]["Authorization"] == "Bearer synthetic-key"
    assert post_calls[0][2] is None
    assert downloads[0][0] == ticket_url
    assert downloads[0][1]["cookie"] is None


def test_piggo_userdata_profile_and_all_torrent_pages():
    """用户资料、流量、做种与下载分页全部成功后才发布完整统计。"""
    request = Mock()
    seeding = {"kind": "seeding", "items": [{"id": 1, "size": 1024, "seeders": 4}],
               "page": 1, "per_page": 100, "pages": 1, "total": 1}
    leeching = {"kind": "leeching", "items": [{"id": 2, "size": 4096, "seeders": 2}],
                "page": 1, "per_page": 100, "pages": 1, "total": 1}
    responses = [FakeResponse(PROFILE), FakeResponse(seeding), FakeResponse(leeching)]
    request.post_res.side_effect = responses
    parser = PiggoSiteUserInfo(
        site_name="猪猪", url="https://piggo.me/", site_cookie="unused-cookie",
        apikey=" synthetic-key ", token=None, request_utils=request, ua="Piggo-Test",
    )

    parser.parse()

    assert parser.err_msg is None
    assert (parser.userid, parser.username, parser.user_level) == (42, "synthetic-user", "主管")
    assert (parser.upload, parser.download, parser.ratio, parser.bonus) == (2684354560, 1073741824, 2.5, 12.5)
    assert (parser.seeding, parser.seeding_size, parser.seeding_info) == (1, 1024, [[4, 1024]])
    assert (parser.leeching, parser.leeching_size) == (1, 4096)
    assert [call.kwargs["json"] for call in request.post_res.call_args_list] == [
        {}, {"kind": "seeding", "page": 1, "per_page": 100},
        {"kind": "leeching", "page": 1, "per_page": 100},
    ]
    assert all(response.closed for response in responses)


def test_piggo_unread_notice_and_missing_token_fail_without_pages():
    """必读公告与缺少长期令牌均明确失败，不回退旧网页或 Cookie。"""
    parser = PiggoSiteUserInfo(
        site_name="猪猪", url="https://piggo.me/", site_cookie="unused-cookie",
        apikey=None, token=None, request_utils=Mock(),
    )
    parser.parse()
    assert parser.userid is None and "长期存取令牌" in parser.err_msg
    parser._request_utils.post_res.assert_not_called()

    with pytest.raises(ValueError, match="网页端阅读"):
        piggo.response_data(403, {
            "code": "notice_unread",
            "data": {"code": "notice_unread", "notice_gate": {"title": "必读公告"}},
        })


def test_piggo_connection_test_and_registrations(monkeypatch):
    """适配器注册搜索、用户资料与 API 连通性检测能力。"""
    response = FakeResponse(PROFILE)
    request = _install_sync_responses(monkeypatch, [response])
    module = object.__new__(IndexerModule)

    assert SPIDER_PARSER_CLASSES["Piggo"] is PiggoSpider
    assert module.test_site(INDEXER) == (True, "连接成功")
    assert PiggoSpider.get_search_page_size() == 100
    request.post_res.assert_called_once_with(
        url="https://api.piggo.me/v1/api/me", json={}, allow_redirects=False,
    )
    assert response.closed
