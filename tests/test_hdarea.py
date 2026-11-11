"""HDArea API v1 目录覆盖、种子与字幕检索、两段式下载、用户数据及连通性测试。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.core.context import TorrentInfo
from app.db.models.site import Site
from app.helper import site as catalog
from app.helper.rss import RssHelper
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.hdarea import HDAreaSiteUserInfo
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.modules.indexer.spider.hdarea import HDAreaSpider
from app.modules.subtitle import SubtitleModule
from app.schemas import MediaType


class FakeJsonResponse:
    """离线 JSON 响应替身。"""

    def __init__(
        self,
        payload: dict,
        status_code: int = 200,
        reason: str = "OK",
        headers: dict | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.reason = reason
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def __bool__(self) -> bool:
        return self.status_code < 400
    def json(self) -> dict:
        """返回深拷贝载荷避免用例间共享可变状态。"""
        return deepcopy(self._payload)


def _sample_torrents_payload() -> dict:
    return {
        "success": True,
        "code": 0,
        "http_code": 200,
        "message": "OK",
        "data": {
            "items": [
                {
                    "id": 434799,
                    "name": "Dune Part Two 2024 UHD BluRay 2160p DV HDR x265 Atmos-HDArea",
                    "small_descr": "沙丘2 | 国英双语 | 特效字幕",
                    "category_id": 300,
                    "category_name": "Movie UHD-4K",
                    "torznab_cat": 2000,
                    "size": 30695677476,
                    "seeders": 18,
                    "leechers": 2,
                    "times_completed": 144,
                    "added": "2026-10-21 17:42:04",
                    "first_upload": True,
                    "forbid_transfer": True,
                    "tags": ["sticky", "zz"],
                    "sp_state": 4,
                    "download_volume_factor": 0,
                    "upload_volume_factor": 2,
                    "promotion_until": "2026-11-12 18:00:00",
                    "imdb_id": "tt15239678",
                    "detail_url": "https://hdarea.club/details.php?id=434799",
                    "download_url": "https://hdarea.club/download.php?id=434799&dl_token=search-token",
                },
                {
                    "id": 105429,
                    "name": "Happy Toon S08 Complete 1080p WEB-DL AVC AAC-HDArea",
                    "small_descr": "剧集番剧合集",
                    "category_id": 405,
                    "category_name": "Animations",
                    "torznab_cat": 5000,
                    "size": 13600127377,
                    "seeders": 3,
                    "leechers": 0,
                    "times_completed": 8,
                    "added": "2023-11-03 17:39:15",
                    "first_upload": False,
                    "forbid_transfer": False,
                    "tags": [],
                    "sp_state": 5,
                    "download_volume_factor": 0.5,
                    "upload_volume_factor": 1,
                    "promotion_until": "0000-00-00 00:00:00",
                    "imdb_id": None,
                    "detail_url": "https://hdarea.club/details.php?id=105429",
                },
            ],
            "pagination": {
                "page": 0,
                "per_page": 100,
                "count": 2,
                "total": 2,
                "has_more": False,
            },
        },
    }


def _runtime_indexer() -> dict:
    return {
        **deepcopy(catalog.SITE_OVERRIDES["hdarea.club"]),
        "id": 12,
        "domain": "https://hdarea.club/",
        "url": "https://hdarea.club/",
        "apikey": "a" * 32,
        "cookie": "expired-browser-cookie",
        "ua": "MoviePilot-Test/2.16",
        "proxy": 0,
        "pri": 1,
    }


def test_sites_helper_overrides_legacy_nexusphp_entry_preserving_db_fields(monkeypatch):
    """内置二进制资源中的旧版 NexusPhp 配置应被升级为 HDArea API，且保留数据库实例字段。"""
    legacy_merged_entry = {
        "id": 12,
        "name": "高清视界",
        "domain": "https://hdarea.club/",
        "url": "https://hdarea.club/",
        "schema": "NexusPhp",
        "public": False,
        "apikey": "b" * 32,
        "cookie": "legacy-cookie",
        "pri": 3,
        "category": {
            "movie": [{"id": 300, "cat": "Movies/UHD", "desc": "Movies UHD-4K"}],
            "tv": [{"id": 402, "cat": "TV", "desc": "TV Series"}],
        },
    }
    resource = Mock(auth_level=2)
    resource.get_indexsites.return_value = {
        "hdarea.club": {
            "id": "HDarea",
            "name": "高清视界",
            "url": "https://hdarea.club/",
            "public": False,
        }
    }
    resource.get_indexer.side_effect = (
        lambda dom: deepcopy(legacy_merged_entry) if "hdarea.club" in dom else {}
    )
    resource.async_get_indexer = AsyncMock(
        side_effect=lambda dom: deepcopy(legacy_merged_entry) if "hdarea.club" in dom else {}
    )
    resource.get_indexers.return_value = [deepcopy(legacy_merged_entry)]
    resource.async_get_indexers = AsyncMock(return_value=[deepcopy(legacy_merged_entry)])

    oper = Mock()
    oper.list_active.return_value = []
    oper.async_list_active = AsyncMock(return_value=[])
    monkeypatch.setattr(catalog, "ResourceSitesHelper", lambda: resource)
    monkeypatch.setattr(catalog, "SiteOper", lambda: oper)

    helper = catalog.SitesHelper()
    entry = helper.get_indexer("https://hdarea.club/index.php")
    assert entry["id"] == 12
    assert entry["apikey"] == "b" * 32
    assert entry["parser"] == "HDArea"
    assert entry["schema"] == "HDArea"
    assert entry["subtitles"] is True
    assert entry["category"]["movie"] == [{"id": 300, "cat": "Movies/UHD", "desc": "Movies UHD-4K"}]
    assert asyncio.run(helper.async_get_indexer("hdarea.club")) == entry
    assert helper.get_indexers() == [entry]
    assert asyncio.run(helper.async_get_indexers()) == [entry]


def test_sites_helper_override_respects_auth_when_resource_absent(monkeypatch):
    """精简环境下未加载二进制条目时，仍受 auth_level 保护并可在认证后构建配置。"""
    resource = Mock(auth_level=1)
    resource.get_indexsites.return_value = {}
    resource.get_indexer.return_value = {}
    resource.async_get_indexer = AsyncMock(return_value={})
    resource.get_indexers.return_value = []
    resource.async_get_indexers = AsyncMock(return_value=[])

    saved = Site(
        id=15,
        name="高清视界",
        domain="hdarea.club",
        url="https://hdarea.club/",
        apikey="c" * 32,
        is_active=True,
        pri=1,
    )
    oper = Mock()
    oper.get_by_domain.return_value = saved
    oper.async_get_by_domain = AsyncMock(return_value=saved)
    oper.list_active.return_value = [saved]
    oper.async_list_active = AsyncMock(return_value=[saved])

    monkeypatch.setattr(catalog, "ResourceSitesHelper", lambda: resource)
    monkeypatch.setattr(catalog, "SiteOper", lambda: oper)

    helper = catalog.SitesHelper()
    assert helper.get_indexer("hdarea.club") == {}
    assert helper.get_indexers() == []

    resource.auth_level = 2
    built = helper.get_indexer("hdarea.club")
    assert built["id"] == 15
    assert built["parser"] == "HDArea"
    assert built["domain"] == "https://hdarea.club/"
    assert len(built["category"]["movie"]) == 12
    assert helper.get_indexers() == [built]


def test_hdarea_spider_search_params_pagination_and_parsing(monkeypatch):
    """验证 0 基页码、IMDb 精准参数、分类推导、促销因子、标签与异步搜索一致。"""
    captured_calls = []

    def fake_get_res(self, url, params=None, **_kwargs):
        captured_calls.append(("sync", url, deepcopy(params), deepcopy(self._headers)))
        return FakeJsonResponse(_sample_torrents_payload())

    async def fake_async_get_res(self, url, params=None, **_kwargs):
        captured_calls.append(("async", url, deepcopy(params), deepcopy(self._headers)))
        return FakeJsonResponse(_sample_torrents_payload())

    monkeypatch.setattr("app.modules.indexer.spider.hdarea.RequestUtils.get_res", fake_get_res)
    monkeypatch.setattr(
        "app.modules.indexer.spider.hdarea.AsyncRequestUtils.get_res",
        fake_async_get_res,
    )
    monkeypatch.setattr("app.modules.indexer.SitesHelper", lambda: Mock(check=lambda _d: (False, "")))
    monkeypatch.setattr("app.modules.indexer.SiteOper.success", lambda self, **_kw: None)
    monkeypatch.setattr(
        "app.modules.indexer.SiteOper.async_success",
        AsyncMock(return_value=None),
    )

    indexer_module = IndexerModule()
    site = _runtime_indexer()
    assert indexer_module.get_search_page_size(site) == 100

    imdb_results = indexer_module.search_torrents(
        site=site,
        keyword="tt15239678",
        mtype=MediaType.MOVIE,
        page=0,
    )
    assert len(imdb_results) == 2
    first, second = imdb_results
    assert first.title.startswith("Dune Part Two")
    assert first.description == "沙丘2 | 国英双语 | 特效字幕"
    assert first.category == MediaType.MOVIE.value
    assert first.downloadvolumefactor == 0.0
    assert first.uploadvolumefactor == 2.0
    assert first.volume_factor == "2X免费"
    assert first.freedate == "2026-11-12 18:00:00"
    assert first.imdbid == "tt15239678"
    assert first.labels == ["置顶", "中字", "首发", "禁转"]

    assert second.category == MediaType.TV.value
    assert second.downloadvolumefactor == 0.5
    assert second.uploadvolumefactor == 1.0
    assert second.freedate is None

    async_results = asyncio.run(
        indexer_module.async_search_torrents(
            site=site,
            keyword="Dune Part Two",
            mtype=MediaType.TV,
            cat="402,403",
            page=2,
        )
    )
    assert len(async_results) == 2

    _, sync_url, sync_params, sync_headers = captured_calls[0]
    assert sync_url == "https://hdarea.club/api/v1/torrents"
    assert sync_params["page"] == 0
    assert sync_params["per_page"] == 100
    assert sync_params["imdb"] == "tt15239678"
    assert "q" not in sync_params
    assert "300" in sync_params["cat"].split(",")
    assert sync_headers["X-API-Key"] == "a" * 32

    _, _, async_params, _ = captured_calls[1]
    assert async_params["page"] == 2
    assert async_params["q"] == "Dune Part Two"
    assert async_params["cat"] == "402,403"


def test_hdarea_two_stage_download_resolves_short_lived_token_without_cookie(
    monkeypatch,
    tmp_path: Path,
):
    """DownloadChain 应通过 genDlToken 换取短效直链，且下载时不携带过期站点 Cookie。"""
    spider = HDAreaSpider(_runtime_indexer())
    enclosure = spider._build_download_url(51785)
    torrent = TorrentInfo(
        site=12,
        site_name="高清视界",
        site_cookie="stale-cookie-should-be-dropped",
        site_ua="MoviePilot-Test/2.16",
        site_proxy=False,
        title="The Mindy Project S01",
        enclosure=enclosure,
    )

    post_calls = []
    download_calls = []

    def fake_post_res(self, url, params=None, **_kwargs):
        post_calls.append((url, deepcopy(params), deepcopy(self._headers), self._cookies))
        return FakeJsonResponse(
            {
                "success": True,
                "code": 0,
                "data": {
                    "ok": True,
                    "id": 51785,
                    "download_url": "https://hdarea.club/download.php?id=51785&dl_token=fresh-dl-token",
                },
            }
        )

    def fake_download_torrent(self, url, cookie=None, ua=None, proxy=False, cache_invalid=True, **_kw):
        download_calls.append((url, cookie, ua, proxy, cache_invalid))
        return tmp_path / "t.torrent", b"d8:announce4:test4:infod4:name4:testee", "test", ["test.mkv"], ""

    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    monkeypatch.setattr("app.chain.download.RequestUtils.post_res", fake_post_res)
    monkeypatch.setattr("app.chain.download.TorrentHelper.download_torrent", fake_download_torrent)

    content, folder, files = DownloadChain().download_torrent(torrent)
    assert content.startswith(b"d8:announce")
    assert folder == "test"
    assert files == ["test.mkv"]

    assert len(post_calls) == 1
    tok_url, tok_params, tok_headers, tok_cookies = post_calls[0]
    assert tok_url == "https://hdarea.club/api/v1/torrents/genDlToken"
    assert tok_params == {"id": 51785}
    assert tok_headers["X-API-Key"] == "a" * 32
    assert tok_cookies is None

    assert download_calls == [
        (
            "https://hdarea.club/download.php?id=51785&dl_token=fresh-dl-token",
            None,
            "MoviePilot-Test/2.16",
            False,
            False,
        )
    ]


def test_hdarea_subtitle_search_and_detail_subtitle_links(monkeypatch):
    """字幕检索与下载器字幕自动提取均应走 /api/v1/subtitles 接口。"""
    subtitle_payload = {
        "success": True,
        "code": 0,
        "data": {
            "items": [
                {
                    "id": 8193,
                    "torrent_id": 454829,
                    "lang_id": 25,
                    "language": "简体中文",
                    "title": "Dune.Part.Two.2024.chs",
                    "filename": "Dune.Part.Two.2024.chs.ass",
                    "ext": "ass",
                    "size": 100936,
                    "hits": 5,
                    "uploader": "anonymous",
                    "added": "2026-11-07 12:48:26",
                    "download_url": "https://hdarea.club/downloadsubs.php?torrentid=454829&subid=8193&dl_token=sub-tok",
                }
            ],
            "pagination": {"page": 0, "per_page": 100, "total": 1},
        },
    }
    seen_queries = []

    def fake_get_res(self, url, params=None, **_kwargs):
        seen_queries.append((url, deepcopy(params)))
        return FakeJsonResponse(subtitle_payload)

    monkeypatch.setattr("app.modules.indexer.spider.hdarea.RequestUtils.get_res", fake_get_res)
    monkeypatch.setattr("app.modules.indexer.SitesHelper", lambda: Mock(check=lambda _d: (False, "")))
    monkeypatch.setattr("app.modules.indexer.SiteOper.success", lambda self, **_kw: None)

    site = _runtime_indexer()
    subs = IndexerModule().search_subtitles(site=site, keyword="Dune", page=0)
    assert len(subs) == 1
    assert subs[0].language == "简体中文"
    assert subs[0].size == 100936
    assert subs[0].enclosure.endswith("dl_token=sub-tok")
    assert subs[0].page_url == "https://hdarea.club/details.php?id=454829"

    monkeypatch.setattr(
        "app.modules.subtitle.SiteOper.get",
        lambda self, _sid: Site(id=12, domain="hdarea.club", url="https://hdarea.club/"),
    )
    monkeypatch.setattr("app.modules.subtitle.SitesHelper.get_indexer", lambda self, _d: site)
    links = SubtitleModule()._get_subtitle_links(
        TorrentInfo(site=12, page_url="https://hdarea.club/details.php?id=454829&hit=1")
    )
    assert links == [
        "https://hdarea.club/downloadsubs.php?torrentid=454829&subid=8193&dl_token=sub-tok"
    ]
    assert seen_queries[0] == (
        "https://hdarea.club/api/v1/subtitles",
        {"page": 0, "per_page": 100, "q": "Dune"},
    )
    assert seen_queries[1] == (
        "https://hdarea.club/api/v1/subtitles",
        {"torrent_id": "454829"},
    )


def test_hdarea_user_parser_reads_profile_seeding_and_messages(monkeypatch):
    """用户数据解析器应读取个人资料、做种分布与未读消息正文并生成去重标识。"""
    responses = {
        "https://hdarea.club/api/v1/user/profile": {
            "success": True,
            "code": 0,
            "data": {
                "id": 113057,
                "username": "tester",
                "class": 2,
                "class_name": "Power User",
                "added": "2022-09-23 19:40:16",
                "uploaded": 7105150420247,
                "downloaded": 93823162365,
                "share_ratio": 75.729,
                "seedbonus": 5464141.3,
                "seeding_count": 2,
                "seeding_size": 3000,
                "leeching_count": 1,
                "unread_messages_count": 1,
            },
        },
        "https://hdarea.club/api/v1/user/torrents?type=seeding&page=0&per_page=100": {
            "success": True,
            "code": 0,
            "data": {
                "items": [
                    {"id": 1, "size": 1000, "seeders": 5},
                    {"id": 2, "size": 2000, "seeders": 3},
                ],
                "pagination": {"page": 0, "per_page": 100, "has_more": False},
            },
        },
        "https://hdarea.club/api/v1/user/messages?unread=1&page=0&per_page=100": {
            "success": True,
            "code": 0,
            "data": {
                "items": [
                    {
                        "id": 289109,
                        "subject": "系统更新通知",
                        "body_html": "第一行<br /><b>第二行</b>",
                        "added": "2026-11-11 11:26:05",
                        "unread": True,
                    }
                ],
                "pagination": {"page": 0, "per_page": 100, "total": 1},
            },
        },
    }

    parser = HDAreaSiteUserInfo(
        site_name="高清视界",
        url="https://hdarea.club/",
        site_cookie="",
        apikey="d" * 32,
        token="",
    )
    monkeypatch.setattr("app.modules.indexer.parser.settings.SITE_MESSAGE", True)
    monkeypatch.setattr(
        parser,
        "_get_page_content",
        lambda url, **_kw: json.dumps(responses[url], ensure_ascii=False),
    )

    parser.parse()
    assert parser.err_msg is None
    assert parser.userid == "113057"
    assert parser.username == "tester"
    assert parser.user_level == "Power User"
    assert parser.upload == 7105150420247
    assert parser.download == 93823162365
    assert parser.ratio == 75.729
    assert parser.bonus == 5464141.3
    assert parser.seeding == 2
    assert parser.seeding_size == 3000
    assert parser.seeding_info == [[5, 1000], [3, 2000]]
    assert parser.leeching == 1
    assert parser.message_unread == 1
    assert parser.message_unread_contents == [
        (
            "系统更新通知",
            "2026-11-11 11:26:05",
            "第一行\n第二行",
            "hdarea-message:289109",
            "https://hdarea.club/messages.php?action=viewmessage&id=289109",
        )
    ]


def test_hdarea_user_parser_gracefully_handles_missing_user_scope(monkeypatch):
    """子令牌未勾选 user 权限导致站内信返回 403 时，不得污染 err_msg，有 Cookie 时可回退网页读取。"""
    responses = {
        "https://hdarea.club/api/v1/user/profile": {
            "success": True,
            "code": 0,
            "data": {
                "id": 113057,
                "username": "tester",
                "class_name": "VIP",
                "added": "2022-09-23 19:40:16",
                "uploaded": 100,
                "downloaded": 50,
                "seedbonus": 10.0,
                "seeding_count": 0,
                "seeding_size": 0,
                "unread_messages_count": 2,
            },
        },
        "https://hdarea.club/api/v1/user/messages?unread=1&page=0&per_page=100": {
            "success": False,
            "code": 403,
            "http_code": 403,
            "message": "Scope user required",
            "data": None,
        },
    }

    no_cookie_parser = HDAreaSiteUserInfo(
        site_name="高清视界",
        url="https://hdarea.club/",
        site_cookie="",
        apikey="e" * 32,
        token="",
    )
    monkeypatch.setattr("app.modules.indexer.parser.settings.SITE_MESSAGE", True)
    monkeypatch.setattr(
        no_cookie_parser,
        "_get_page_content",
        lambda url, **_kw: json.dumps(responses[url], ensure_ascii=False),
    )
    no_cookie_parser.parse()
    assert no_cookie_parser.err_msg is None
    assert no_cookie_parser.message_unread == 2
    assert no_cookie_parser.message_unread_contents == []

    cookie_parser = HDAreaSiteUserInfo(
        site_name="高清视界",
        url="https://hdarea.club/",
        site_cookie="mock_cookie_present",
        apikey="e" * 32,
        token="",
    )
    monkeypatch.setattr(
        cookie_parser,
        "_get_page_content",
        lambda url, **_kw: json.dumps(responses[url], ensure_ascii=False),
    )
    monkeypatch.setattr(
        NexusPhpSiteUserInfo,
        "_pase_unread_msgs",
        lambda self: self.message_unread_contents.append(("网页回退标题", "2026-11-11 12:00:00", "正文")),
    )
    cookie_parser.parse()
    assert cookie_parser.err_msg is None
    assert cookie_parser.message_unread_contents == [
        ("网页回退标题", "2026-11-11 12:00:00", "正文")
    ]


def test_hdarea_site_connectivity_scope_validation_and_rss(monkeypatch):
    """站点测试应校验 API Key 与 read/download 权限，RSS 辅助应支持 API Key 直链生成与续期。"""
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    chain = SiteChain()
    assert "hdarea.club" in chain.special_site_test

    empty_key_site = Site(domain="hdarea.club", url="https://hdarea.club/", apikey="")
    assert chain.special_site_test["hdarea.club"](empty_key_site) == (False, "请配置站点 API Key")

    status_scopes = ["read"]

    def fake_status_get(self, url, **_kwargs):
        assert url == "https://hdarea.club/api/v1/status"
        return FakeJsonResponse(
            {
                "success": True,
                "code": 0,
                "data": {
                    "authenticated_user": {
                        "id": 113057,
                        "token_scopes": status_scopes,
                    }
                },
            }
        )

    monkeypatch.setattr("app.chain.site.RequestUtils.get_res", fake_status_get)
    keyed_site = Site(domain="hdarea.club", url="https://hdarea.club/", apikey="f" * 32)
    assert chain.special_site_test["hdarea.club"](keyed_site) == (
        False,
        "当前 API 令牌缺少 download 权限",
    )

    status_scopes.append("download")
    assert chain.special_site_test["hdarea.club"](keyed_site) == (True, "连接成功")

    rss_url, err = RssHelper().get_rss_link(
        url="https://hdarea.club/",
        cookie="",
        ua="",
        apikey="f" * 32,
    )
    assert err == ""
    assert rss_url == f"https://hdarea.club/torrentrss.php?rows=50&linktype=dl&apikey={'f' * 32}"

    updated_rss = {}
    monkeypatch.setattr(
        "app.chain.torrents.SiteOper.update_rss",
        lambda self, domain, rss: updated_rss.update({domain: rss}),
    )
    TorrentsChain()._TorrentsChain__renew_rss_url(
        "hdarea.club",
        {
            "url": "https://hdarea.club/",
            "apikey": "1" * 32,
            "rss": f"https://hdarea.club/torrentrss.php?rows=50&ifree=1&apikey={'0' * 32}",
        },
    )
    assert updated_rss["hdarea.club"] == (
        f"https://hdarea.club/torrentrss.php?rows=50&ifree=1&apikey={'1' * 32}"
    )


def test_hdarea_spider_retries_once_on_http_429(monkeypatch):
    """触发 HTTP 429 频率限制时应按夹紧后的 Retry-After 等待并自动重试一次。"""
    slept_sync = []
    slept_async = []
    sync_attempts = {"count": 0}
    async_attempts = {"count": 0}

    def fake_sync_get(self, url, params=None, **_kwargs):
        sync_attempts["count"] += 1
        if sync_attempts["count"] == 1:
            return FakeJsonResponse(
                {"code": 429, "message": "Rate limit exceeded"},
                status_code=429,
                reason="Too Many Requests",
                headers={"Retry-After": "12"},
            )
        return FakeJsonResponse(_sample_torrents_payload())

    async def fake_async_get(self, url, params=None, **_kwargs):
        async_attempts["count"] += 1
        if async_attempts["count"] == 1:
            return FakeJsonResponse(
                {"code": 429, "message": "Rate limit exceeded"},
                status_code=429,
                reason="Too Many Requests",
                headers={"Retry-After": "1.5"},
            )
        return FakeJsonResponse(_sample_torrents_payload())

    async def record_async_sleep(delay: float):
        slept_async.append(delay)

    monkeypatch.setattr("app.modules.indexer.spider.hdarea.RequestUtils.get_res", fake_sync_get)
    monkeypatch.setattr("app.modules.indexer.spider.hdarea.AsyncRequestUtils.get_res", fake_async_get)
    monkeypatch.setattr("app.modules.indexer.spider.hdarea.time.sleep", slept_sync.append)
    monkeypatch.setattr("app.modules.indexer.spider.hdarea.asyncio.sleep", record_async_sleep)

    spider = HDAreaSpider(_runtime_indexer())
    err_sync, items_sync = spider.search(keyword="Dune")
    assert err_sync is False
    assert len(items_sync) == 2
    assert sync_attempts["count"] == 2
    assert slept_sync == [5.0]

    err_async, items_async = asyncio.run(spider.async_search(keyword="Dune"))
    assert err_async is False
    assert len(items_async) == 2
    assert async_attempts["count"] == 2
    assert slept_async == [1.5]


def test_site_favicon_fallback_and_api_only_cloudflare_protection(monkeypatch):
    """首页受限时仍应尝试默认 favicon.ico；纯 API 站点受盾拦截时不覆盖已有缓存图标且不抛错。"""
    from app.core.event import Event
    from app.schemas.types import EventType

    class RawResp:
        def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
            self.status_code = status_code
            self.content = content
            self.text = text

        def __bool__(self) -> bool:
            return self.status_code < 400

    def home_403_icon_200(self, url, **_kw):
        if url.endswith("favicon.ico"):
            return RawResp(200, content=b"\x89PNG\r\n\x1a\nPNGDATA")
        return RawResp(403, content=b"Forbidden", text="Forbidden")

    monkeypatch.setattr("app.chain.site.RequestUtils.get_res", home_403_icon_200)
    icon_url, icon_b64 = SiteChain._SiteChain__parse_favicon(
        url="https://api-site.example/",
        cookie="",
        ua="MP",
        api_only=True,
    )
    assert icon_url == "https://api-site.example/favicon.ico"
    assert icon_b64 is not None

    monkeypatch.setattr(
        "app.chain.site.RequestUtils.get_res",
        lambda self, url, **_kw: RawResp(403, content=b"<!DOCTYPE html><html>CF</html>", text="CF"),
    )
    blocked_url, blocked_b64 = SiteChain._SiteChain__parse_favicon(
        url="https://hdarea.club/",
        cookie="",
        ua="MP",
        api_only=True,
    )
    assert blocked_url is None
    assert blocked_b64 is None

    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    update_calls = []
    oper = Mock()
    oper.get_by_domain.return_value = Site(
        id=12,
        name="高清视界",
        domain="hdarea.club",
        url="https://hdarea.club/",
        apikey="a" * 32,
        cookie="",
    )
    oper.get_icon_by_domain.return_value = Mock(base64="data:image/png;base64,EXISTING")
    oper.update_icon.side_effect = lambda **kw: update_calls.append(kw)
    monkeypatch.setattr("app.chain.site.SiteOper", lambda: oper)
    monkeypatch.setattr(
        "app.chain.site.SitesHelper",
        lambda: Mock(get_indexer=lambda _d: {"name": "高清视界", "domain": "https://hdarea.club/"}),
    )
    SiteChain().cache_site_icon(Event(EventType.SiteUpdated, {"domain": "hdarea.club"}))
    assert update_calls == []


def test_download_chain_surfaces_two_stage_api_error_reason(monkeypatch):
    """两段式换取下载凭证返回 403 权限不足时，应将具体 API 错误透传至通知正文。"""
    spider = HDAreaSpider(_runtime_indexer())
    torrent = TorrentInfo(
        site=12,
        site_name="高清视界",
        title="Dune Part Two 2024",
        enclosure=spider._build_download_url(434799),
    )

    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    monkeypatch.setattr(
        "app.chain.download.RequestUtils.post_res",
        lambda self, url, **_kw: FakeJsonResponse(
            {"success": False, "code": 403, "message": "Insufficient scope: download required"},
            status_code=403,
            reason="Forbidden",
        ),
    )
    posted_notifications = []
    chain = DownloadChain()
    monkeypatch.setattr(chain, "post_message", posted_notifications.append)

    content, folder, files = chain.download_torrent(torrent)
    assert content is None
    assert folder == ""
    assert files == []
    assert len(posted_notifications) == 1
    assert "HTTP 403" in posted_notifications[0].text
    assert "Insufficient scope: download required" in posted_notifications[0].text


@pytest.mark.parametrize("use_rust_path", [True, False])
def test_rss_helper_and_torrents_chain_parse_extended_attrs_and_ifree_prefix(
    monkeypatch,
    use_rust_path: bool,
):
    """Rust 与 Python 双路径均应解析 torznab:attr、promotion_until 并剥离 ifree 促销/分类前缀。"""
    rss_xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed" xmlns:nexusphp="https://nexusphp.org/ns">
  <channel>
    <item>
      <title>[2X免费][Movies Blu-ray] Dune.Part.Two.2024.2160p.UHD.BluRay.HEVC-HDArea</title>
      <link>https://hdarea.club/details.php?id=12345</link>
      <pubDate>Wed, 11 Nov 2026 15:30:00 +0800</pubDate>
      <enclosure url="https://hdarea.club/download.php?id=12345&amp;apikey=test" length="78451204096" type="application/x-bittorrent" />
      <torznab:attr name="size" value="78451204096" />
      <torznab:attr name="seeders" value="42" />
      <torznab:attr name="leechers" value="5" />
      <torznab:attr name="imdb" value="15239678" />
      <torznab:attr name="downloadvolumefactor" value="0" />
      <torznab:attr name="uploadvolumefactor" value="2" />
      <nexusphp:promotion_until>2026-11-12 18:00:00</nexusphp:promotion_until>
    </item>
    <item>
      <title>[ANi] Frieren S01E01 1080p WEB-DL AAC CHS</title>
      <link>https://hdarea.club/details.php?id=12346</link>
      <enclosure url="https://hdarea.club/download.php?id=12346&amp;apikey=test" length="524288000" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="10" />
      <torznab:attr name="downloadvolumefactor" value="1" />
      <torznab:attr name="uploadvolumefactor" value="1" />
    </item>
  </channel>
</rss>"""

    class FakeXmlResp:
        status_code = 200
        content = rss_xml.encode("utf-8")
        text = rss_xml

    monkeypatch.setattr("app.helper.rss.RequestUtils.get_res", lambda self, url, **_kw: FakeXmlResp())
    monkeypatch.setattr(
        "app.helper.rss.RequestUtils.get_decoded_xml_content",
        staticmethod(lambda *_a, **_kw: rss_xml),
    )
    if not use_rust_path:
        monkeypatch.setattr("app.helper.rss.rust_accel.parse_rss_items", lambda *_a: None)

    rss_url = "https://hdarea.club/torrentrss.php?rows=50&linktype=dl&ifree=1&apikey=test"
    site_dict = {
        "id": 12,
        "name": "高清视界",
        "url": "https://hdarea.club/",
        "rss": rss_url,
        "public": False,
        "pri": 1,
    }
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    monkeypatch.setattr(
        "app.chain.torrents.SitesHelper",
        lambda: Mock(get_indexer=lambda _d: site_dict),
    )

    torrents = TorrentsChain().rss("hdarea.club")
    assert len(torrents) == 2
    first, second = torrents
    assert first.title == "Dune.Part.Two.2024.2160p.UHD.BluRay.HEVC-HDArea"
    assert first.seeders == 42
    assert first.peers == 5
    assert first.downloadvolumefactor == 0.0
    assert first.uploadvolumefactor == 2.0
    assert first.volume_factor == "2X免费"
    assert first.freedate == "2026-11-12 18:00:00"
    assert first.imdbid == "tt15239678"

    assert second.title == "[ANi] Frieren S01E01 1080p WEB-DL AAC CHS"
    assert second.seeders == 10
    assert second.volume_factor == "普通"
