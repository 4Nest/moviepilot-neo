"""Mikan Project RSS 搜索适配测试：字段映射、URL 校验、请求参数与模块分发。"""

import asyncio

from app.modules.indexer import IndexerModule
from app.modules.indexer.spider.mikan import MikanSpider
from app.schemas import MediaType


RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Mikan Project - 搜索结果</title>
  <item>
    <guid isPermaLink="false">[北宇治字幕组] GROTESQQQUE [Movie][WebRip][繁日内嵌]</guid>
    <link>https://mikanani.me/Home/Episode/1b443fa09b2332839a619e0bc68f8311a01198f2</link>
    <title>[北宇治字幕组] GROTESQQQUE [Movie][WebRip][繁日内嵌]</title>
    <description>[北宇治字幕组] GROTESQQQUE [Movie][WebRip][繁日内嵌][2.48 GB]</description>
    <torrent xmlns="https://mikanani.me/0.1/">
      <link>https://mikanani.me/Home/Episode/1b443fa09b2332839a619e0bc68f8311a01198f2</link>
      <contentLength>2662879744</contentLength>
      <pubDate>2026-10-27T12:08:29.582</pubDate>
    </torrent>
    <enclosure type="application/x-bittorrent" length="2662879744"
               url="https://mikanani.me/Download/20261027/1b443fa09b2332839a619e0bc68f8311a01198f2.torrent" />
  </item>
  <item>
    <link>https://mikanani.me/Home/Episode/58e38c6d83c0ff2801499addc49c9a6578469255</link>
    <title>[LoliHouse] Show - 01</title>
    <description>[LoliHouse] Show - 01</description>
    <enclosure type="application/x-bittorrent" length="100"
               url="https://mikanani.me/Download/20261027/58e38c6d83c0ff2801499addc49c9a6578469255.torrent" />
  </item>
</channel></rss>""".encode("utf-8")


class _FakeResponse:
    def __init__(self, content: bytes = RSS_XML, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class _RequestRecorder:
    def __init__(self):
        self.calls = []

    def get_res(self, request, url: str, params: dict = None, **kwargs):
        self.calls.append({"url": url, "params": params, **kwargs})
        return _FakeResponse()

    async def async_get_res(self, request, url: str, params: dict = None, **kwargs):
        self.calls.append({"url": url, "params": params, **kwargs})
        return _FakeResponse()


def _indexer(**kwargs) -> dict:
    indexer = {"id": 1, "name": "Mikan", "domain": "https://mikanani.me/", "ua": "MoviePilot-Test", "proxy": False}
    indexer.update(kwargs)
    return indexer


def test_mikan_parse_maps_release_fields():
    torrents = MikanSpider._parse_xml(RSS_XML)

    assert len(torrents) == 2
    first, second = torrents
    assert first["title"].startswith("[北宇治字幕组] GROTESQQQUE")
    assert first["enclosure"].startswith("https://mikanani.me/Download/")
    assert first["page_url"].startswith("https://mikanani.me/Home/Episode/")
    assert first["size"] == 2662879744
    assert first["category"] == MediaType.MOVIE.value
    assert first["description"].endswith("[2.48 GB]")
    assert second["category"] == MediaType.TV.value
    assert second["size"] == 100


def test_mikan_parse_rejects_cross_origin_links():
    content = RSS_XML.replace(
        b"https://mikanani.me/Download/", b"https://mirror.example/Download/"
    )
    assert MikanSpider._parse_xml(content) == []


def test_mikan_sync_search_uses_rss_search(monkeypatch):
    recorder = _RequestRecorder()
    monkeypatch.setattr(
        "app.modules.indexer.spider.mikan.RequestUtils.get_res",
        lambda request, **kwargs: recorder.get_res(request, **kwargs),
    )
    error, torrents = MikanSpider(_indexer()).search("GROTESQQQUE -グロテスク-", mtype=MediaType.MOVIE, page=0)

    assert error is False
    assert len(torrents) == 2
    assert recorder.calls[0]["url"] == "https://mikanani.me/RSS/Search"
    assert recorder.calls[0]["params"] == {"searchstr": "GROTESQQQUE -グロテスク-"}


def test_mikan_async_search_uses_same_contract(monkeypatch):
    recorder = _RequestRecorder()
    monkeypatch.setattr(
        "app.modules.indexer.spider.mikan.AsyncRequestUtils.get_res",
        lambda request, **kwargs: recorder.async_get_res(request, **kwargs),
    )
    error, torrents = asyncio.run(MikanSpider(_indexer()).async_search("GROTESQQQUE", page=0))

    assert error is False
    assert len(torrents) == 2
    assert recorder.calls[0]["params"] == {"searchstr": "GROTESQQQUE"}


def test_mikan_later_page_skips_network(monkeypatch):
    def fail_request(*_args, **_kwargs):
        raise AssertionError("不应请求 Mikan")

    monkeypatch.setattr("app.modules.indexer.spider.mikan.RequestUtils.get_res", fail_request)
    assert MikanSpider(_indexer()).search("Show", page=1) == (False, [])


def test_indexer_module_dispatches_mikan_by_domain(monkeypatch):
    captured = {}

    def fake_search(_self, keyword, mtype, cat, page):
        captured.update({"keyword": keyword, "mtype": mtype, "cat": cat, "page": page})
        return False, [{
            "title": "[北宇治字幕组] GROTESQQQUE",
            "enclosure": "https://mikanani.me/Download/20261027/example.torrent",
        }]

    monkeypatch.setattr(MikanSpider, "search", fake_search)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda _site, _keyword=None: True))
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", staticmethod(lambda **_kwargs: None))

    torrents = object.__new__(IndexerModule).search_torrents(
        site=_indexer(), keyword="GROTESQQQUE", mtype=MediaType.MOVIE, cat=None, page=0,
    )

    assert captured == {"keyword": "GROTESQQQUE", "mtype": MediaType.MOVIE, "cat": None, "page": 0}
    assert len(torrents) == 1
    assert torrents[0].site_cookie is None
    assert IndexerModule.get_search_page_size(_indexer()) is None


def test_indexer_module_async_dispatches_mikan_by_domain(monkeypatch):
    captured = {}

    async def fake_search(_self, keyword, mtype, cat, page):
        captured.update({"keyword": keyword, "mtype": mtype, "cat": cat, "page": page})
        return False, [{
            "title": "[北宇治字幕组] GROTESQQQUE",
            "enclosure": "https://mikanani.me/Download/20261027/example.torrent",
        }]

    monkeypatch.setattr(MikanSpider, "async_search", fake_search)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda _site, _keyword=None: True))

    async def fake_statistic(**_kwargs):
        return None

    monkeypatch.setattr(IndexerModule, "_IndexerModule__async_indexer_statistic", staticmethod(fake_statistic))

    torrents = asyncio.run(object.__new__(IndexerModule).async_search_torrents(
        site=_indexer(), keyword="GROTESQQQUE", mtype=MediaType.MOVIE, cat=None, page=0,
    ))

    assert captured["mtype"] == MediaType.MOVIE
    assert len(torrents) == 1
    assert torrents[0].site_cookie is None
