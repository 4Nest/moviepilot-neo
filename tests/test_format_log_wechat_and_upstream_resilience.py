"""E00 保留、AST 安全偏移求值、文件日志异常堆栈、企业微信空标题及上游韧性回归测试。"""

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app.chain.media import MediaChain
from app.chain.torrents import TorrentsChain, reset_rss_renew_state_for_tests
from app.core.cache import AsyncFileCache, FileCache
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.format import FormatParser, calculate_episode_offset, safe_eval_arithmetic
from app.helper.image import ImageHelper
from app.helper.plugin import PluginHelper
from app.helper.rss import RssHelper, is_rss_document, is_rss_expired_text
from app.log import LogEntry, LoggerManager, NonBlockingFileHandler, _normalize_exc_info
from app.modules.anilist.anilist import AniListApi
from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.transmission.transmission import Transmission
from app.modules.wechat.wechat import WeChat
from app.schemas.types import MediaType


def _make_valid_png_bytes() -> bytes:
    """生成可通过 Pillow verify() 校验的合法 1x1 PNG 字节。"""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def test_format_parser_preserves_episode_zero_and_ast_safe_eval():
    """FormatParser 与算术求值必须完整保留 E00 并拦截任意非算术代码执行。"""
    assert safe_eval_arithmetic("0 + 0") == 0
    assert safe_eval_arithmetic("(20 - 12) * 2 // 2") == 8
    assert calculate_episode_offset("EP+0", 0) == 0
    assert calculate_episode_offset("2*EP-12", 12) == 12

    for malicious in (
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd')",
        "[x for x in (1, 2)]",
        "1; import os",
        "(lambda: 1)()",
        "a.b",
    ):
        with pytest.raises(ValueError):
            safe_eval_arithmetic(malicious)

    parser = FormatParser(
        eformat="{prefix}S01E{ep}{suffix}",
        offset="EP+0",
        part=None,
    )
    assert parser.match("Show.S01E00.1080p.mkv") is True
    start_ep, end_ep, part = parser.split_episode(
        "Show.S01E00.1080p.mkv",
        MetaInfo("Show.S01E00.1080p.mkv"),
    )
    assert start_ep == 0
    assert end_ep is None
    assert part is None


def test_media_chain_handle_tv_episode_file_preserves_episode_zero(tmp_path):
    """MediaChain._handle_tv_episode_file 遇到 E00 特别篇文件时不得误判为无集数跳过。"""
    ep0_path = tmp_path / "Show.S01E00.mkv"
    ep0_path.write_bytes(b"0")

    chain = object.__new__(MediaChain)
    recognized_media = MediaInfo()
    recognized_media.type = MediaType.TV
    recognized_media.tmdb_id = 999
    chain.recognize_media = lambda **_kw: recognized_media

    scraped_episodes = []
    chain._scrape_nfo_generic = lambda **kw: scraped_episodes.append(("nfo", kw["episode_number"]))
    chain._scrape_images_generic = lambda **kw: scraped_episodes.append(("img", kw["episode_number"]))

    chain._handle_tv_episode_file(
        fileitem=SimpleNamespace(path=str(ep0_path)),
        filepath=ep0_path,
        mediainfo=recognized_media,
        parent=SimpleNamespace(path=str(tmp_path)),
        overwrite=True,
    )
    assert scraped_episodes == [("nfo", 0), ("img", 0)]


def test_non_blocking_file_handler_writes_exception_traceback_sync_and_batch(tmp_path):
    """LoggerManager 与 NonBlockingFileHandler 在同步与批量落盘路径下均应保留完整异常堆栈。"""
    sync_log = tmp_path / "sync_trace.log"
    batch_log = tmp_path / "batch_trace.log"
    handler = NonBlockingFileHandler()
    mgr = LoggerManager()

    try:
        raise RuntimeError("sync-boom-marker")
    except RuntimeError:
        handler.write_log(
            "ERROR",
            "同步日志错误",
            sync_log,
            exc_info=_normalize_exc_info(True),
        )

    try:
        raise ValueError("batch-boom-marker")
    except ValueError:
        entry = LogEntry(
            "ERROR",
            "异步批量错误",
            batch_log,
            exc_info=_normalize_exc_info(True),
        )
        handler._write_batch([entry])

    sync_text = sync_log.read_text("utf-8")
    assert "RuntimeError: sync-boom-marker" in sync_text
    assert "Traceback (most recent call last):" in sync_text

    batch_text = batch_log.read_text("utf-8")
    assert "ValueError: batch-boom-marker" in batch_text
    assert "Traceback (most recent call last):" in batch_text
    assert callable(mgr.exception)


