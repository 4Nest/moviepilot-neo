"""媒体库查询失败与确认缺失的离线回归测试。"""

import asyncio
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from requests import Response, Timeout

from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.chain import download as download_module
from app.chain import subscribe as subscribe_module
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.metainfo import MetaInfo
from app.db.models.subscribe import Subscribe
from app.modules.emby import EmbyModule
from app.modules.emby import emby as emby_module
from app.schemas import ActionContext, ExistMediaInfo, MediaInfo
from app.schemas.subscribe import compute_subscribe_completed_episode
from app.schemas.types import MediaType
from app.workflow.actions import add_download as add_download_module
from app.workflow.actions.add_download import AddDownloadAction


def _media_info():
    return SimpleNamespace(
        type=MediaType.TV, tmdb_id=10001, douban_id=None, bangumi_id=None,
        anilist_id=None, title="青之箱", year="2024", season=1,
        title_year="青之箱 (2024)", source="themoviedb", media_id="10001",
        seasons={1: list(range(1, 38))},
    )


def _response(payload=None, status=200):
    response = Response()
    response.status_code = status
    response.json = Mock(return_value=payload)
    return response


def _emby_client(monkeypatch):
    client = object.__new__(emby_module.Emby)
    client._host = "https://emby.invalid/"
    client._apikey = "test-key"
    client.user = "test-user"
    request = Mock()
    monkeypatch.setattr(emby_module, "RequestUtils", lambda: request)
    return client, request


def _emby_module(monkeypatch, *clients):
    module = object.__new__(EmbyModule)
    monkeypatch.setattr(module, "get_instances", Mock(return_value={
        f"Emby {index}": client for index, client in enumerate(clients)
    }))
    return module


def _chain(monkeypatch, modules=(), plugins=None):
    chain = object.__new__(DownloadChain)
    monkeypatch.setattr(chain, "modulemanager", SimpleNamespace(
        get_running_modules=lambda _method: modules,
    ), raising=False)
    monkeypatch.setattr(chain, "pluginmanager", SimpleNamespace(
        get_plugin_modules=lambda: plugins or {},
    ), raising=False)
    mediaserver = Mock()
    mediaserver.get_item_id.return_value = "series-id"
    monkeypatch.setattr(download_module, "MediaServerOper", lambda: mediaserver)
    return chain


@pytest.mark.parametrize("failure", ["timeout", "no_response", "http_error", "invalid_json"])
def test_emby_episode_query_failure_does_not_become_missing(monkeypatch, failure):
    """真实 Emby 到下载链路中的请求失败不能产生整季缺失。"""
    client, request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, "get_iteminfo", Mock(return_value=SimpleNamespace(tmdbid=10001)))
    if failure == "timeout":
        request.get_res.side_effect = Timeout("offline timeout")
    elif failure == "no_response":
        request.get_res.return_value = None
    elif failure == "http_error":
        request.get_res.return_value = _response(status=503)
    else:
        request.get_res.return_value = _response({"error": "invalid payload"})
    chain = _chain(monkeypatch, [_emby_module(monkeypatch, client)])

    assert chain.get_no_exists_info(MetaInfo("青之箱 S01"), _media_info()) == (False, {})


def test_emby_timeout_preserves_subscription_library_progress(monkeypatch):
    """真实 HTTP 超时经模块、下载链和订阅重算后，应保留原有 26/37 且不写库。"""
    parent = Subscribe(
        id=1, name="青之箱", year="2024", type=MediaType.TV.value,
        tmdbid=10001, media_source="themoviedb", media_id="10001", season=1,
        start_episode=1, total_episode=37, lack_episode=37, note=[], state="R",
        best_version=0, best_version_full=0, skip_library_check=0,
        episode_priority={}, version_mode="all",
        version_rules=[{"id": "adweb", "name": "ADWeb", "enabled": True,
                        "settings": {"best_version": 0}}],
        version_progress={"adweb": {
            "note": [26], "lack_episode": 11, "known_episodes": list(range(1, 27)),
        }},
    )
    before = deepcopy(parent.version_progress)
    assert compute_subscribe_completed_episode(parent) == 26
    client, request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, "get_iteminfo", Mock(return_value=SimpleNamespace(tmdbid=10001)))
    request.get_res.side_effect = Timeout("offline timeout")
    downloader = _chain(monkeypatch, [_emby_module(monkeypatch, client)])
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)
    oper = Mock()
    oper.get.return_value = parent
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    chain = object.__new__(SubscribeChain)
    monkeypatch.setattr(chain, "_rlock", RLock())
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=_media_info()))

    summary = chain.refresh_subscribe_progress(parent, scene="timeout_regression")

    assert summary["updated"] is False
    assert summary["reason"] == "resolve_missing_failed"
    assert parent.version_progress == before
    assert compute_subscribe_completed_episode(parent) == 26
    assert parent.total_episode == 37
    oper.update.assert_not_called()
    request.get_res.assert_called_once()


