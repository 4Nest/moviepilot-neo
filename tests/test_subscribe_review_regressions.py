"""旧订阅失败保护、多版本范围隔离与强制搜索的回归测试。"""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Timeout

from app import schemas
from app.chain import subscribe as module
from app.chain.subscribe import SubscribeChain, build_subscribe_meta, build_subscribe_version_view
from app.schemas.subscribe import compute_subscribe_version_total_episode
from tests.test_library_query_failure import _emby_client, _emby_module, _chain, _media_info as _library_media_info
from tests.test_subscribe_version_progress import _build_subscribe, _media_info, _missing, progress_chain


def _independent_versions():
    """两个剧集组使用不同的自动范围，长版本尚未下载后半季。"""
    return _build_subscribe(total_episode=12, manual_total_episode=0,
        version_rules=[
            {'id': 'short', 'name': '12集分组', 'enabled': True,
             'settings': {'total_episode': 12, 'manual_total_episode': 0,
                          'episode_group': 'short-group', 'best_version': 0}},
            {'id': 'long', 'name': '24集分组', 'enabled': True,
             'settings': {'total_episode': 24, 'manual_total_episode': 0,
                          'episode_group': 'long-group', 'best_version': 0}},
        ], version_progress={
            'short': {'note': list(range(1, 12)), 'lack_episode': 1, 'completed': False},
            'long': {'note': list(range(1, 13)), 'lack_episode': 12, 'completed': False},
        })


@pytest.mark.parametrize('entrypoint', ['search', 'rss'])
@pytest.mark.parametrize('library_state', ['timeout', 'missing', 'complete'])
def test_legacy_library_result_preserves_completion_contract(monkeypatch, entrypoint, library_state):
    """旧普通订阅查询失败保留进度；明确缺集继续订阅，明确齐全集才归档。"""
    parent = _build_subscribe(manual_total_episode=1, version_rules=[], version_progress={},
                              note=[26], lack_episode=11)
    media = _library_media_info()
    media.clear = Mock()
    media.get_message_image = Mock(return_value=None)
    media.to_dict = Mock(return_value={})
    client, request = _emby_client(monkeypatch)
    monkeypatch.setattr(client, 'get_iteminfo', Mock(return_value=SimpleNamespace(tmdbid=10001)))
    request.get_res.side_effect = Timeout('offline timeout')
    downloader = _chain(monkeypatch, [_emby_module(monkeypatch, client)])
    if library_state != 'timeout':
        result = (True, {}) if library_state == 'complete' else (False, _missing(range(27, 38)))
        monkeypatch.setattr(downloader, 'get_no_exists_info', Mock(return_value=result))
    monkeypatch.setattr(module, 'DownloadChain', lambda: downloader)
    oper = Mock()
    oper.get.return_value = parent
    oper.list.return_value = [parent]
    monkeypatch.setattr(module, 'SubscribeOper', lambda: oper)
    chain = object.__new__(SubscribeChain)
    monkeypatch.setattr(chain, '_rlock', RLock())
    monkeypatch.setattr(chain, 'recognize_media', Mock(return_value=media))
    monkeypatch.setattr(chain, 'get_sub_sites', Mock(return_value=[]))
    monkeypatch.setattr(chain, 'post_message', Mock())
    monkeypatch.setattr(module, 'global_vars', SimpleNamespace(is_system_stopped=False))
    config = Mock()
    config.get.return_value = None
    monkeypatch.setattr(module, 'SystemConfigOper', lambda: config)
    search = Mock()
    search.process.return_value = []
    monkeypatch.setattr(module, 'SearchChain', lambda: search)
    monkeypatch.setattr(module, 'TorrentHelper', Mock())
    monkeypatch.setattr(module, 'WordsMatcher', Mock())
    monkeypatch.setattr(module.eventmanager, 'send_event', Mock(return_value=None))
    monkeypatch.setattr(module.MoviePilotServerHelper, 'sub_done_async', Mock())
    if entrypoint == 'search':
        chain.search(sid=parent.id)
    else:
        chain.match({'example.test': []})
    if library_state == 'timeout':
        request.get_res.assert_called_once()
    else:
        downloader.get_no_exists_info.assert_called_once()
        request.get_res.assert_not_called()
    if library_state == 'complete':
        oper.delete.assert_called_once_with(parent.id)
        oper.add_history.assert_called_once()
    else:
        oper.delete.assert_not_called()
        assert parent.lack_episode == 11
        oper.add_history.assert_not_called()


