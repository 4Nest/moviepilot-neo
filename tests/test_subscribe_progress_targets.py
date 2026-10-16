"""目标范围变化及媒体库快照回退的离线回归测试。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import schemas
from app.chain import subscribe as module
from app.chain.subscribe import SubscribeChain, build_subscribe_meta, build_subscribe_version_view
from app.db.models.subscribe import Subscribe
from app.schemas.types import MediaType


@pytest.fixture
def state(monkeypatch):
    """构造单版本订阅并仅在内存中重放数据库写入。"""
    parent = Subscribe(
        id=1, name="青之箱", type=MediaType.TV.value, year="2024", tmdbid=10001,
        media_source="themoviedb", media_id="10001", season=1,
        total_episode=37, start_episode=1, manual_total_episode=0, lack_episode=37,
        best_version=0, skip_library_check=0, note=[],
        version_rules=[{"id": "adweb", "name": "ADWeb", "enabled": True,
                        "settings": {"total_episode": 0, "start_episode": 1, "best_version": 0}}],
        version_progress={"adweb": {"note": [26], "lack_episode": 36}},
    )
    chain = object.__new__(SubscribeChain)
    oper = Mock()

    def update(_sid, payload):
        for key, value in payload.items():
            setattr(parent, key, deepcopy(value))

    oper.update.side_effect = update
    oper.get.return_value = parent
    monkeypatch.setattr(module, "SubscribeOper", lambda: oper)
    monkeypatch.setattr(chain, "_SubscribeChain__apply_episodes_refresh", lambda current, *a, **k: current)
    return parent, chain


def _persist(parent, chain, missing, total=37):
    """通过实际版本快照 writer 写入查询结果，不依赖新增字段命名。"""
    view = build_subscribe_version_view(parent, parent.version_rules[0])
    no_exists = None if missing is None else {
        "tmdb:10001": {1: schemas.NotExistMediaInfo(
            season=1, episodes=list(missing), total_episode=total, start_episode=1,
        )},
    }
    chain._SubscribeChain__persist_version_progress(parent, "adweb", view, no_exists=no_exists)


def test_successful_snapshot_survives_parent_total_growth(state):
    """总集数增加仅扩大缺口，不能凭空增加已经完成的集数。"""
    parent, chain = state
    parent.total_episode = 25
    parent.version_progress["adweb"]["note"] = [5]
    _persist(parent, chain, range(6, 26), total=25)
    assert schemas.Subscribe.model_validate(parent).completed_episode == 5
    parent.total_episode = 37
    parent.lack_episode += 12
    assert schemas.Subscribe.model_validate(parent).completed_episode == 5


def test_successful_snapshot_counts_parent_prefix_once(state):
    """父订阅从第26集开始时，前25集视为完成，不与库内快照重复累加。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
    parent.start_episode = 26
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26


def test_unknown_refresh_preserves_known_episodes_and_new_download(state):
    """无法查询媒体库时保留成功快照，同时计入新增下载事实。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    parent.version_progress["adweb"]["note"] = [26, 27]
    _persist(parent, chain, None)
    assert schemas.Subscribe.model_validate(parent).completed_episode == 27
    assert parent.version_progress["adweb"]["lack_episode"] == 10


def test_successful_library_removal_can_lower_known_progress(state):
    """成功查到文件确实被删除应覆盖旧快照，不能永远合并旧库内集号。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    _persist(parent, chain, [*range(1, 26), *range(27, 38)])
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1


