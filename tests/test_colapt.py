"""Cola 目录、API 搜索、账户与两段式下载的离线契约测试。"""

import asyncio
import base64
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app import schemas
from app.api.endpoints import site as site_endpoint
from app.core.context import TorrentInfo
from app.db.models.site import Site
from app.helper import site as catalog
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.mtorrent import MTorrentSiteUserInfo
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.schemas import MediaType


class FakeResponse:
    """仅回放响应正文，禁止依赖真实站点。"""

    status_code = 200
    reason = "OK"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        """返回独立响应，避免用例互相污染。"""
        return deepcopy(self.payload)


@pytest.fixture
def directory(monkeypatch):
    """替换资源与存储边界，保留目录合并的真实实现。"""
    resource = Mock(auth_level=2, indexer_version="2.4.8")
    resource.get_indexsites.return_value = {}
    resource.get_indexer.return_value = {}
    resource.get_indexers.return_value = []
    resource.async_get_indexer = AsyncMock(return_value={})
    resource.async_get_indexers = AsyncMock(return_value=[])
    resource.check.return_value = (False, "")
    saved = Site(id=42, name="Cola", domain="colapt.com", url="https://www.colapt.com/",
                 apikey="test-key", cookie="unused-cookie", token="unused-token", is_active=True, pri=2)
    oper = Mock()
    oper.get_by_domain.return_value = saved
    oper.async_get_by_domain = AsyncMock(return_value=saved)
    oper.list_active.return_value = [saved]
    oper.async_list_active = AsyncMock(return_value=[saved])
    monkeypatch.setattr(catalog, "ResourceSitesHelper", lambda: resource)
    monkeypatch.setattr(catalog, "SiteOper", lambda: oper)
    return catalog.SitesHelper(), resource, oper, saved


@pytest.fixture
def search_payload() -> dict:
    """回放 2026-10-16 接口样本，只包含资源字段，不含账号或下载票。"""
    return json.loads((Path(__file__).parent / "fixtures/colapt/search.json").read_text())


def _indexer() -> dict:
    return {**catalog.SITE_SUPPLEMENTS["colapt.com"], "id": 42, "apikey": "test-key", "ua": "MoviePilot-Test"}


def test_colapt_directory_merges_configuration_and_preserves_auth(directory):
    """新增、搜索、异步浏览均能取得补充站点，但不能绕过认证。"""
    helper, resource, oper, saved = directory
    assert helper.get_indexsites()["colapt.com"]["public"] is False
    entry = helper.get_indexer("https://www.colapt.com/account/tracker")
    assert entry["parser"] == "mTorrent"
    assert entry["schema"] == "MTorrent"
    assert entry["domain"] == saved.url
    assert entry["id"] == 42
    assert entry["apikey"] == "test-key"
    assert asyncio.run(helper.async_get_indexer("colapt.com")) == entry
    assert helper.get_indexers() == [entry]
    assert asyncio.run(helper.async_get_indexers()) == [entry]
    assert helper.check("colapt.com") == (False, "")
    resource.check.assert_called_once_with("colapt.com")
    resource.auth_level = 0
    assert helper.get_indexer("colapt.com") == {}
    assert asyncio.run(helper.async_get_indexer("colapt.com")) == {}
    assert helper.get_indexers() == []
    assert asyncio.run(helper.async_get_indexers()) == []
    assert "colapt.com" in helper.get_indexsites()
    oper.get_by_domain.assert_called_once_with("colapt.com")


def test_colapt_directory_respects_disabled_deleted_and_updated_sites(directory):
    """停用与删除立即影响搜索，更新 Key 立即生效，未知域名不被接受。"""
    helper, _resource, oper, saved = directory
    saved.is_active = False
    assert helper.get_indexers() == []
    assert asyncio.run(helper.async_get_indexers()) == []
    saved.apikey = "updated-key"
    assert helper.get_indexer("colapt.com")["apikey"] == "updated-key"
    oper.get_by_domain.return_value = None
    oper.async_get_by_domain.return_value = None
    oper.list_active.return_value = []
    oper.async_list_active.return_value = []
    assert helper.get_indexer("colapt.com")["id"] == "colapt"
    assert asyncio.run(helper.async_get_indexer("colapt.com"))["id"] == "colapt"
    assert helper.get_indexers() == []
    assert asyncio.run(helper.async_get_indexers()) == []
    assert helper.get_indexer("https://colapt.com.invalid/") == {}