def test_long_version_does_not_finish_with_short_version_range(monkeypatch):
    """12集剧集组的版本完成，不能把另一24集剧集组的半季下载判为完成。"""
    parent = _independent_versions()
    media = _media_info()
    media.seasons = {1: list(range(1, 13))}
    media.get_message_image = Mock(return_value=None)
    media.to_dict = Mock(return_value={})
    download = SimpleNamespace(
        version_rule_id='short', media_info=media, selected_episodes=[12],
        meta_info=SimpleNamespace(episode_list=[12]),
        torrent_info=SimpleNamespace(pri_order=0), confirmed_full_coverage=False,
    )
    oper = Mock()
    monkeypatch.setattr(module, 'SubscribeOper', lambda: oper)
    monkeypatch.setattr(module.eventmanager, 'send_event', Mock(return_value=None))
    monkeypatch.setattr(module.MoviePilotServerHelper, 'sub_done_async', Mock())
    chain = object.__new__(SubscribeChain)
    monkeypatch.setattr(chain, 'post_message', Mock())

    chain.finish_subscribe_or_not(parent, build_subscribe_meta(parent), media,
                                  downloads=[download], lefts={})

    assert not oper.delete.called, parent.version_progress
    assert parent.version_progress['short']['completed'] is True
    assert parent.version_progress['long']['completed'] is False
    assert parent.version_progress['long']['lack_episode'] == 12


@pytest.mark.parametrize('new_episode', [None, 27])
def test_force_search_keeps_library_snapshot(monkeypatch, new_episode):
    """强制搜索有无下载均保留原库快照，新下载只追加到本版本事实。"""
    chain = object.__new__(SubscribeChain)
    chain._rlock = RLock()
    parent = _build_subscribe(manual_total_episode=1, version_progress={"adweb": {
        "note": [26], "lack_episode": 11,
        "known_episodes": list(range(1, 27)), "completed": False,
    }})
    before = schemas.Subscribe.model_validate(parent).completed_episode
    oper = Mock()
    oper.get.return_value = parent
    oper.list.return_value = [parent]
    monkeypatch.setattr(module, "SubscribeOper", lambda: oper)
    media = _media_info()
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=media))
    monkeypatch.setattr(chain, "get_sub_sites", Mock(return_value=[]))
    monkeypatch.setattr(module, "global_vars", SimpleNamespace(is_system_stopped=False))
    config = Mock()
    config.get.return_value = None
    monkeypatch.setattr(module, "SystemConfigOper", lambda: config)
    downloader = Mock()
    monkeypatch.setattr(module, "DownloadChain", lambda: downloader)
    search = Mock()
    context = SimpleNamespace(
        media_info=media, selected_episodes=[new_episode],
        meta_info=SimpleNamespace(episode_list=[new_episode]),
        torrent_info=SimpleNamespace(pri_order=0), confirmed_full_coverage=False,
    )
    search.process.return_value = [context] if new_episode else []
    monkeypatch.setattr(module, "SearchChain", lambda: search)
    download = Mock(return_value=([context], _missing([*range(1, 26), *range(28, 38)])))
    monkeypatch.setattr(chain, '_SubscribeChain__download_best_version_with_full_pack_first', download)

    chain.search(sid=parent.id, force_search=True)

    downloader.get_no_exists_info.assert_not_called()
    assert parent.skip_library_check == 0
    assert before == 26
    snapshot = parent.version_progress['adweb']
    assert snapshot['known_episodes'] == list(range(1, 27))
    assert snapshot['note'] == ([26, 27] if new_episode else [26])
    assert snapshot['lack_episode'] == (10 if new_episode else 11)
    assert schemas.Subscribe.model_validate(parent).completed_episode == (27 if new_episode else 26)
    if new_episode:
        download.assert_called_once()
    else:
        download.assert_not_called()


