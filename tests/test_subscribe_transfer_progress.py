"""整理入库后实时更新电视剧订阅进度的离线回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.core.event import Event
from app.core.metainfo import MetaInfo
from app.schemas import TransferInfo
from app.schemas.types import EventType, MediaType


def _transfer_event(success=True, season=1, episodes=(2, 3)):
    """构造一次电视剧入库完成事件。"""
    meta = MetaInfo(f"Test.Show.S{season:02d}E01")
    meta.begin_season = season
    meta.set_episode_list(list(episodes))
    return Event(EventType.TransferComplete, {
        "mediainfo": MediaInfo(title="测试剧", type=MediaType.TV, tmdb_id=874),
        "meta": meta,
        "transferinfo": TransferInfo(success=success),
    })


def _subscribe(**overrides):
    """构造待更新的电视剧订阅对象。"""
    values = {
        "id": 1,
        "name": "测试剧",
        "type": MediaType.TV.value,
        "tmdbid": 874,
        "doubanid": None,
        "bangumiid": None,
        "season": 1,
        "state": "R",
        "total_episode": 3,
        "lack_episode": 2,
        "note": [1],
    }
    values.update(overrides)
    subscribe = SimpleNamespace(**values)
    subscribe.to_dict = lambda: {
        key: getattr(subscribe, key) for key in values
    }
    return subscribe


def test_transfer_complete_merges_episodes_and_broadcasts_completion(monkeypatch):
    """成功入库应合并离散集号、清零缺集并发送订阅调整事件。"""
    subscribe = _subscribe()
    oper = Mock()
    oper.list.side_effect = lambda state=None: [subscribe] if state == "R,N" else []
    send_event = Mock()
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    monkeypatch.setattr(subscribe_module.eventmanager, "send_event", send_event)

    SubscribeChain.__new__(SubscribeChain).check_transfer_complete(_transfer_event())

    oper.update.assert_called_once()
    assert oper.update.call_args.args[0] == 1
    assert oper.update.call_args.args[1]["note"] == [1, 2, 3]
    assert oper.update.call_args.args[1]["lack_episode"] == 0
    assert send_event.call_args.args[0] == EventType.SubscribeModified
    assert send_event.call_args.args[1].subscribe_id == 1


@pytest.mark.parametrize("scene", ["failed", "other_season", "paused"])
def test_transfer_complete_ignores_failed_or_unmatched_subscriptions(monkeypatch, scene):
    """失败入库、其他季及暂停订阅均不更新进度。"""
    event = _transfer_event(success=scene != "failed", season=2 if scene == "other_season" else 1)
    subscribe = _subscribe(state="S" if scene == "paused" else "R")
    oper = Mock()
    oper.list.side_effect = lambda state=None: (
        [subscribe] if state == "R,N" and subscribe.state in {"R", "N"} else []
    )
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    monkeypatch.setattr(subscribe_module.eventmanager, "send_event", Mock())

    SubscribeChain.__new__(SubscribeChain).check_transfer_complete(event)

    oper.update.assert_not_called()