def test_wechat_send_message_omits_empty_or_none_title():
    """企业微信发送纯文本消息且 title 为空或 None 时，正文不得出现 'None\\n' 前缀。"""
    wx = object.__new__(WeChat)
    wx._appid = 1000001
    wx._send_msg_url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    sent_payloads = []
    wx._WeChat__split_content = lambda content: [content]
    wx._WeChat__post_request = lambda _url, payload: (sent_payloads.append(payload) or True)

    assert wx._WeChat__send_message(title=None, text="订阅已完成") is True
    assert wx._WeChat__send_message(title="", text="第二条通知") is True
    assert sent_payloads[0]["text"]["content"] == "订阅已完成"
    assert sent_payloads[1]["text"]["content"] == "第二条通知"


def test_rss_html_login_detection_and_safe_verified_renewal(monkeypatch):
    """RSS 返回 HTML 登录页时应触发续期，新链接须验证可解析后才写库，失败时节流退避。"""
    reset_rss_renew_state_for_tests()
    login_html = "<html><body><form action='/takelogin.php'><input type='password' name='password'/></form></body></html>"
    valid_rss = '<?xml version="1.0"?><rss version="2.0"><channel><item><title>Ep01</title><enclosure url="https://pt.org/dl/1.torrent" length="1024"/></item></channel></rss>'

    assert is_rss_expired_text(login_html) is True
    assert is_rss_document(valid_rss) is True
    assert is_rss_expired_text(valid_rss) is False

    class LoginResp:
        """模拟过期重定向响应。"""

        status_code = 403
        content = login_html.encode("utf-8")
        text = login_html + "<!-- rss expired -->"

    monkeypatch.setattr("app.helper.rss.RequestUtils.get_res", lambda *_a, **_kw: LoginResp())
    assert RssHelper().parse("https://pt.org/torrentrss.php?passkey=old") is None

    site_cfg = {
        "id": 9,
        "name": "测试PT",
        "domain": "pt.org",
        "url": "https://pt.org/",
        "rss": "https://pt.org/torrentrss.php?rows=50&passkey=old_key",
        "cookie": "uid=1",
        "public": False,
    }
    saved_rss = {}
    alerts = []
    get_link_calls = {"n": 0}

    chain = object.__new__(TorrentsChain)
    chain.post_message = alerts.append
    monkeypatch.setattr("app.chain.torrents.SitesHelper.get_indexer", lambda self, _d: site_cfg)
    monkeypatch.setattr(
        "app.chain.torrents.SiteOper.update_rss",
        lambda self, domain, rss: saved_rss.update({domain: rss}),
    )

    def fake_get_rss_link(*_a, **_kw):
        get_link_calls["n"] += 1
        return "https://pt.org/torrentrss.php?rows=50&passkey=bad_new_key", ""

    monkeypatch.setattr("app.chain.torrents.RssHelper.get_rss_link", fake_get_rss_link)
    monkeypatch.setattr(
        "app.chain.torrents.RssHelper.parse",
        lambda self, url, *_a, **_kw: None if "old_key" in url else False,
    )

    assert chain.rss("pt.org") == []
    assert saved_rss == {}
    assert len(alerts) == 1
    assert get_link_calls["n"] == 1

    # 退避冷却期内再次刷新不会重复请求生成页或重复推送通知
    assert chain.rss("pt.org") == []
    assert get_link_calls["n"] == 1
    assert len(alerts) == 1

    # 清理退避后，新链接验证通过则立即落盘并在同轮直接返回解析出的种子
    reset_rss_renew_state_for_tests()
    monkeypatch.setattr(
        "app.chain.torrents.RssHelper.get_rss_link",
        lambda *_a, **_kw: ("https://pt.org/torrentrss.php?rows=50&passkey=good_key", ""),
    )
    monkeypatch.setattr(
        "app.chain.torrents.RssHelper.parse",
        lambda self, url, *_a, **_kw: (
            None
            if "old_key" in url
            else [{"title": "Verified.Show.S01E01", "enclosure": "https://pt.org/1.torrent", "size": 100}]
        ),
    )
    recovered = chain.rss("pt.org")
    assert saved_rss["pt.org"] == "https://pt.org/torrentrss.php?rows=50&passkey=good_key"
    assert len(recovered) == 1
    assert recovered[0].title == "Verified.Show.S01E01"


