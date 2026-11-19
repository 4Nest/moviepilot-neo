"""
媒体库查询失败不得退化为整季缺失的离线回归测试（适配上游 PR #7445 / #7453）。

覆盖范围：
- Emby / Jellyfin 标题搜索、缓存回退搜索、详情校验、分集接口的无响应/超时/HTTP 错误/非法结构；
- Plex 未连接、条目抓取异常、搜索异常、分集读取异常，以及正常 NotFound 空结果；
- 多服务器合并查询中任一服务器失败时严格模式报错、宽松模式保留既有语义；
- 订阅刷新进度、搜索、RSS 刷新与故障恢复全链路，确保失败时跳过本轮并保留原进度与下载事实。
"""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plexapi.exceptions import NotFound
from requests import Response, Timeout

from app.chain import download as download_module
from app.chain import subscribe as subscribe_module
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.metainfo import MetaInfo
from app.db.models.subscribe import Subscribe
from app.modules.emby import EmbyModule
from app.modules.emby import emby as emby_module
from app.modules.jellyfin import JellyfinModule
from app.modules.jellyfin import jellyfin as jellyfin_module
from app.modules.plex import PlexModule
from app.modules.plex import plex as plex_module
from app.schemas import ExistMediaInfo
from app.schemas.types import MediaType


def _query_error_type():
    """按需引入 MediaServerQueryError，避免 fail-first 阶段被顶层 ImportError 掩盖。"""
    from app.schemas.exception import MediaServerQueryError

    return MediaServerQueryError


def _response(payload=None, status: int = 200) -> Response:
    """构造保留真实 HTTP 状态与 close 语义的离线响应。"""
    response = Response()
    response.status_code = status
    response._content = b""
    response._content_consumed = True
    response.json = Mock(return_value=payload)
    return response


def _media_info():
    """构造单季 200 集的电视剧媒体信息。"""
    return SimpleNamespace(
        type=MediaType.TV,
        tmdb_id=12345,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
        title="示例剧集",
        original_title="Example Show",
        year="2023",
        season=1,
        title_year="示例剧集 (2023)",
        source="themoviedb",
        media_id="12345",
        seasons={1: list(range(1, 201))},
        episode_group=None,
    )


def _subscribe(**overrides) -> Subscribe:
    """构造普通电视剧订阅对象。"""
    data = {
        "id": 7444,
        "name": "示例剧集",
        "year": "2023",
        "type": MediaType.TV.value,
        "tmdbid": 12345,
        "media_source": "themoviedb",
        "media_id": "12345",
        "season": 1,
        "start_episode": 1,
        "total_episode": 200,
        "manual_total_episode": 0,
        "lack_episode": 33,
        "note": list(range(157, 168)),
        "state": "R",
        "best_version": 0,
        "best_version_full": 0,
        "skip_library_check": 0,
        "episode_priority": {},
        "version_rules": None,
        "version_progress": None,
    }
    data.update(overrides)
    return Subscribe(**data)


def _download_chain(monkeypatch, modules, *, server_names=None, cached_item_id="cached-series"):
    """构造仅隔离模块表、服务器配置与持久化出口的 DownloadChain。"""
    chain = object.__new__(DownloadChain)
    monkeypatch.setattr(
        chain,
        "modulemanager",
        SimpleNamespace(get_running_modules=lambda _method: modules),
        raising=False,
    )
    monkeypatch.setattr(
        chain,
        "pluginmanager",
        SimpleNamespace(get_plugin_modules=lambda: {}),
        raising=False,
    )
    mediaserver = Mock()
    mediaserver.get_item_id.return_value = cached_item_id
    monkeypatch.setattr(download_module, "MediaServerOper", lambda: mediaserver)
    if server_names is not None:
        monkeypatch.setattr(
            download_module.DownloadChain,
            "_get_mediaserver_names",
            staticmethod(lambda: list(server_names)),
            raising=False,
        )
    return chain, mediaserver