def test_model_round_trip_preserves_successful_snapshot(state):
    """返回模型序列化不能丢弃用于目标范围计算的新进度事实。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    dumped = schemas.Subscribe.model_validate(parent).model_dump()
    dumped["total_episode"] = 40
    dumped["completed_episode"] = None
    assert schemas.Subscribe.model_validate(dumped).completed_episode == 26


def test_automatic_total_growth_ignores_stale_version_setting(state, monkeypatch):
    """自动总集数更新后不能继续用版本里的旧自动总数截断已完成集号。"""
    parent, chain = state
    parent.total_episode = 25
    parent.version_rules[0]["settings"]["total_episode"] = 25
    parent.version_rules[0]["settings"]["manual_total_episode"] = 0
    view = build_subscribe_version_view(parent, parent.version_rules[0])
    media = SimpleNamespace(
        type=MediaType.TV, tmdb_id=10001, douban_id=None, bangumi_id=None, anilist_id=None,
        title_year="青之箱 (2024)", source="themoviedb", media_id="10001",
        seasons={1: list(range(1, 38))},
    )
    downloader = Mock()
    downloader.get_no_exists_info.return_value = (False, {"tmdb:10001": {1: schemas.NotExistMediaInfo(
        season=1, episodes=list(range(27, 38)), total_episode=37, start_episode=1,
    )}})
    monkeypatch.setattr(module, "DownloadChain", lambda: downloader)
    _exists, missing = chain.check_and_handle_existing_media(view, build_subscribe_meta(view), media, "tmdb:10001")
    chain._SubscribeChain__persist_version_progress(parent, "adweb", view, no_exists=missing)
    assert parent.total_episode == 37
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
    # 下一轮展开不应重新得到陈旧的25集目标。
    assert build_subscribe_version_view(parent, parent.version_rules[0]).total_episode == 37


def test_legacy_lack_without_episode_snapshot_is_not_reinterpreted(state):
    """旧缺集摘要没有计算范围，只能按明确下载记录显示，等待成功重算。"""
    parent, _chain = state
    parent.version_progress["adweb"] = {"note": [5], "lack_episode": 20}
    parent.total_episode = 25
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1
    parent.total_episode = 37
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1


def test_manual_version_range_clips_known_episodes(state):
    """手动缩小版本目标时，库内快照和已下载事实都按新范围计数。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    parent.version_rules[0]["settings"].update({
        "manual_total_episode": 1, "total_episode": 20, "start_episode": 10,
    })
    view = build_subscribe_version_view(parent, parent.version_rules[0])
    assert view.total_episode == 20
    assert SubscribeChain.compute_lack_episode(view) == 0
    assert schemas.Subscribe.model_validate(parent).completed_episode == 20


def test_successful_whole_season_missing_replaces_old_snapshot(state):
    """整季缺失的空集号协议不会误记为全部存在，仍保留真实下载事实。"""
    parent, chain = state
    _persist(parent, chain, range(27, 38))
    _persist(parent, chain, [])
    assert parent.version_progress["adweb"]["known_episodes"] == []
    assert parent.version_progress["adweb"]["lack_episode"] == 36
    assert schemas.Subscribe.model_validate(parent).completed_episode == 1


def test_runtime_total_growth_survives_parent_fact_recording(state, monkeypatch):
    """写库不修改旧父对象时，预检查后汇总事实仍须使用增长后的目标。"""
    parent, chain = state
    parent.total_episode = 25
    parent.version_rules[0]["settings"].update({
        "total_episode": 25, "manual_total_episode": 0,
    })
    updates = []
    oper = Mock()
    # Oper 的数据库更新不会就地改变调用方手里的父订阅对象。
    oper.update.side_effect = lambda _sid, payload: updates.append(deepcopy(payload))
    monkeypatch.setattr(module, "SubscribeOper", lambda: oper)
    media = SimpleNamespace(
        type=MediaType.TV, tmdb_id=10001, douban_id=None, bangumi_id=None, anilist_id=None,
        title_year="青之箱 (2024)", source="themoviedb", media_id="10001",
        seasons={1: list(range(1, 38))},
    )
    downloader = Mock()
    downloader.get_no_exists_info.return_value = (False, {"tmdb:10001": {1: schemas.NotExistMediaInfo(
        season=1, episodes=list(range(27, 38)), total_episode=37, start_episode=1,
    )}})
    monkeypatch.setattr(module, "DownloadChain", lambda: downloader)
    view = build_subscribe_version_view(parent, parent.version_rules[0])

    exists, missing = chain.check_and_handle_existing_media(
        view, build_subscribe_meta(view), media, "tmdb:10001",
    )
    assert exists is False
    assert view.total_episode == 37
    chain._SubscribeChain__persist_version_progress(parent, "adweb", view, no_exists=missing)
    chain._SubscribeChain__record_version_download_facts(parent, media, [], no_exists=missing)

    assert any(payload.get("total_episode") == 37 for payload in updates)
    assert parent.total_episode == 37
    assert parent.version_progress["adweb"]["known_episodes"] == list(range(1, 27))
    assert parent.version_progress["adweb"]["lack_episode"] == 11
    assert parent.version_progress["adweb"]["completed"] is False
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