def test_image_helper_prefers_github_proxy_and_plugin_negative_cache(monkeypatch):
    """ImageHelper 优先通过 GITHUB_PROXY 拉取 GitHub 图标；PluginHelper 对失败仓库写入 5 分钟负缓存。"""
    monkeypatch.setattr(settings, "GITHUB_PROXY", "https://gh-mirror.example/")
    raw_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/test.png"
    png_bytes = _make_valid_png_bytes()

    called_urls = []

    def fake_get_res(self, url, **_kw):
        called_urls.append(url)
        if url.startswith("https://gh-mirror.example/"):
            return SimpleNamespace(status_code=502, content=b"")
        return SimpleNamespace(status_code=200, content=png_bytes)

    monkeypatch.setattr("app.helper.image.RequestUtils.get_res", fake_get_res)
    helper = ImageHelper()
    result = helper.fetch_image(raw_icon, use_cache=False)
    assert result == png_bytes
    assert called_urls[0].startswith("https://gh-mirror.example/https://raw.githubusercontent.com/")
    assert called_urls[1] == raw_icon

    # 插件仓库远端请求失败后，短期内重复获取直接命中负缓存不再发网
    PluginHelper.clear_repo_failures()
    repo_calls = {"n": 0}

    def failing_repo_get(self, url, **_kw):
        repo_calls["n"] += 1
        return None

    monkeypatch.setattr("app.helper.plugin.is_fresh", lambda *_a, **_kw: False)
    monkeypatch.setattr("app.helper.plugin.RequestUtils.get_res", failing_repo_get)
    plugin_helper = PluginHelper()
    repo_url = "https://github.com/example-org/unreachable-plugin-repo"
    assert plugin_helper.get_plugins(repo_url) is None
    assert plugin_helper.get_plugins(repo_url) is None
    assert repo_calls["n"] == 1


def test_transmission_reconnect_on_transient_rpc_error():
    """Transmission 遇到瞬时 RPC 连接异常时应自动重连一次并重试成功。"""
    tr = object.__new__(Transmission)
    tr._host = "127.0.0.1"
    tr._port = 9091
    tr._trarg = ["id", "name", "hashString", "status", "labels"]
    first_client = Mock()
    first_client.get_torrents.side_effect = ConnectionError("rpc session expired")
    second_client = Mock()
    expected_torrent = SimpleNamespace(hashString="ok_after_reconnect", status="downloading", labels=[])
    second_client.get_torrents.return_value = [expected_torrent]

    tr.trc = first_client
    reconnected = {"count": 0}

    def fake_reconnect():
        reconnected["count"] += 1
        tr.trc = second_client

    tr.reconnect = fake_reconnect
    torrents, has_err = tr.get_torrents()
    assert has_err is False
    assert torrents == [expected_torrent]
    assert reconnected["count"] == 1