@pytest.fixture(
    params=[
        (emby_module.Emby, EmbyModule, emby_module),
        (jellyfin_module.Jellyfin, JellyfinModule, jellyfin_module),
    ],
    ids=["emby", "jellyfin"],
)
def emby_family_query(request, monkeypatch):
    """连接真实 Emby/Jellyfin 客户端、模块与 DownloadChain，仅替换 HTTP 和持久化。"""
    client_type, module_type, client_mod = request.param
    client = object.__new__(client_type)
    client._host = "https://media.invalid/"
    client._apikey = "synthetic-key"
    client.user = "test-user"
    client._sync_libraries = []
    transport = Mock()
    monkeypatch.setattr(client_mod, "RequestUtils", Mock(return_value=transport))
    if hasattr(client, "_request"):
        monkeypatch.setattr(client, "_request", lambda: transport)
    module = object.__new__(module_type)
    monkeypatch.setattr(module, "get_instances", lambda: {"main": client})
    monkeypatch.setattr(module, "get_instance", lambda name: client if name == "main" else None)
    chain, mediaserver = _download_chain(monkeypatch, [module], server_names=["main"])
    return chain, transport, client, mediaserver, _media_info()


@pytest.mark.parametrize("phase", ["search", "uncached-search", "detail", "episodes"])
@pytest.mark.parametrize("failure", ["timeout", "no_response", "http", "malformed", "wrong-items"])
def test_emby_and_jellyfin_query_failure_raises_in_strict_mode(emby_family_query, phase, failure):
    """标题搜索、详情或分集查询失败在严格模式下必须抛 MediaServerQueryError，不得污染 no_exists。"""
    chain, transport, _client, mediaserver, media = emby_family_query
    if failure == "timeout":
        failed_action = Timeout("offline timeout")
    else:
        failed_action = {
            "no_response": None,
            "http": _response({"Items": []}, status=503),
            "malformed": _response({}),
            "wrong-items": _response({"Items": {}}),
        }[failure]

    series = {
        "Id": "series-1",
        "Type": "Series",
        "Name": media.title,
        "ProductionYear": 2023,
        "ProviderIds": {"Tmdb": "12345"},
    }

    def _apply_side_effect(steps):
        def _side_effect(*_args, **_kwargs):
            current = steps.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

        transport.get_res.side_effect = _side_effect

    if phase == "uncached-search":
        mediaserver.get_item_id.return_value = None
        _apply_side_effect([failed_action])
    elif phase == "search":
        # 缓存 itemid 查详情返回 404（失效），回退标题搜索失败
        _apply_side_effect([_response({}, status=404), failed_action])
    elif phase == "detail":
        # 缓存详情 404 -> 标题搜索命中 -> 新条目详情读取失败
        detail_failure = failed_action if failure in ("timeout", "no_response", "http") else _response("bad-json", status=200)
        _apply_side_effect([_response({}, status=404), _response({"Items": [series]}), detail_failure])
    else:
        # 缓存详情成功 -> 分集查询失败
        _apply_side_effect([_response(series), failed_action])

    previous = {"tmdb:other": {}}
    with pytest.raises(_query_error_type(), match="媒体库.*查询失败"):
        chain.get_no_exists_info(
            MetaInfo("示例剧集 S01"),
            media,
            no_exists=previous,
            raise_on_query_failure=True,
        )

    assert previous == {"tmdb:other": {}}


def test_emby_and_jellyfin_confirmed_absence_still_produces_missing(emby_family_query):
    """成功返回 HTTP 200 且 Items=[] 的确认未入库，严格模式下仍正常返回整季缺失。"""
    chain, transport, _client, mediaserver, media = emby_family_query
    mediaserver.get_item_id.return_value = None
    transport.get_res.return_value = _response({"Items": []})

    exists, missing = chain.get_no_exists_info(
        MetaInfo("示例剧集 S01"),
        media,
        raise_on_query_failure=True,
    )

    assert exists is False
    assert missing["tmdb:12345"][1].episodes == []
    assert missing["tmdb:12345"][1].total_episode == 200


def test_emby_and_jellyfin_unconfigured_client_raises_in_strict_mode(emby_family_query):
    """配置缺失或连接未就绪时，严格模式不能放行整季补缺。"""
    chain, _transport, client, _mediaserver, media = emby_family_query
    client._host = None

    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(
            MetaInfo("示例剧集 S01"),
            media,
            raise_on_query_failure=True,
        )


def test_default_lenient_mode_preserves_error_isolation(emby_family_query):
    """默认宽松模式不抛异常，严格模式抛 MediaServerQueryError。"""
    chain, transport, _client, _mediaserver, media = emby_family_query
    transport.get_res.return_value = None

    lenient_result = chain.media_exists(media, server="main")
    assert not lenient_result

    with pytest.raises(_query_error_type()):
        chain.media_exists(media, server="main", raise_on_query_failure=True)