@pytest.mark.parametrize("payload, expected", [
    ({"Items": []}, {}),
    ({"Items": [{"ParentIndexNumber": 1, "IndexNumber": 26}]}, {1: [26]}),
])
def test_emby_success_keeps_empty_and_existing_episodes(monkeypatch, payload, expected):
    """成功返回的空库与已有剧集仍按实际缺集计算。"""
    client, request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, "get_iteminfo", Mock(return_value=SimpleNamespace(tmdbid=10001)))
    request.get_res.return_value = _response(payload)
    chain = _chain(monkeypatch, [_emby_module(monkeypatch, client)])

    exists, missing = chain.get_no_exists_info(MetaInfo("青之箱 S01"), _media_info())

    assert exists is False
    season = missing["tmdb:10001"][1]
    assert season.total_episode == 37
    assert season.episodes == ([episode for episode in range(1, 38) if episode != 26] if expected else [])


@pytest.mark.parametrize("response, expected", [
    (None, None),
    (_response(status=503), None),
    (_response({"error": "unavailable"}), None),
    (_response({"Items": []}), {}),
])
def test_emby_title_search_distinguishes_failure_from_absence(monkeypatch, response, expected):
    """标题搜索的 HTTP 或协议错误不能冒充未入库。"""
    client, request = _emby_client(monkeypatch)
    request.get_res.return_value = response

    assert client.get_tv_episodes(title="青之箱", year="2024") == (None, expected)


def test_emby_item_details_failure_is_not_absence(monkeypatch):
    """已找到剧集但读取条目失败时，不能确认整季缺失。"""
    client, _request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, "get_iteminfo", Mock(return_value=None))
    monkeypatch.setattr(client, "_Emby__get_emby_series_id_by_name", Mock(return_value="series-id"))

    assert client.get_tv_episodes(title="青之箱", year="2024") == (None, None)


@pytest.mark.parametrize("later_episodes", [{}, {1: [26]}])
def test_emby_other_instance_does_not_clear_query_failure(monkeypatch, later_episodes):
    """另一实例的空库或部分剧集不能覆盖失败实例的不确定状态。"""
    failed = SimpleNamespace(get_tv_episodes=Mock(return_value=(None, None)))
    later = SimpleNamespace(get_tv_episodes=Mock(return_value=("later-id", later_episodes)))
    module = _emby_module(monkeypatch, failed, later)

    result = module.media_exists(_media_info(), itemid="cached-id")

    assert result is not None and result.query_failed
    assert not result
    assert later.get_tv_episodes.call_args.kwargs["item_id"] == "cached-id"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("plugin_failure", [False, True])