@pytest.mark.parametrize('persist_before_download', [True, False])
def test_independent_version_growth_survives_download_and_rebuild(
        progress_chain, monkeypatch, persist_before_download,
):
    """搜索先保存或RSS直接下载，均保留本轮增长后的范围及兄弟版本的原范围。"""
    chain, _updates = progress_chain
    parent = _independent_versions()
    rule = parent.version_rules[1]
    runtime = build_subscribe_version_view(parent, rule)
    media = _media_info()
    media.seasons = {1: list(range(1, 27))}
    monkeypatch.setattr(module.eventmanager, 'send_event', Mock(return_value=None))

    exists, missing = chain.check_and_handle_existing_media(
        runtime, build_subscribe_meta(runtime), media, 'tmdb:10001',
    )
    assert exists is False
    assert runtime.total_episode == 26
    assert missing['tmdb:10001'][1].episodes == list(range(13, 27))
    if persist_before_download:
        chain._SubscribeChain__persist_version_progress(parent, 'long', runtime, no_exists=missing)
    download = SimpleNamespace(
        version_rule_id='long', media_info=media, selected_episodes=[25],
        meta_info=SimpleNamespace(episode_list=[25]),
        torrent_info=SimpleNamespace(pri_order=0), confirmed_full_coverage=False,
    )
    chain._SubscribeChain__record_version_download_facts(
        parent, media, [download], no_exists=_missing([*range(13, 25), 26]), runtime_view=runtime,
    )

    long_progress = parent.version_progress['long']
    assert long_progress['total_episode'] == 26
    assert long_progress['episode_group'] == 'long-group'
    assert long_progress['note'] == [*range(1, 13), 25]
    assert long_progress['lack_episode'] == 13
    assert long_progress['completed'] is False
    assert parent.version_progress['short']['total_episode'] == 12
    assert parent.version_progress['short']['lack_episode'] == 1
    assert build_subscribe_version_view(parent, rule).total_episode == 26
    response = schemas.Subscribe.model_validate(parent)
    assert compute_subscribe_version_total_episode(response, response.version_rules[1]) == 26


@pytest.mark.parametrize('settings, progress, expected', [
    ({'manual_total_episode': 0}, {'total_episode': 30, 'episode_group': 'long-group'}, 30),
    ({'manual_total_episode': 1}, {'total_episode': 30, 'episode_group': 'long-group'}, 24),
    ({'manual_total_episode': 0}, {'total_episode': 30, 'episode_group': 'old-group'}, 24),
    ({'manual_total_episode': 0}, {'total_episode': 30}, 24),
    ({'manual_total_episode': 0, 'episode_group': None}, {'total_episode': 30, 'episode_group': None}, 30),
])
def test_independent_total_respects_settings_and_snapshot_identity(settings, progress, expected):
    """自动范围仅复用同剧集组的快照；手动固定范围优先，历史无组快照不能覆盖设置。"""
    parent = _independent_versions()
    rule = parent.version_rules[1]
    rule['settings'].update(settings)
    parent.version_progress['long'].update(progress)

    assert build_subscribe_version_view(parent, rule).total_episode == expected
    response = schemas.Subscribe.model_validate(parent)
    assert compute_subscribe_version_total_episode(response, response.version_rules[1]) == expected


def test_manual_recompute_updates_each_versions_resolved_range(progress_chain, monkeypatch):
    """手动重算按各自剧集组更新自动总数，不能保存仍未增长的旧范围。"""
    chain, _updates = progress_chain
    chain._rlock = RLock()
    parent = _independent_versions()
    module.SubscribeOper().get.return_value = parent

    def recognize(**kwargs):
        media = _media_info()
        total = 26 if kwargs['episode_group'] == 'long-group' else 12
        media.seasons = {1: list(range(1, total + 1))}
        return media

    monkeypatch.setattr(chain, 'recognize_media', recognize)

    summary = chain.refresh_subscribe_progress(parent)

    assert summary['updated'] is True
    assert parent.version_progress['long']['total_episode'] == 26
    assert parent.version_progress['long']['lack_episode'] == 14
    assert parent.version_progress['short']['total_episode'] == 12
    assert parent.version_progress['short']['lack_episode'] == 1
    assert parent.total_episode == 12
