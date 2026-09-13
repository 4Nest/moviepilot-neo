import asyncio

from app.modules.indexer import IndexerModule
from app.modules.indexer.spider.anibt import AniBTSpider
from app.schemas import MediaType


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:anibt="https://anibt.net/xmlns/rss/1.0/"
     xmlns:torrent="https://anibt.moe/xmlns/0.1/">
  <channel>
    <item>
      <title>[NEST] Example Anime - 01 [1080p]</title>
      <link>https://anibt.net/release/rel_example</link>
      <pubDate>Sun, 13 Sep 2026 13:57:11 +0800</pubDate>
      <anibt:torrentUrl>https://anibt.net/api/torrent/rel_example.torrent</anibt:torrentUrl>
      <anibt:releasePageUrl>https://anibt.net/release/rel_example</anibt:releasePageUrl>
      <anibt:animeTitle>示例动画</anibt:animeTitle>
      <anibt:groupName>NEST</anibt:groupName>
      <anibt:resolution>1080p</anibt:resolution>
      <anibt:language>CHS</anibt:language>
      <anibt:language>CHT</anibt:language>
      <anibt:subtitle>INTERNAL</anibt:subtitle>
      <anibt:format>MKV</anibt:format>
      <anibt:fileSize>1456301336</anibt:fileSize>
      <anibt:customTag>WEB-DL</anibt:customTag>
      <torrent:torrent><torrent:contentLength>1456301336</torrent:contentLength></torrent:torrent>
      <enclosure url="https://anibt.net/api/torrent/rel_example.torrent"
                 length="1456301336" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>""".encode("utf-8")


class _FakeResponse:
    def __init__(self, content: bytes = RSS_XML, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class _RequestRecorder:
    def __init__(self):
        self.calls = []

    def get_res(self, request, url: str, params: dict = None, **kwargs):
        self.calls.append({
            "url": url,
            "params": params,
            "headers": request._headers,
            "cookies": request._cookies,
            **kwargs,
        })
        return _FakeResponse()

    async def async_get_res(self, request, url: str, params: dict = None, **kwargs):
        self.calls.append({
            "url": url,
            "params": params,
            "headers": request._headers,
            "cookies": request._cookies,
            **kwargs,
        })
        return _FakeResponse()


def _indexer(**kwargs) -> dict:
    indexer = {
        "id": 43,
        "name": "AniBT",
        "domain": "https://anibt.net/",
        "parser": "legacy-html-parser",
        "cookie": "legacy=session-cookie",
        "ua": "MoviePilot-Test",
        "proxy": False,
        "pri": 1,
    }
    indexer.update(kwargs)
    return indexer


def test_anibt_rss_maps_public_release_fields():
    torrents = AniBTSpider._parse_xml(RSS_XML)

    assert torrents == [{
        "title": "[NEST] Example Anime - 01 [1080p]",
        "description": "示例动画",
        "enclosure": "https://anibt.net/api/torrent/rel_example.torrent",
        "page_url": "https://anibt.net/release/rel_example",
        "pubdate": "2026-09-13 13:57:11",
        "size": 1456301336,
        "seeders": 0,
        "peers": 0,
        "grabs": 0,
        "downloadvolumefactor": 0,
        "uploadvolumefactor": 1,
        "labels": ["NEST", "1080p", "CHS", "CHT", "INTERNAL", "MKV", "WEB-DL"],
        "hit_and_run": False,
        "category": MediaType.TV.value,
    }]


def test_anibt_rss_rejects_cross_origin_download_link():
    malicious = RSS_XML.replace(
        b"https://anibt.net/api/torrent/rel_example.torrent",
        b"https://attacker.example/api/torrent/rel_example.torrent",
    )

    assert AniBTSpider._parse_xml(malicious) == []


def test_anibt_sync_search_uses_public_rss_without_legacy_cookie(monkeypatch):
    recorder = _RequestRecorder()
    monkeypatch.setattr(
        "app.modules.indexer.spider.anibt.RequestUtils.get_res",
        lambda request, **kwargs: recorder.get_res(request, **kwargs),
    )

    error, torrents = AniBTSpider(_indexer()).search(
        keyword="无职转生",
        mtype=MediaType.TV,
        page=0,
    )

    assert not error
    assert len(torrents) == 1
    assert recorder.calls == [{
        "url": "https://anibt.net/rss/magnets.xml",
        "params": {"q": "无职转生"},
        "headers": {
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            "User-Agent": "MoviePilot-Test",
        },
        "cookies": None,
    }]


def test_anibt_async_search_uses_same_public_contract(monkeypatch):
    recorder = _RequestRecorder()
    monkeypatch.setattr(
        "app.modules.indexer.spider.anibt.AsyncRequestUtils.get_res",
        lambda request, **kwargs: recorder.async_get_res(request, **kwargs),
    )

    error, torrents = asyncio.run(
        AniBTSpider(_indexer()).async_search(
            keyword="无职转生",
            mtype=MediaType.TV,
            page=0,
        )
    )

    assert not error
    assert len(torrents) == 1
    assert recorder.calls[0]["params"] == {"q": "无职转生"}
    assert recorder.calls[0]["cookies"] is None


def test_anibt_movie_and_later_page_skip_network(monkeypatch):
    def fail_request(*_args, **_kwargs):
        raise AssertionError("不应请求 AniBT")

    monkeypatch.setattr(
        "app.modules.indexer.spider.anibt.RequestUtils.get_res",
        fail_request,
    )
    spider = AniBTSpider(_indexer())

    assert spider.search("Movie", mtype=MediaType.MOVIE, page=0) == (False, [])
    assert spider.search("Anime", mtype=MediaType.TV, page=1) == (False, [])


def test_indexer_module_dispatches_anibt_by_domain(monkeypatch):
    captured = {}

    def fake_search(_self, keyword, mtype, cat, page):
        captured.update({
            "keyword": keyword,
            "mtype": mtype,
            "cat": cat,
            "page": page,
        })
        return False, [{
            "title": "[NEST] Example Anime - 01",
            "enclosure": "https://anibt.net/api/torrent/rel_example.torrent",
        }]

    monkeypatch.setattr(AniBTSpider, "search", fake_search)
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(lambda _site, _keyword=None: True),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__indexer_statistic",
        staticmethod(lambda **_kwargs: None),
    )

    torrents = object.__new__(IndexerModule).search_torrents(
        site=_indexer(),
        keyword="Example Anime",
        mtype=MediaType.TV,
        cat=None,
        page=0,
    )

    assert captured == {
        "keyword": "Example Anime",
        "mtype": MediaType.TV,
        "cat": None,
        "page": 0,
    }
    assert len(torrents) == 1
    assert torrents[0].title == "[NEST] Example Anime - 01"
    assert torrents[0].site_cookie is None
    assert IndexerModule.get_search_page_size(_indexer()) is None


def test_indexer_module_async_refresh_dispatches_anibt_by_domain(monkeypatch):
    captured = {}

    async def fake_search(_self, keyword, mtype, cat, page):
        captured.update({
            "keyword": keyword,
            "mtype": mtype,
            "cat": cat,
            "page": page,
        })
        return False, [{
            "title": "[NEST] Example Anime - 01",
            "enclosure": "https://anibt.net/api/torrent/rel_example.torrent",
        }]

    async def fake_statistic(**_kwargs):
        return None

    monkeypatch.setattr(AniBTSpider, "async_search", fake_search)
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(lambda _site, _keyword=None: True),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__async_indexer_statistic",
        staticmethod(fake_statistic),
    )

    torrents = asyncio.run(
        object.__new__(IndexerModule).async_refresh_torrents(
            site=_indexer(),
            keyword=None,
            cat=None,
            page=0,
        )
    )

    assert captured == {
        "keyword": None,
        "mtype": None,
        "cat": None,
        "page": 0,
    }
    assert len(torrents) == 1
    assert torrents[0].site_cookie is None
