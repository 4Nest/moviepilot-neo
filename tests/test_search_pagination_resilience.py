import asyncio
from types import SimpleNamespace

import pytest

from app.chain import search as search_module
from app.chain.search import SearchChain
from app.core.context import SubtitleInfo, TorrentInfo
from app.modules.indexer import IndexerModule
from app.utils.http import AsyncRequestUtils, RequestUtils


class _DummyProgress:
    """屏蔽真实进度推送的空进度条桩。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        """开始进度。"""

    def update(self, **_kwargs):
        """更新进度。"""

    def end(self):
        """结束进度。"""


def _make_chain(page_size: int = 2) -> SearchChain:
    """构造不触发外部依赖的 SearchChain 实例。"""
    chain = object.__new__(SearchChain)
    chain.get_search_page_size = lambda site, keyword=None: page_size
    return chain


def _patch_search_env(monkeypatch, sites: list[dict], pages: int = 3) -> None:
    """隔离站点配置、系统配置与进度条依赖。"""

    async def async_get_indexers():
        """返回测试站点列表。"""
        return sites

    monkeypatch.setattr(search_module, "ProgressHelper", _DummyProgress)
    monkeypatch.setattr(
        search_module,
        "SitesHelper",
        lambda: SimpleNamespace(
            get_indexers=lambda: sites,
            async_get_indexers=async_get_indexers,
        ),
    )
    monkeypatch.setattr(
        search_module,
        "SystemConfigOper",
        lambda: SimpleNamespace(get=lambda _key: [site["id"] for site in sites]),
    )
    monkeypatch.setattr(search_module.settings, "SEARCH_RESOURCE_PAGES", pages)


def _torrent(site: dict, title: str) -> TorrentInfo:
    """构造最小可用的种子对象。"""
    return TorrentInfo(
        site=site["id"],
        site_name=site["name"],
        title=title,
        description="",
    )


def _subtitle(site: dict, title: str) -> SubtitleInfo:
    """构造最小可用的字幕对象。"""
    return SubtitleInfo(
        site=site["id"],
        site_name=site["name"],
        title=title,
        language="zh",
        enclosure=f"https://example.com/{title}.srt",
        size=1024,
    )


def test_sync_search_all_sites_preserves_first_page_when_second_page_raises(monkeypatch):
    """同步单站点第 2 页抛异常（如 HTTP 500）时应保留第 1 页结果且不再请求第 3 页。"""
    site = {"id": 1, "name": "财神"}
    _patch_search_env(monkeypatch, [site], pages=3)
    chain = _make_chain(page_size=2)
    calls: list[int] = []

    def fake_search_torrents(site, keyword=None, mtype=None, page=0):
        calls.append(page)
        if page == 0:
            return [_torrent(site, "p0-1"), _torrent(site, "p0-2")]
        raise RuntimeError("站点请求或页面解析失败（HTTP 500，响应内容为空）")

    chain.search_torrents = fake_search_torrents

    results = chain._SearchChain__search_all_sites(keyword="demo", sites=[1])

    assert calls == [0, 1]
    assert [item.title for item in results] == ["p0-1", "p0-2"]


def test_sync_search_all_sites_isolates_single_site_exception(monkeypatch):
    """同步多站点并发搜索中单个站点或续页抛异常时，其余站点与已收集结果不丢失。"""
    sites = [
        {"id": 1, "name": "故障站点"},
        {"id": 2, "name": "续页故障站点"},
        {"id": 3, "name": "正常站点"},
    ]
    _patch_search_env(monkeypatch, sites, pages=3)
    chain = _make_chain(page_size=2)

    def fake_search_torrents(site, keyword=None, mtype=None, page=0):
        if site["id"] == 1:
            raise RuntimeError("HTTP 500")
        if site["id"] == 2:
            if page == 0:
                return [_torrent(site, "s2-p0-1"), _torrent(site, "s2-p0-2")]
            raise RuntimeError("HTTP 500 on page 2")
        if page == 0:
            return [_torrent(site, "s3-p0-1"), _torrent(site, "s3-p0-2")]
        return [_torrent(site, "s3-p1-1")]

    chain.search_torrents = fake_search_torrents

    results = chain._SearchChain__search_all_sites(keyword="demo", sites=[1, 2, 3])

    assert sorted(item.title for item in results) == [
        "s2-p0-1",
        "s2-p0-2",
        "s3-p0-1",
        "s3-p0-2",
        "s3-p1-1",
    ]


def test_async_search_all_sites_preserves_first_page_and_other_sites(monkeypatch):
    """异步多站点搜索中首页或续页抛异常时，保留已收集结果和其余站点结果。"""
    sites = [
        {"id": 1, "name": "故障站点"},
        {"id": 2, "name": "续页故障站点"},
        {"id": 3, "name": "正常站点"},
    ]
    _patch_search_env(monkeypatch, sites, pages=3)
    chain = _make_chain(page_size=2)
    s2_pages: list[int] = []

    async def fake_async_search_torrents(site, keyword=None, mtype=None, page=0):
        if site["id"] == 1:
            raise RuntimeError("HTTP 500")
        if site["id"] == 2:
            s2_pages.append(page)
            if page == 0:
                return [_torrent(site, "s2-p0-1"), _torrent(site, "s2-p0-2")]
            raise RuntimeError("HTTP 500 on page 2")
        if page == 0:
            return [_torrent(site, "s3-p0-1"), _torrent(site, "s3-p0-2")]
        return [_torrent(site, "s3-p1-1")]

    chain.async_search_torrents = fake_async_search_torrents

    results = asyncio.run(
        chain._SearchChain__async_search_all_sites(keyword="demo", sites=[1, 2, 3])
    )

    assert s2_pages == [0, 1]
    assert sorted(item.title for item in results) == [
        "s2-p0-1",
        "s2-p0-2",
        "s3-p0-1",
        "s3-p0-2",
        "s3-p1-1",
    ]


def test_async_search_all_sites_propagates_cancelled_error(monkeypatch):
    """异步搜索中的 asyncio.CancelledError 必须正常向上传播，不得被吞掉。"""
    site = {"id": 1, "name": "站点A"}
    _patch_search_env(monkeypatch, [site], pages=2)
    chain = _make_chain(page_size=2)

    async def fake_async_search_torrents(site, keyword=None, mtype=None, page=0):
        raise asyncio.CancelledError()

    chain.async_search_torrents = fake_async_search_torrents

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chain._SearchChain__async_search_all_sites(keyword="demo", sites=[1]))


def test_async_search_all_sites_stream_isolates_exceptions_and_preserves_results(monkeypatch):
    """流式多站点搜索中首页或续页抛异常时，保留其余结果且续页异常不覆盖首页成功诊断。"""
    sites = [
        {"id": 1, "name": "故障站点"},
        {"id": 2, "name": "续页故障站点"},
        {"id": 3, "name": "正常站点"},
    ]
    _patch_search_env(monkeypatch, sites, pages=3)
    chain = _make_chain(page_size=2)
    s2_pages: list[int] = []

    async def fake_async_search_torrents(site, keyword=None, mtype=None, page=0):
        if site["id"] == 1:
            raise RuntimeError("HTTP 500")
        if site["id"] == 2:
            s2_pages.append(page)
            if page == 0:
                return [_torrent(site, "s2-p0-1"), _torrent(site, "s2-p0-2")]
            raise RuntimeError("HTTP 500 on page 2")
        if page == 0:
            return [_torrent(site, "s3-p0-1"), _torrent(site, "s3-p0-2")]
        return [_torrent(site, "s3-p1-1")]

    chain.async_search_torrents = fake_async_search_torrents

    async def _collect():
        return [
            event
            async for event in chain._SearchChain__async_search_all_sites_stream(
                keyword="demo", sites=[1, 2, 3]
            )
        ]

    events = asyncio.run(_collect())
    append_events = [event for event in events if event["type"] == "append"]
    titles = sorted(item.title for event in append_events for item in event["items"])

    assert s2_pages == [0, 1]
    assert titles == ["s2-p0-1", "s2-p0-2", "s3-p0-1", "s3-p0-2", "s3-p1-1"]

    # 站点 1 首页异常应记录为 error；站点 2 续页异常应按 empty 收口，避免把已成功的站点标为 error
    s1_diags = [e["site_diagnostic"].outcome for e in append_events if e["site_id"] == 1]
    s2_diags = [e["site_diagnostic"].outcome for e in append_events if e["site_id"] == 2]
    assert s1_diags == ["error"]
    assert "success" in s2_diags
    assert "error" not in s2_diags


def test_async_search_all_sites_stream_propagates_cancelled_error(monkeypatch):
    """流式搜索中的 asyncio.CancelledError 必须正常向上传播。"""
    site = {"id": 1, "name": "站点A"}
    _patch_search_env(monkeypatch, [site], pages=2)
    chain = _make_chain(page_size=2)

    async def fake_async_search_torrents(site, keyword=None, mtype=None, page=0):
        raise asyncio.CancelledError()

    chain.async_search_torrents = fake_async_search_torrents

    async def _consume():
        return [
            event
            async for event in chain._SearchChain__async_search_all_sites_stream(
                keyword="demo", sites=[1]
            )
        ]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_consume())


def test_indexer_second_page_http_500_empty_response_keeps_first_page(monkeypatch):
    """通过 IndexerModule + SiteSpider 搜索时，第 2 页返回 HTTP 500 空响应应保留第 1 页结果。"""
    site = {
        "id": 10,
        "name": "测试PT",
        "domain": "https://pt.example.com",
        "result_num": 2,
        "search": {
            "paths": [{"path": "torrents.php?search={keyword}&page={page}"}],
        },
        "torrents": {
            "list": {"selector": "table.torrents tr.item"},
            "fields": {
                "title": {"selector": "a.title"},
            },
        },
    }
    _patch_search_env(monkeypatch, [site], pages=3)

    page0_html = """
    <html><body><table class="torrents">
        <tr class="item"><td><a class="title">Movie.One.2024</a></td></tr>
        <tr class="item"><td><a class="title">Movie.Two.2024</a></td></tr>
    </table></body></html>
    """
    resp_ok = SimpleNamespace(status_code=200, content=page0_html.encode("utf-8"), text=page0_html, encoding="utf-8")
    resp_500 = SimpleNamespace(status_code=500, content=b"", text="", encoding="utf-8")

    sync_urls: list[str] = []
    async_urls: list[str] = []

    def fake_sync_get_res(self, url, **_kwargs):
        sync_urls.append(url)
        return resp_ok if "page=0" in url else resp_500

    async def fake_async_get_res(self, url, **_kwargs):
        async_urls.append(url)
        return resp_ok if "page=0" in url else resp_500

    monkeypatch.setattr(RequestUtils, "get_res", fake_sync_get_res)
    monkeypatch.setattr(AsyncRequestUtils, "get_res", fake_async_get_res)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda _s, _w=None: True))
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", staticmethod(lambda **_kw: None))

    async def async_noop_stat(**_kw):
        return None

    monkeypatch.setattr(IndexerModule, "_IndexerModule__async_indexer_statistic", staticmethod(async_noop_stat))

    indexer = IndexerModule()
    chain = _make_chain(page_size=2)
    chain.search_torrents = indexer.search_torrents
    chain.async_search_torrents = indexer.async_search_torrents

    sync_results = chain._SearchChain__search_all_sites(keyword="Movie", sites=[10])
    async_results = asyncio.run(chain._SearchChain__async_search_all_sites(keyword="Movie", sites=[10]))

    assert len(sync_urls) == 2
    assert len(async_urls) == 2
    assert [item.title for item in sync_results] == ["Movie.One.2024", "Movie.Two.2024"]
    assert [item.title for item in async_results] == ["Movie.One.2024", "Movie.Two.2024"]


def test_async_subtitle_search_and_stream_isolate_exceptions(monkeypatch):
    """字幕并发搜索和流式搜索在单站点或续页抛异常时应保留其余结果。"""
    sites = [
        {"id": 1, "name": "字幕站1", "subtitles": {"result_num": 1}},
        {"id": 2, "name": "字幕站2", "subtitles": {"result_num": 1}},
    ]
    _patch_search_env(monkeypatch, sites, pages=2)
    chain = _make_chain(page_size=1)

    async def fake_async_search_subtitles(site, keyword=None, page=0):
        if site["id"] == 1:
            if page == 0:
                return [_subtitle(site, "sub-1-p0")]
            raise RuntimeError("subtitle page 2 failed")
        raise RuntimeError("subtitle site 2 failed")

    chain.async_search_subtitles = fake_async_search_subtitles

    list_results = asyncio.run(
        chain._SearchChain__async_search_subtitles_all_sites(keyword="demo", sites=[1, 2])
    )
    assert [item.title for item in list_results] == ["sub-1-p0"]

    async def _collect_stream():
        return [
            event
            async for event in chain._SearchChain__async_search_subtitles_all_sites_stream(
                keyword="demo", sites=[1, 2]
            )
        ]

    stream_events = asyncio.run(_collect_stream())
    stream_titles = [
        item.title
        for event in stream_events
        if event["type"] == "append"
        for item in event["items"]
    ]
    assert stream_titles == ["sub-1-p0"]
