"""单版本订阅媒体库进度与下载事实隔离的回归测试。"""

from copy import deepcopy
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import schemas
from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain, build_subscribe_version_view
from app.db.models.subscribe import Subscribe
from app.schemas.types import MediaType


def _build_subscribe(**overrides):
    values = {
        "id": 1,
        "name": "青之箱",
        "year": "2024",
        "type": MediaType.TV.value,
        "tmdbid": 10001,
        "media_source": "themoviedb",
        "media_id": "10001",
        "season": 1,
        "start_episode": 1,
        "total_episode": 37,
        "lack_episode": 37,
        "note": [],
        "state": "R",
        "best_version": 0,
        "best_version_full": 0,
        "skip_library_check": 0,
        "episode_priority": {},
        "version_mode": "all",
        "version_rules": [{
            "id": "adweb", "name": "ADWeb", "enabled": True,
            "settings": {"best_version": 0},
        }],
        "version_progress": {"adweb": {"note": [26], "lack_episode": 36}},
    }
    values.update(overrides)
    return Subscribe(**values)


def _missing(episodes):
    return {
        "tmdb:10001": {
            1: schemas.NotExistMediaInfo(
                season=1, episodes=list(episodes), total_episode=37, start_episode=1,
            ),
        },
    }


def _media_info():
    return SimpleNamespace(
        type=MediaType.TV, tmdb_id=10001, douban_id=None, bangumi_id=None,
        anilist_id=None, title_year="青之箱 (2024)", source="themoviedb",
        media_id="10001", seasons={1: list(range(1, 38))},
    )


@pytest.fixture
def progress_chain(monkeypatch):
    """构造没有后台初始化的真实链，并截获所有订阅持久化操作。"""
    chain = object.__new__(SubscribeChain)
    updates = []
    oper = Mock()
    oper.update.side_effect = lambda sid, data: updates.append((sid, deepcopy(data)))
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    return chain, updates


@pytest.mark.parametrize("no_exists, expected", [
    (_missing(range(27, 38)), 11),
    ({}, 0),
    (None, 36),
])
def test_single_version_lack_uses_known_library_result(no_exists, expected):
    """单版本合并库内集数；明确无缺集与未知结果必须区分。"""
    parent = _build_subscribe()
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    assert SubscribeChain.compute_lack_episode(view, no_exists=no_exists) == expected
    assert view.note == [26]
    assert parent.note == []


@pytest.mark.parametrize("mode", ["multiple", "disabled_sibling", "skip_library", "best_version"])
def test_version_lack_keeps_independent_download_facts(mode):
    """多版本、跳过查库和洗版都不能把其他版本的库内文件算作下载事实。"""
    parent = _build_subscribe()
    if mode in ("multiple", "disabled_sibling"):
        parent.version_rules.append({
            "id": "other", "name": "其他版本", "enabled": mode == "multiple", "settings": {},
        })
    elif mode == "skip_library":
        parent.skip_library_check = 1
    else:
        parent.version_rules[0]["settings"]["best_version"] = 1
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    assert SubscribeChain.compute_lack_episode(view, no_exists=_missing(range(27, 38))) == 36
    assert SubscribeChain.compute_lack_episode(view, no_exists={}) == 36


def test_single_version_persistence_keeps_library_progress(progress_chain):
    """持久化不能把已计算的 26/37 重新覆盖为仅下载记录的 1/37。"""
    chain, updates = progress_chain
    parent = _build_subscribe()
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    chain._SubscribeChain__persist_version_progress(
        parent, "adweb", view, no_exists=_missing(range(27, 38)),
    )

    assert parent.version_progress["adweb"]["lack_episode"] == 11
    assert parent.version_progress["adweb"]["note"] == [26]
    assert parent.version_progress["adweb"]["completed"] is False
    assert parent.lack_episode == 37
    assert parent.note == []
    assert all(set(data) == {"version_progress"} for _, data in updates)
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26


@pytest.mark.parametrize("no_exists, expected_lack, completed", [(None, 36, False), ({}, 0, True)])
def test_version_persistence_requires_known_completion(progress_chain, no_exists, expected_lack, completed):
    """持久化只有明确的无缺集结果可以把单版本标记为完成。"""
    chain, _updates = progress_chain
    parent = _build_subscribe()
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    chain._SubscribeChain__persist_version_progress(parent, "adweb", view, no_exists=no_exists)

    assert parent.version_progress["adweb"]["lack_episode"] == expected_lack
    assert parent.version_progress["adweb"]["completed"] is completed


def test_single_version_response_reads_library_progress():
    """数据库对象和 Pydantic 响应都使用版本快照的媒体库进度。"""
    parent = _build_subscribe(version_progress={"adweb": {
        "note": [26], "lack_episode": 11, "known_episodes": list(range(1, 27)),
    }})

    assert schemas.compute_subscribe_completed_episode(parent) == 26
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26