def test_plex_query_failures_and_confirmed_absence(monkeypatch):
    """Plex 未连接、抓取异常、搜索异常、分集读取异常均在严格模式下抛异常；NotFound+空搜索仍视为缺失。"""
    media = _media_info()

    def _build_plex_chain(plex_obj):
        client = object.__new__(plex_module.Plex)
        client._host = "http://plex.invalid:32400/"
        client._playhost = None
        client._token = "token"
        client._plex = plex_obj
        module = object.__new__(PlexModule)
        monkeypatch.setattr(module, "get_instances", lambda: {"plex-main": client})
        monkeypatch.setattr(module, "get_instance", lambda name: client if name == "plex-main" else None)
        return _download_chain(monkeypatch, [module], server_names=["plex-main"])[0], client

    # 1. 未连接
    chain, _ = _build_plex_chain(None)
    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(MetaInfo("示例剧集 S01"), media, raise_on_query_failure=True)

    # 2. fetchItem 抛网络超时（非 NotFound）
    plex_mock = Mock()
    plex_mock.fetchItem.side_effect = Timeout("plex timeout")
    chain, _ = _build_plex_chain(plex_mock)
    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(MetaInfo("示例剧集 S01"), media, raise_on_query_failure=True)

    # 3. NotFound 后搜索抛异常
    plex_mock = Mock()
    plex_mock.fetchItem.side_effect = NotFound("stale")
    plex_mock.library.search.side_effect = RuntimeError("plex 503")
    chain, _ = _build_plex_chain(plex_mock)
    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(MetaInfo("示例剧集 S01"), media, raise_on_query_failure=True)

    # 4. episodes() 抛异常
    show = Mock()
    show.key = "/library/metadata/100"
    show.guids = [{"id": "tmdb://12345"}]
    show.episodes.side_effect = Timeout("episodes timeout")
    plex_mock = Mock()
    plex_mock.fetchItem.return_value = show
    chain, _ = _build_plex_chain(plex_mock)
    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(MetaInfo("示例剧集 S01"), media, raise_on_query_failure=True)

    # 5. 正常 NotFound + 搜索返回空列表 -> 确认未入库，正常产生缺失
    plex_mock = Mock()
    plex_mock.fetchItem.side_effect = NotFound("stale")
    plex_mock.library.search.return_value = []
    chain, _ = _build_plex_chain(plex_mock)
    exists, missing = chain.get_no_exists_info(
        MetaInfo("示例剧集 S01"),
        media,
        raise_on_query_failure=True,
    )
    assert exists is False
    assert missing["tmdb:12345"][1].episodes == []
    assert missing["tmdb:12345"][1].total_episode == 200


@pytest.mark.parametrize("failed_first", [False, True])
def test_multi_server_partial_failure_raises_in_strict_mode(monkeypatch, failed_first):
    """多服务器合并查询时，任一服务器失败在严格模式下不得用另一台的部分集数推断缺失。"""
    chain, _ = _download_chain(monkeypatch, [], server_names=["main", "backup"])
    media = _media_info()
    query_error = _query_error_type()("backup 查询失败")
    results = [ExistMediaInfo(type=MediaType.TV, seasons={1: [167]}), query_error]
    chain.media_exists = Mock(side_effect=results[::-1] if failed_first else results)

    with pytest.raises(_query_error_type()):
        chain.get_no_exists_info(MetaInfo("示例剧集 S01"), media, raise_on_query_failure=True)

    assert all(call.kwargs["raise_on_query_failure"] for call in chain.media_exists.call_args_list)


def test_multi_server_partial_failure_keeps_confirmed_in_lenient_mode(monkeypatch):
    """宽松模式下多服务器一台失败、另一台确认存在时，保留已确认剧集。"""
    chain, mediaserver = _download_chain(monkeypatch, [], server_names=["main", "backup"])
    media = _media_info()

    def _media_exists(**kwargs):
        if kwargs.get("server") == "main":
            return ExistMediaInfo(type=MediaType.TV, server="main", query_failed=True)
        return ExistMediaInfo(type=MediaType.TV, server="backup", seasons={1: list(range(1, 168))})

    chain.media_exists = Mock(side_effect=_media_exists)

    result = chain._media_exists_with_server_cache(media, mediaserver, {})

    assert result is not None
    assert not result.query_failed
    assert result.seasons == {1: list(range(1, 168))}


