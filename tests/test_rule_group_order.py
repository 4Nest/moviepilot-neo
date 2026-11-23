"""
规则组按调用方 group_names 顺序返回的回归测试。
"""

import pytest

from app.core.context import MediaInfo, TorrentInfo
from app.helper.rule import RuleHelper
from app.modules.filter import FilterModule
from app.modules.filter.builtin_rules import BUILTIN_RULE_SET
from app.schemas import FilterRuleGroup
from app.schemas.types import MediaType


def test_rule_group_by_media_preserves_caller_group_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    调用方传入的规则组顺序（如订阅/洗版规则组顺序）决定后置组覆盖优先级，不得被规则定义存储顺序覆盖。
    """
    media = MediaInfo(type=MediaType.MOVIE, title="Example", tmdb_id=1)
    helper = RuleHelper()
    # 存储顺序故意把无 '>' 的全局前置过滤放后面、把分档规则放前面
    tiered_movie = FilterRuleGroup(name="tiered-movie", rule_string="4K & CNSUB > 1080P & CNSUB", media_type="电影")
    global_pre = FilterRuleGroup(name="global-pre", rule_string="!CAM", media_type="")
    monkeypatch.setattr(helper, "get_rule_groups", lambda: [tiered_movie, global_pre])

    selected = helper.get_rule_group_by_media(
        media=media,
        group_names=["global-pre", "tiered-movie", "global-pre"],
    )
    assert [g.name for g in selected] == ["global-pre", "tiered-movie"]

    module = FilterModule()
    module.rulehelper = helper
    module.rule_set = BUILTIN_RULE_SET

    t_4k = TorrentInfo(title="Example.2025.2160p.WEB-DL.H265-GRP", description="中字")
    t_1080p = TorrentInfo(title="Example.2025.1080p.WEB-DL.H264-GRP", description="中字")
    result = module.filter_torrents(["global-pre", "tiered-movie"], [t_4k, t_1080p], media)

    assert len(result) == 2
    assert t_4k.pri_order == 100
    assert t_1080p.pri_order == 99
