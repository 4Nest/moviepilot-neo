"""FluxTorrent 兼容层搜索、促销、用户资料与连通性的离线测试。"""

import asyncio
import json
from unittest.mock import Mock

import pytest

from app.modules.indexer import IndexerModule, SPIDER_PARSER_CLASSES
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.fluxtorrent import FluxTorrentSiteUserInfo
from app.modules.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.schemas import MediaType
from app.utils.string import StringUtils

SITE = {
    "id": 88,
    "name": "好学",
    "url": "https://www.hxpt.org/",
    "domain": "https://www.hxpt.org/",
    "parser": "FluxTorrent",
    "schema": "FluxTorrent",
    "apikey": "flux-test-key",
    "ua": "Flux-Test",
}
ITEM = {
    "id": 123,
    "name": "Example.Movie.2026.2160p.WEB-DL",
    "small_descr": "示例电影",
    "seeders": 15,
    "leechers": 2,
    "completed": 30,
    "size": 21474836480,
    "added": 1753084800,
    "category_name": "电影",
    "category_newznab": 2000,
    "promotion": "x2free",
    "promotion_name": "2X免费",
    "official": True,
}


class FakeResponse:
    """提供 FluxTorrent 响应解析所需的离线契约。"""

    def __init__(self, data, code=0, status_code=200, message="ok"):
        """保存固定响应信封和 HTTP 状态。"""
        self.status_code = status_code
        self._payload = {"code": code, "message": message, "data": data}
        self.closed = False

    def json(self):
        """返回预设 JSON 数据。"""
        return self._payload

    def close(self):
        """记录同步响应释放。"""
        self.closed = True

    async def aclose(self):
        """记录异步响应释放。"""
        self.closed = True


def test_fluxtorrent_search_maps_fields_and_promotions(monkeypatch):
    """搜索应携带 Token，映射分类、促销、标签和下载地址。"""
    response = FakeResponse({"items": [ITEM]})
    request = Mock()
    request.get_res.return_value = response
    constructor = Mock(return_value=request)
    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils", constructor)

    failed, torrents = FluxTorrentSpider(SITE).search("Example", page=1)

    assert failed is False
    request.get_res.assert_called_once_with(
        url="https://www.hxpt.org/api/v1/compat/nexusphp/torrents.json",
        params={"page": 2, "pagesize": 50, "keyword": "Example"},
    )
    assert constructor.call_args.kwargs["headers"]["Authorization"] == "Token flux-test-key"
    assert torrents == [{
        "title": ITEM["name"], "description": ITEM["small_descr"],
        "enclosure": "https://www.hxpt.org/api/v1/compat/nexusphp/download.php?id=123&apikey=flux-test-key",
        "pubdate": StringUtils.format_timestamp(ITEM["added"]), "size": ITEM["size"],
        "seeders": 15, "peers": 2, "grabs": 30,
        "downloadvolumefactor": 0.0, "uploadvolumefactor": 2.0,
        "page_url": "https://www.hxpt.org/torrent/123",
        "labels": ["2X免费", "官方"], "category": MediaType.MOVIE.value,
    }]
    assert response.closed


@pytest.mark.parametrize("promotion,expected", [
    ("free", (0.0, 1.0)), ("x2free", (0.0, 2.0)), ("half", (0.5, 1.0)),
    ("x2half", (0.5, 2.0)), ("x2", (1.0, 2.0)), ("p30", (0.3, 1.0)),
    (None, (1.0, 1.0)),
])
def test_fluxtorrent_all_promotion_factors(promotion, expected):
    """全部兼容层促销档位均映射为正确下载和上传倍率。"""
    assert FluxTorrentSpider._volume_factors(promotion) == expected


@pytest.mark.asyncio
async def test_fluxtorrent_async_search_and_error(monkeypatch):
    """异步入口与同步契约一致，业务错误不会发布搜索结果。"""
    response = FakeResponse({"items": [ITEM]})
    request = Mock()

    async def get_res(**_kwargs):
        """异步回放固定种子列表。"""
        return response

    request.get_res = get_res
    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.AsyncRequestUtils", Mock(return_value=request))
    failed, torrents = await FluxTorrentSpider(SITE).async_search("Example")
    assert failed is False and len(torrents) == 1 and response.closed

    monkeypatch.setattr(
        "app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res",
        lambda *_args, **_kwargs: FakeResponse(None, code=401, status_code=401),
    )
    assert FluxTorrentSpider(SITE).search("Example") == (True, [])


def test_fluxtorrent_userdata(monkeypatch):
    """用户接口解析账号、流量、分享率、魔力和未读通知。"""
    profile = {
        "id": 42, "username": "tester", "class": 9, "class_name": "VIP",
        "uploaded": 21474836480, "downloaded": 1073741824,
        "ratio": 20.0, "ratio_infinite": False, "seedbonus": 1024,
        "unread_messages": 3,
    }
    monkeypatch.setattr(
        FluxTorrentSiteUserInfo, "_get_page_content",
        lambda self, url, **_kwargs: json.dumps({"code": 0, "data": profile}),
    )
    parser = FluxTorrentSiteUserInfo(
        site_name="好学", url=SITE["url"], site_cookie=None,
        apikey=SITE["apikey"], token=None, ua="Flux-Test",
    )

    parser.parse()

    assert parser.err_msg is None
    assert (parser.userid, parser.username, parser.user_level) == (42, "tester", "VIP")
    assert (parser.upload, parser.download, parser.ratio, parser.bonus) == (
        21474836480, 1073741824, 20.0, 1024.0,
    )
    assert parser.message_unread == 3


def test_fluxtorrent_connection_and_registration(monkeypatch):
    """专站注册搜索、用户资料以及基于 user.json 的连通性测试。"""
    response = FakeResponse({"id": 42, "username": "tester"})
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils", Mock(return_value=request))
    module = object.__new__(IndexerModule)

    assert SPIDER_PARSER_CLASSES["FluxTorrent"] is FluxTorrentSpider
    assert SiteSchema.FluxTorrent.value == "FluxTorrent"
    assert FluxTorrentSpider.get_search_page_size() == 50
    assert module.test_site(SITE) == (True, "连接成功")
    assert request.get_res.call_args.kwargs["url"].endswith("/api/v1/compat/nexusphp/user.json")
    assert response.closed


def test_fluxtorrent_requires_api_token(monkeypatch):
    """缺少 API Key 时同步、异步与测活均不得发出网络请求。"""
    request = Mock()
    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils", Mock(return_value=request))
    spider = FluxTorrentSpider(dict(SITE, apikey=None, token=None))

    assert spider.search("Example") == (True, [])
    assert asyncio.run(spider.async_search("Example")) == (True, [])
    assert spider.test_connection()[0] is False
    request.get_res.assert_not_called()