def test_disabled_single_version_does_not_contribute_progress():
    """停用规则在字典和 Pydantic 形式下都不计入完成集数。"""
    parent = _build_subscribe(version_progress={"adweb": {"note": [26], "lack_episode": 11}})
    parent.version_rules[0]["enabled"] = False

    assert schemas.compute_subscribe_completed_episode(parent) == 0
    assert schemas.Subscribe.model_validate(parent).completed_episode == 0


@pytest.mark.parametrize("explicit_start", [False, True])
def test_single_version_response_counts_only_target_episode_range(explicit_start):
    """从第 26 集开始的版本不能把范围外的前 25 集伪计成版本下载。"""
    parent = _build_subscribe(
        start_episode=26,
        version_progress={"adweb": {"note": [26], "lack_episode": 11}},
    )
    if explicit_start:
        parent.version_rules[0]["settings"]["start_episode"] = 26
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    assert SubscribeChain.compute_lack_episode(view, no_exists=_missing(range(27, 38))) == 11
    assert schemas.compute_subscribe_completed_episode(parent) == 1
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1
    assert parent.version_progress["adweb"]["note"] == [26]


@pytest.mark.parametrize("progress, expected", [
    ({}, 0),
    ({"adweb": {"note": [26]}}, 1),
    ({"adweb": {"note": [26], "lack_episode": None}}, 1),
])
def test_response_without_version_lack_falls_back_to_download_facts(progress, expected):
    """老快照没有缺集计数时不能把父行初始值误当作媒体库统计。"""
    parent = _build_subscribe(lack_episode=0, version_progress=progress)

    assert schemas.compute_subscribe_completed_episode(parent) == expected
    assert schemas.Subscribe.model_validate(parent).completed_episode == expected


@pytest.mark.parametrize("mode", ["multiple", "disabled_sibling", "skip_library", "best_version"])
def test_response_isolated_modes_do_not_use_library_lack(mode):
    """其他隔离模式即便存在库内缺集快照，也只显示本版本下载进度。"""
    parent = _build_subscribe(version_progress={"adweb": {"note": [26], "lack_episode": 11}})
    if mode in ("multiple", "disabled_sibling"):
        parent.version_rules.append({
            "id": "other", "name": "其他版本", "enabled": mode == "multiple", "settings": {},
        })
    elif mode == "skip_library":
        parent.skip_library_check = 1
    else:
        parent.version_rules[0]["settings"]["best_version"] = 1

    assert schemas.compute_subscribe_completed_episode(parent) == 1
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1


def test_manual_recompute_updates_version_snapshot_only(progress_chain, monkeypatch):
    """手动重算合并库内 E01–E25 与本版本 E26，并只持久化版本快照。"""
    chain, updates = progress_chain
    parent = _build_subscribe()
    subscribe_module.SubscribeOper().get.return_value = parent
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=_media_info()))
    downloader = Mock()
    downloader.get_no_exists_info.return_value = (False, _missing(range(26, 38)))
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)

    summary = chain.refresh_subscribe_progress(parent, scene="manual_recompute")

    assert summary["updated"] is True
    assert parent.version_progress["adweb"]["lack_episode"] == 11
    assert parent.version_progress["adweb"]["note"] == [26]
    assert parent.lack_episode == 37
    assert parent.note == []
    assert updates
    assert all(set(data) == {"version_progress"} for _, data in updates)
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
    downloader.get_no_exists_info.assert_called_once()


def test_manual_recompute_failure_preserves_snapshot(progress_chain, monkeypatch):
    """媒体库缺集解析失败时不把现有版本进度写成完成。"""
    chain, updates = progress_chain
    parent = _build_subscribe()
    subscribe_module.SubscribeOper().get.return_value = parent
    original = deepcopy(parent.version_progress)
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=_media_info()))
    monkeypatch.setattr(chain, "resolve_subscribe_missing", Mock(return_value=(False, {})))

    summary = chain.refresh_subscribe_progress(parent, scene="manual_recompute")

    assert summary["updated"] is False
    assert parent.version_progress == original
    assert updates == []