def test_colapt_directory_prefers_future_upstream_entry_without_duplicates(directory):
    """上游日后收录后优先使用资源配置，不生成重复站点。"""
    helper, resource, _oper, _saved = directory
    entry = {**_indexer(), "name": "Upstream Cola", "pri": 1}
    resource.get_indexsites.return_value = {"colapt.com": {"name": "Upstream Cola"}}
    resource.get_indexer.return_value = entry
    resource.async_get_indexer.return_value = entry
    resource.get_indexers.return_value = [entry]
    resource.async_get_indexers.return_value = [entry]
    assert helper.get_indexsites()["colapt.com"]["name"] == "Upstream Cola"
    assert helper.get_indexer("colapt.com") == entry
    assert asyncio.run(helper.async_get_indexer("colapt.com")) == entry
    assert helper.get_indexers() == [entry]
    assert asyncio.run(helper.async_get_indexers()) == [entry]


def test_colapt_can_be_added_through_site_endpoint(monkeypatch, directory):
    """新增站点入口使用补充目录，正确保存 API Key 和私有站点属性。"""
    helper, resource, oper, _saved = directory
    oper.async_get_by_domain.return_value = None
    monkeypatch.setattr(site_endpoint, "SitesHelper", lambda: helper)
    monkeypatch.setattr(site_endpoint.Site, "async_get_by_domain", AsyncMock(return_value=None))
    saved = {}
    monkeypatch.setattr(site_endpoint.Site, "create", lambda self, db: saved.update(self.to_dict()))
    monkeypatch.setattr(site_endpoint.eventmanager, "async_send_event", AsyncMock())
    form = schemas.Site(url="https://www.colapt.com/", apikey="test-key")
    result = asyncio.run(site_endpoint.add_site(db=Mock(), site_in=form, _=Mock()))
    assert result.success is True
    assert saved["name"] == "Cola"
    assert saved["domain"] == "colapt.com"
    assert saved["apikey"] == "test-key"
    assert saved["public"] == 0
    resource.auth_level = 0
    result = asyncio.run(site_endpoint.add_site(db=Mock(), site_in=form, _=Mock()))
    assert result.success is False
    assert "未通过认证" in result.message


@pytest.mark.parametrize("asynchronous", [False, True])
def test_colapt_search_maps_real_response(monkeypatch, search_payload, asynchronous):
    """同步和异步搜索均正确映射实际整数成功码与资源字段。"""
    captured = {}

    def respond(request, url, **kwargs):
        captured.update(url=url, headers=request._headers, payload=kwargs["json"])
        return FakeResponse(search_payload)

    async def async_respond(request, url, **kwargs):
        return respond(request, url, **kwargs)

    monkeypatch.setattr("app.modules.indexer.spider.mtorrent.RequestUtils.post_res", respond)
    monkeypatch.setattr("app.modules.indexer.spider.mtorrent.AsyncRequestUtils.post_res", async_respond)
    spider = MTorrentSpider(_indexer())
    error, rows = asyncio.run(spider.async_search("玉簟秋", MediaType.TV, 1)) if asynchronous \
        else spider.search("玉簟秋", MediaType.TV, 1)
    assert error is False
    assert len(rows) == 3
    assert rows[1]["description"] == "玉簟秋 (2026)"
    assert rows[1]["size"] == 3540459387
    assert rows[1]["peers"] == 3
    assert rows[1]["imdbid"] == "tt37109705"
    assert rows[1]["downloadvolumefactor"] == 0
    assert rows[1]["category"] == "电视剧"
    assert rows[1]["page_url"] == "https://www.colapt.com/detail/632"
    assert captured["url"] == "https://api.colapt.com/api/torrent/search"
    assert captured["payload"]["pageNumber"] == 2
    assert captured["payload"]["keyword"] == "玉簟秋"
    assert captured["payload"]["categories"] == spider._tv_category
    assert captured["headers"]["x-api-key"] == "test-key"
    assert not {"cookie", "authorization", "origin"}.intersection(k.lower() for k in captured["headers"])


@pytest.mark.parametrize("payload,expected_error", [
    ({"code": 0, "data": {"data": []}}, False),
    ({"code": "0", "data": {"data": []}}, False),
    ({"code": 403, "data": {"data": []}}, True),
    ({"code": 0, "data": None}, True),
    ({"code": 0, "data": {"data": {}}}, True),
    ([], True),
])
def test_mtorrent_rejects_failed_or_malformed_search(monkeypatch, payload, expected_error):
    """业务失败与无结果区分，不能将失效 Key 显示成正常空结果。"""
    monkeypatch.setattr("app.modules.indexer.spider.mtorrent.RequestUtils.post_res",
                        lambda *args, **kwargs: FakeResponse(payload))
    assert MTorrentSpider(_indexer()).search("") == (expected_error, [])