@pytest.mark.parametrize("fallback", [None, ExistMediaInfo(type=MediaType.TV, seasons={1: [26]})])
def test_module_fallback_preserves_failed_library_query(monkeypatch, asynchronous, plugin_failure, fallback):
    """同步、异步及插件到系统的回退都必须保留先前查询失败。"""
    failure = ExistMediaInfo(type=MediaType.TV, query_failed=True)
    failure_call = AsyncMock(return_value=failure) if asynchronous else Mock(return_value=failure)
    fallback_call = AsyncMock(return_value=fallback) if asynchronous else Mock(return_value=fallback)
    failing = SimpleNamespace(get_name=lambda: "Emby", get_priority=lambda: 1, media_exists=failure_call)
    later = SimpleNamespace(get_name=lambda: "Other", get_priority=lambda: 2, media_exists=fallback_call)
    plugins = {("test", "测试插件"): {"media_exists": failure_call}} if plugin_failure else {}
    chain = _chain(monkeypatch, [later] if plugin_failure else [failing, later], plugins)

    if asynchronous:
        result = asyncio.run(chain.async_run_module("media_exists", mediainfo=_media_info()))
    else:
        result = chain.media_exists(_media_info())

    assert result is not None and result.query_failed
    assert not result
    fallback_call.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_successful_fallback_remains_available(monkeypatch, asynchronous):
    """未找到时继续查询其他模块，完整成功的回退仍可返回已有剧集。"""
    success = ExistMediaInfo(type=MediaType.TV, seasons={1: list(range(1, 27))})
    empty_call = AsyncMock(return_value=None) if asynchronous else Mock(return_value=None)
    success_call = AsyncMock(return_value=success) if asynchronous else Mock(return_value=success)
    empty = SimpleNamespace(get_name=lambda: "Empty", get_priority=lambda: 1, media_exists=empty_call)
    found = SimpleNamespace(get_name=lambda: "Found", get_priority=lambda: 2, media_exists=success_call)
    chain = _chain(monkeypatch, [empty, found])

    if asynchronous:
        result = asyncio.run(chain.async_run_module("media_exists", mediainfo=_media_info()))
    else:
        result = chain.media_exists(_media_info())

    assert result is success
    assert result and not result.query_failed


@pytest.mark.parametrize("query_result, should_download, successful", [
    (ExistMediaInfo(type=MediaType.TV, query_failed=True), False, False),
    (ExistMediaInfo(type=MediaType.TV, seasons={1: [26]}), False, True),
    (None, True, True),
])
def test_workflow_only_lack_handles_library_failure(monkeypatch, query_result, should_download, successful):
    """仅下载缺失的工作流遇到查询失败应失败且不写缓存，成功缺失仍可下载。"""
    downloader = Mock()
    downloader.media_exists.return_value = query_result
    downloader.download_single.return_value = "test-hash"
    monkeypatch.setattr(add_download_module, "DownloadChain", lambda: downloader)
    monkeypatch.setattr(add_download_module.global_vars, "is_workflow_stopped", lambda _: False)
    action = AddDownloadAction("download")
    monkeypatch.setattr(action, "check_cache", Mock(return_value=False))
    monkeypatch.setattr(action, "save_cache", Mock())
    context = ActionContext.model_construct(torrents=[SimpleNamespace(
        torrent_info=SimpleNamespace(site="Anibt", title="青之箱 S01E26"),
        meta_info=MetaInfo("青之箱 S01E26"), media_info=_media_info(),
    )], downloads=[])

    result = action.execute(1, {"only_lack": True}, context)

    assert action.done and action.success is successful
    assert bool(result.downloads) is should_download
    assert downloader.download_single.called is should_download
    assert action.save_cache.called is should_download


@pytest.mark.parametrize("episodes", [None, [], list(range(1, 38))])
def test_missing_endpoint_reports_failure_and_preserves_success(monkeypatch, episodes):
    """缺集接口将真实 Emby 超时转为 502，保留成功空库和完整入库的不同返回。"""
    client, request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, "get_iteminfo", Mock(return_value=SimpleNamespace(tmdbid=10001)))
    if episodes is None:
        request.get_res.side_effect = Timeout("offline timeout")
    else:
        request.get_res.return_value = _response({"Items": [
            {"ParentIndexNumber": 1, "IndexNumber": episode} for episode in episodes
        ]})
    downloader = _chain(monkeypatch, [_emby_module(monkeypatch, client)])
    monkeypatch.setattr(mediaserver_endpoint, "DownloadChain", lambda: downloader)
    media = MediaInfo(
        type=MediaType.TV.value, title="青之箱", year="2024", tmdb_id=10001,
        season=1, seasons={1: list(range(1, 38))},
    )

    if episodes is None:
        with pytest.raises(HTTPException) as error:
            mediaserver_endpoint.not_exists(media)
        assert error.value.status_code == 502
        assert error.value.detail == "媒体服务器请求失败"
    else:
        result = mediaserver_endpoint.not_exists(media)
        if episodes:
            assert result == []
        else:
            assert len(result) == 1
            assert result[0].season == 1 and result[0].total_episode == 37