@pytest.mark.parametrize("supplied_missing", [False, True])
def test_download_facts_retain_library_progress(progress_chain, monkeypatch, supplied_missing):
    """下载第 26 集后仍计入库内前 25 集，note 只记录真实的新下载。"""
    chain, updates = progress_chain
    parent = _build_subscribe(version_progress={"adweb": {"note": [], "lack_episode": 37}})
    media = _media_info()
    download = SimpleNamespace(
        version_rule_id="adweb", media_info=media,
        selected_episodes=[26], meta_info=SimpleNamespace(episode_list=[26]),
        torrent_info=SimpleNamespace(pri_order=0), confirmed_full_coverage=False,
    )
    downloader = Mock()
    downloader.get_no_exists_info.return_value = (False, _missing(range(26, 38)))
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)
    kwargs = {"no_exists": _missing(range(27, 38))} if supplied_missing else {}

    chain._SubscribeChain__record_version_download_facts(parent, media, [download], **kwargs)

    assert parent.version_progress["adweb"]["lack_episode"] == 11
    assert parent.version_progress["adweb"]["note"] == [26]
    assert parent.version_progress["adweb"]["completed"] is False
    assert parent.note == []
    assert parent.lack_episode == 37
    assert all(set(data) == {"version_progress"} for _, data in updates)
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
    if supplied_missing:
        downloader.get_no_exists_info.assert_not_called()
    else:
        downloader.get_no_exists_info.assert_called_once()


@pytest.mark.parametrize("entrypoint", ["search", "rss"])
@pytest.mark.parametrize("library_result, expected_lack", [
    ((False, _missing(range(26, 38))), 11),
    ((True, {}), 0),
    ((False, {}), None),
])
def test_search_and_rss_persist_library_progress(
        progress_chain, monkeypatch, entrypoint, library_result, expected_lack,
):
    """实际搜索和 RSS 入口保留查库结果；缺集查询失败时保留版本快照。"""
    chain, updates = progress_chain
    chain._rlock = RLock()
    parent = _build_subscribe(manual_total_episode=1)
    original = deepcopy(parent.version_progress)
    oper = subscribe_module.SubscribeOper()
    oper.get.return_value = parent
    oper.list.side_effect = lambda _states: [parent]
    media = _media_info()
    media.clear = Mock()
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=media))
    monkeypatch.setattr(chain, "get_sub_sites", Mock(return_value=[]))
    monkeypatch.setattr(subscribe_module, "global_vars", SimpleNamespace(is_system_stopped=False))
    config = Mock()
    config.get.return_value = None
    monkeypatch.setattr(subscribe_module, "SystemConfigOper", lambda: config)
    downloader = Mock()
    downloader.get_no_exists_info.return_value = deepcopy(library_result)
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)
    search = Mock()
    search.process.return_value = []
    monkeypatch.setattr(subscribe_module, "SearchChain", lambda: search)
    monkeypatch.setattr(subscribe_module, "TorrentHelper", Mock())
    monkeypatch.setattr(subscribe_module, "WordsMatcher", Mock())

    if entrypoint == "search":
        chain.search(sid=parent.id)
    else:
        # 空字典会在订阅处理前返回；保留一个站点空缓存才能覆盖无匹配资源分支。
        chain.match({"example.test": []})

    downloader.get_no_exists_info.assert_called_once()
    assert parent.lack_episode == 37
    assert parent.note == []
    if expected_lack is None:
        assert parent.version_progress == original
        assert updates == []
        search.process.assert_not_called()
    else:
        snapshot = parent.version_progress["adweb"]
        assert snapshot["lack_episode"] == expected_lack
        assert snapshot["note"] == [26]
        assert snapshot["completed"] is (expected_lack == 0)
        assert updates
        assert all(set(data) == {"version_progress"} for _, data in updates)
        assert schemas.Subscribe.model_validate(parent).completed_episode == 37 - expected_lack
        if entrypoint == "search" and expected_lack:
            search.process.assert_called_once()


def test_record_download_during_library_failure_keeps_last_successful_snapshot(progress_chain, monkeypatch):
    """已成功添加的新下载不能因随后查库超时丢失，也不能清除之前确认的库内集数。"""
    chain, _updates = progress_chain
    parent = _build_subscribe(version_progress={"adweb": {
        "note": [26], "lack_episode": 11, "known_episodes": list(range(1, 27)),
    }})
    media = _media_info()
    download = SimpleNamespace(
        version_rule_id="adweb", media_info=media,
        selected_episodes=[27], meta_info=SimpleNamespace(episode_list=[27]),
        torrent_info=SimpleNamespace(pri_order=0), confirmed_full_coverage=False,
    )
    downloader = Mock()
    downloader.get_no_exists_info.return_value = (False, {})
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)

    chain._SubscribeChain__record_version_download_facts(parent, media, [download])

    assert parent.version_progress["adweb"]["note"] == [26, 27]
    assert parent.version_progress["adweb"]["known_episodes"] == list(range(1, 27))
    assert parent.version_progress["adweb"]["lack_episode"] == 10
    assert parent.version_progress["adweb"]["completed"] is False
    assert schemas.Subscribe.model_validate(parent).completed_episode == 27