def test_subscribe_recovery_preserves_progress_and_note(emby_family_query, monkeypatch):
    """订阅在媒体库故障时中止对账并保留原进度与 note；恢复后按真实已有 167 集计算缺集。"""
    owner, transport, _client, _mediaserver, media = emby_family_query
    chain = object.__new__(SubscribeChain)
    chain._SubscribeChain__refresh_total_episode_before_completion = Mock()
    chain.finish_subscribe_or_not = Mock()
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: owner)
    subscribe = _subscribe()
    meta = MetaInfo("示例剧集 S01")

    transport.get_res.return_value = None
    with pytest.raises(_query_error_type(), match="媒体库.*查询失败"):
        chain.check_and_handle_existing_media(subscribe, meta, media, "tmdb:12345")

    chain.finish_subscribe_or_not.assert_not_called()
    assert subscribe.lack_episode == 33
    assert subscribe.note == list(range(157, 168))

    transport.get_res.side_effect = [
        _response({
            "Id": "series-1",
            "Type": "Series",
            "Name": media.title,
            "ProviderIds": {"Tmdb": "12345"},
        }),
        _response({
            "Items": [{"ParentIndexNumber": 1, "IndexNumber": ep} for ep in range(1, 168)],
        }),
    ]
    exists, missing = chain.check_and_handle_existing_media(subscribe, meta, media, "tmdb:12345")

    assert exists is False
    assert missing["tmdb:12345"][1].episodes == list(range(168, 201))
    assert subscribe.note == list(range(157, 168))


def test_refresh_subscribe_progress_skips_on_media_query_failure(emby_family_query, monkeypatch):
    """单订阅刷新进度遇到媒体库查询失败时返回 media_query_failed 且不写库。"""
    owner, transport, _client, _mediaserver, media = emby_family_query
    transport.get_res.return_value = None
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: owner)
    subscribe = _subscribe()
    oper = Mock()
    oper.get.return_value = subscribe
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    chain = object.__new__(SubscribeChain)
    monkeypatch.setattr(chain, "_rlock", RLock())
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=media))

    summary = chain.refresh_subscribe_progress(subscribe, scene="query_failure_test")

    assert summary["updated"] is False
    assert summary["reason"] == "media_query_failed"
    assert subscribe.lack_episode == 33
    assert subscribe.note == list(range(157, 168))
    oper.update.assert_not_called()


def test_subscribe_search_skips_subscription_on_media_query_failure(monkeypatch):
    """订阅搜索遇到 MediaServerQueryError 时跳过当前订阅，不触发搜索也不修改订阅状态。"""
    subscribe = _subscribe()
    media = _media_info()
    chain = object.__new__(SubscribeChain)
    chain._rlock = RLock()
    chain.subscribeoper = Mock()
    chain.subscribeoper.get.return_value = subscribe
    chain.downloadchain = Mock()
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: chain.subscribeoper)
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=media))
    monkeypatch.setattr(
        chain,
        "check_and_handle_existing_media",
        Mock(side_effect=_query_error_type()("媒体库查询失败")),
    )
    search_chain = Mock()
    monkeypatch.setattr(subscribe_module, "SearchChain", lambda: search_chain)

    chain.search(sid=subscribe.id, state="R")

    search_chain.process.assert_not_called()
    chain.subscribeoper.update.assert_not_called()


def test_subscribe_match_skips_subscription_on_media_query_failure(monkeypatch):
    """订阅刷新匹配遇到 MediaServerQueryError 时跳过当前订阅，不触发下载也不修改订阅状态。"""
    subscribe = _subscribe()
    media = _media_info()
    chain = object.__new__(SubscribeChain)
    chain._rlock = RLock()
    subscribeoper = Mock()
    subscribeoper.list.return_value = [subscribe]
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: subscribeoper)
    monkeypatch.setattr(chain, "get_sub_sites", Mock(return_value=[]))
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=media))
    monkeypatch.setattr(
        chain,
        "check_and_handle_existing_media",
        Mock(side_effect=_query_error_type()("媒体库查询失败")),
    )
    download_chain = Mock()
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: download_chain)

    torrent_context = SimpleNamespace(
        torrent_info=SimpleNamespace(title="示例剧集 S01E01"),
        meta_info=MetaInfo("示例剧集 S01E01"),
        media_info=media,
        media_recognize_fail_count=0,
    )
    chain.match(torrents={"example.com": [torrent_context]})

    download_chain.batch_download.assert_not_called()
    subscribeoper.update.assert_not_called()