@pytest.mark.parametrize("code", [0, "0"])
def test_mtorrent_profile_seeding_and_messages_accept_both_success_codes(monkeypatch, code):
    """Cola 整数成功码与馒头字符串成功码使用同一账户解析口径。"""
    parser = MTorrentSiteUserInfo(site_name="Cola", url="https://www.colapt.com/",
                                 site_cookie="unused-cookie", apikey="test-key", token=None)
    parser._parse_site_page("")
    parser._parse_user_base_info(json.dumps({"code": code, "data": {
        "id": 7, "username": "fixture-user", "role": "1",
        "memberStatus": {"createdDate": "2026-10-01 00:00:00"},
        "memberCount": {"uploaded": "1024", "downloaded": "512", "shareRate": "2", "bonus": "6"},
    }}))
    assert parser.username == "fixture-user"
    assert parser.upload == 1024
    assert parser.download == 512
    assert parser.user_level == "User"
    monkeypatch.setattr(parser, "_get_page_content", lambda **kwargs: json.dumps({"data": {"seeder": 1}}))
    parser._parse_user_torrent_seeding_info(json.dumps({"code": code, "data": {
        "data": [{"torrent": {"size": "4096", "source": "3"}}],
    }}))
    assert parser.seeding == 1
    assert parser.seeding_size == 4096
    parser._parse_message_unread_links(json.dumps({"code": code, "data": {"data": [{
        "id": 1, "title": "站内通知", "createdDate": "2026-10-01", "context": "测试内容", "unread": True,
    }]}}), [])
    assert parser.message_unread_contents == [("站内通知", "2026-10-01", "测试内容")]


@pytest.mark.parametrize("code,expected", [(0, True), ("0", True), (403, False)])
def test_colapt_connection_uses_api_key_not_html(monkeypatch, code, expected):
    """从实际分派入口测试 Cola，避免首页无登录标记导致 Cookie 失效误报。"""
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    captured = {}

    def respond(request, url, **kwargs):
        captured.update(url=url, headers=request._headers)
        return FakeResponse({"code": code, "data": {"id": 7}, "message": "invalid"})

    monkeypatch.setattr("app.chain.site.RequestUtils.post_res", respond)
    site = SimpleNamespace(url="https://www.colapt.com/", apikey="test-key", ua=None, timeout=15, proxy=0)
    tester = SiteChain().special_site_test["colapt.com"]
    assert tester(site)[0] is expected
    assert captured["url"] == "https://api.colapt.com/api/member/profile"
    assert captured["headers"]["x-api-key"] == "test-key"
    captured.clear()
    site.apikey = ""
    assert tester(site) == (False, "请配置站点 API Key")
    assert captured == {}


def test_colapt_download_exchanges_ticket_without_cookies(monkeypatch, search_payload):
    """实际搜索结果经下载链换票，最终种子请求不携带 Cookie 或 API Key。"""
    spider = MTorrentSpider(_indexer())
    _, rows = spider._process_search_response(search_payload)
    enclosure = rows[1]["enclosure"]
    encoded, url = enclosure[1:].split("]", 1)
    assert url == "https://api.colapt.com/api/torrent/genDlToken"
    assert json.loads(base64.b64decode(encoded))["params"] == {"id": 632}
    captured = {}

    def respond(request, url, **kwargs):
        assert request._headers["x-api-key"] == "test-key"
        assert kwargs["params"] == {"id": 632}
        assert url == "https://api.colapt.com/api/torrent/genDlToken"
        return FakeResponse({"code": 0, "data": "https://api.colapt.com/downloads/test-ticket/632"})

    def download(_helper, **kwargs):
        captured.update(kwargs)
        return None, b"test-torrent", "", ["episode.mkv"], ""

    monkeypatch.setattr("app.chain.download.RequestUtils.post_res", respond)
    monkeypatch.setattr("app.chain.download.TorrentHelper.download_torrent", download)
    torrent = TorrentInfo(title="测试种子", enclosure=enclosure, site_cookie="unused-cookie")
    assert object.__new__(DownloadChain).download_torrent(torrent)[0] == b"test-torrent"
    assert captured["cookie"] is None
    assert captured["url"] == "https://api.colapt.com/downloads/test-ticket/632"
    assert captured["cache_invalid"] is False


def test_colapt_indexer_dispatch(monkeypatch, search_payload):
    """目录中的 parser 能进入主搜索链，而非退回通用 HTML 爬虫。"""
    monkeypatch.setattr("app.modules.indexer.spider.mtorrent.RequestUtils.post_res",
                        lambda *args, **kwargs: FakeResponse(search_payload))
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(module, "_IndexerModule__search_check", lambda *args, **kwargs: True)
    monkeypatch.setattr(module, "_IndexerModule__indexer_statistic", lambda *args, **kwargs: None)
    result = module.search_torrents(site=_indexer(), keyword="玉簟秋")
    assert len(result) == 3
    assert result[1].site == 42
    assert result[1].title == search_payload["data"]["data"][1]["name"]