def test_emby_and_jellyfin_get_items_paginate_by_200(monkeypatch):
    """Emby 与 Jellyfin 在 limit == -1 全量同步时应按 200 条/页循环分页拉取。"""
    total_items = [
        {"Id": f"item-{idx}", "Name": f"Movie {idx}", "Type": "Movie", "ProviderIds": {"Tmdb": str(idx)}}
        for idx in range(450)
    ]

    def make_paged_getter(seen_requests):
        def _fake_get_res(self, url, params=None, **_kw):
            start = int((params or {}).get("StartIndex", 0))
            limit = int((params or {}).get("Limit", 0))
            seen_requests.append((start, limit))
            batch = total_items[start : start + limit]
            resp = Mock(status_code=200)
            resp.json.return_value = {"Items": batch, "TotalRecordCount": len(total_items)}
            return resp

        return _fake_get_res

    emby_calls = []
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils.get_res", make_paged_getter(emby_calls))
    emby = object.__new__(Emby)
    emby._host = "http://emby.local/"
    emby._apikey = "k"
    emby.user = "u1"
    emby_items = list(emby.get_items(parent="lib1", limit=-1))
    assert len(emby_items) == 450
    assert emby_calls == [(0, 200), (200, 200), (400, 200)]

    jf_calls = []
    jf = object.__new__(Jellyfin)
    jf._host = "http://jf.local/"
    jf._apikey = "k"
    jf.user = "u1"
    jf._request = lambda: SimpleNamespace(
        get_res=lambda url, params=None, **_kw: make_paged_getter(jf_calls)(None, url, params)
    )
    jf_items = list(jf.get_items(parent="lib1", limit=-1))
    assert len(jf_items) == 450
    assert jf_calls == [(0, 200), (200, 200), (400, 200)]


def test_anilist_uses_official_graphql_and_persists_chinese_cache(monkeypatch, tmp_path):
    """AniListApi 应直连官方 GraphQL 端点，并对中文标题映射持久化缓存及失败保留旧译名退避。"""
    monkeypatch.setattr(settings, "GITHUB_PROXY", "https://gh-proxy.test/")

    api = AniListApi()
    api._file_cache = FileCache(base=tmp_path)
    api._async_file_cache = AsyncFileCache(base=tmp_path)

    graphql_urls = []
    api._request = SimpleNamespace(
        post_res=lambda url, **_kw: (
            graphql_urls.append(url)
            or SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": {
                        "Media": {
                            "id": 154587,
                            "title": {"romaji": "Sousou no Frieren", "english": "Frieren", "native": "葬送のフリーレン"},
                        }
                    }
                },
            )
        )
    )

    fetched_translation_urls = []
    translation_payload = [{"id": 154587, "title": "葬送的芙莉莲"}]
    monkeypatch.setattr(
        "app.modules.anilist.anilist.RequestUtils.get_json",
        lambda self, url, **_kw: (fetched_translation_urls.append(url) or translation_payload),
    )

    data = api._invoke("query { Media { id } }", {"id": 154587})
    assert graphql_urls == ["https://graphql.anilist.co"]
    assert data["Media"]["id"] == 154587

    translations = api._translation_map()
    assert translations[154587]["title"] == "葬送的芙莉莲"
    assert fetched_translation_urls[0].startswith("https://gh-proxy.test/https://raw.githubusercontent.com/")

    # 新建实例复用同一持久化目录时应直接命中磁盘 FileCache，无需再次发起外网下载
    api2 = AniListApi()
    api2._file_cache = FileCache(base=tmp_path)
    api2._async_file_cache = AsyncFileCache(base=tmp_path)
    monkeypatch.setattr(
        "app.modules.anilist.anilist.RequestUtils.get_json",
        lambda *_a, **_kw: pytest.fail("命中有效磁盘缓存时不应请求远端"),
    )
    assert api2._translation_map()[154587]["title"] == "葬送的芙莉莲"

    # 缓存过期且远端刷新失败时，应保留旧译名并写入 5 分钟失败退避
    api2._translations_expires_at = 0.0
    api2._file_cache.delete(api2._translations_key, region=api2._translations_region)
    fail_calls = {"n": 0}

    def fail_get_json(_self, _url, **_kw):
        fail_calls["n"] += 1
        return None

    api2._request = SimpleNamespace(get_json=lambda url, **_kw: fail_get_json(None, url, **_kw))
    monkeypatch.setattr("app.modules.anilist.anilist.RequestUtils.get_json", fail_get_json)
    stale = api2._translation_map()
    assert stale[154587]["title"] == "葬送的芙莉莲"
    # 镜像路由 + 原地址共尝试 2 次，之后进入 5 分钟退避不再触发请求
    assert fail_calls["n"] == 2
    assert api2._translation_map()[154587]["title"] == "葬送的芙莉莲"
    assert fail_calls["n"] == 2
