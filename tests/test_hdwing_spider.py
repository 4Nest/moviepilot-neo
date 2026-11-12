"""HDWinG 促销回填、通用解析与安全降级的离线测试。"""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.modules.indexer import HDWingPageSpider, HDWingSpider, SPIDER_PARSER_CLASSES

_PAGE_HTML = """
<html><head><meta name="x-csrf" content="csrf-token"></head><body>
<table><tr class="torrent"><td><a title="Free.Torrent">Free.Torrent</a></td>
<td><span class="sp_state_placeholder" id="101"></span></td></tr>
<tr class="torrent"><td><a title="Normal.Torrent">Normal.Torrent</a></td>
<td><span class="sp_state_placeholder" id="102"></span></td></tr></table>
</body></html>
"""
_INDEXER = {
    "id": 1,
    "name": "HDWinG",
    "domain": "https://hdwing.org/",
    "parser": "HDWing",
    "search": {"paths": [{"path": "torrents.php"}], "params": {"search": "{keyword}"}},
    "torrents": {
        "list": {"selector": "tr.torrent"},
        "fields": {
            "title": {"selector": "a[title]", "attribute": "title"},
            "downloadvolumefactor": {"case": {"img.pro_free": 0, "*": 1}},
            "uploadvolumefactor": {"case": {"img.pro_free2up": 2, "*": 1}},
            "freedate": {"selector": "span.sp_state_placeholder"},
        },
    },
}
_PROMOTIONS = {
    "status": 200,
    "message": {
        "101": {
            "sp_state": '<img class="pro_free" src="pic/trans.gif" alt="Free">',
            "timeout": "2026-11-13 12:00:00",
        },
        "102": {"sp_state": "", "timeout": ""},
    },
}


def _response(payload=None, status_code: int = 200) -> MagicMock:
    """构造不访问网络的促销接口响应。"""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_hdwing_registered_and_injects_promotions(monkeypatch):
    """注册专站解析器，并在通用解析前注入免费标识和截止时间。"""
    request = MagicMock()
    request.post_res.return_value = _response(_PROMOTIONS)
    request_factory = MagicMock(return_value=request)
    monkeypatch.setattr("app.modules.indexer.spider.hdwing.RequestUtils", request_factory)

    assert SPIDER_PARSER_CLASSES["HDWing"] is HDWingSpider
    spider = HDWingPageSpider(deepcopy(_INDEXER))
    torrents = spider.parse(_PAGE_HTML)

    assert spider.is_error is False
    assert torrents[0]["downloadvolumefactor"] == 0
    assert torrents[0]["freedate"] == "2026-11-13 12:00:00"
    assert torrents[1]["downloadvolumefactor"] == 1
    request.post_res.assert_called_once_with(
        url="https://hdwing.org/ajax_promotion.php",
        data={"ids[]": ["101", "102"], "csrf": "csrf-token"},
    )
    assert request_factory.call_args.kwargs["headers"] == {"X-Requested-With": "XMLHttpRequest"}
    assert request_factory.call_args.kwargs["referer"] == "https://hdwing.org/torrents.php"


def test_hdwing_logout_csrf_and_duplicate_ids(monkeypatch):
    """退出链接可提供 CSRF，重复占位符只查询一次且都获得回填。"""
    request = MagicMock()
    request.post_res.return_value = _response(_PROMOTIONS)
    monkeypatch.setattr("app.modules.indexer.spider.hdwing.RequestUtils", MagicMock(return_value=request))
    html = _PAGE_HTML.replace('<meta name="x-csrf" content="csrf-token">', "")
    html = html.replace("</body>", '<a href="logout.php?action=logout&amp;csrf=logout-token">退出</a></body>')
    html = html.replace("</table>", '<span id="101" class="sp_state_placeholder"></span></table>')

    injected = HDWingPageSpider(deepcopy(_INDEXER)).inject_promotions(html)

    assert injected.count('class="pro_free"') == 2
    assert injected.count("2026-11-13 12:00:00") == 2
    assert request.post_res.call_args.kwargs["data"] == {
        "ids[]": ["101", "102"], "csrf": "logout-token",
    }


def test_hdwing_skips_request_without_placeholder(monkeypatch):
    """没有真实占位符时不调用促销接口，也不影响普通页面解析。"""
    request = MagicMock()
    monkeypatch.setattr("app.modules.indexer.spider.hdwing.RequestUtils", MagicMock(return_value=request))
    html = _PAGE_HTML.replace('class="sp_state_placeholder"', 'class="disabled-sp_state_placeholder"')

    torrents = HDWingPageSpider(deepcopy(_INDEXER)).parse(html)

    assert len(torrents) == 2
    request.post_res.assert_not_called()


@pytest.mark.parametrize("response", [None, _response(status_code=429), _response(status_code=500)])
def test_hdwing_promotion_failure_preserves_original_page(monkeypatch, response):
    """无响应、限流及服务端失败均安全降级为原始 HTML。"""
    request = MagicMock()
    request.post_res.return_value = response
    monkeypatch.setattr("app.modules.indexer.spider.hdwing.RequestUtils", MagicMock(return_value=request))

    spider = HDWingPageSpider(deepcopy(_INDEXER))
    torrents = spider.parse(_PAGE_HTML)

    assert spider.is_error is False
    assert len(torrents) == 2
    assert all(item["downloadvolumefactor"] == 1 for item in torrents)


@pytest.mark.asyncio
async def test_hdwing_async_search_uses_same_page_spider(monkeypatch):
    """异步搜索复用同一促销页面爬虫并返回统一搜索契约。"""
    async def fake_async_get_torrents(self):
        """离线返回固定结果并记录页面爬虫上下文。"""
        assert self.keyword == "Example"
        assert self.page == 2
        return [{"title": "Example"}]

    monkeypatch.setattr(HDWingPageSpider, "async_get_torrents", fake_async_get_torrents)
    spider = HDWingSpider(deepcopy(_INDEXER))

    assert await spider.async_search(keyword="Example", page=2) == (False, [{"title": "Example"}])
    assert HDWingSpider.get_search_page_size() == 100
