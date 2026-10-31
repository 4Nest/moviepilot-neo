"""内置可信制作组（TRUSTED）与枪版（CAM）规则的过滤行为测试；Rust 与 Python 两条路径结果必须一致。"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.context import TorrentInfo
from app.helper.rule import DEFAULT_TRUSTED_RELEASE_GROUPS, RuleHelper
from app.modules.filter import FilterModule
from app.schemas import CustomRule


class _RuleHelper:
    """过滤模块测试用规则仓库：固定规则串、可信组名单和自定义规则。"""

    def __init__(self, rule_string: str, trusted=DEFAULT_TRUSTED_RELEASE_GROUPS, custom_rules=()):
        self.rule_string = rule_string
        self.trusted = list(trusted)
        self.custom_rules = list(custom_rules)

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        return [SimpleNamespace(name="test", rule_string=self.rule_string)]

    def get_trusted_release_groups(self):
        return self.trusted

    def get_custom_rules(self):
        return self.custom_rules


def _filter(titles, rule_string, use_rust, descriptions=None, **helper_kwargs):
    module = FilterModule()
    module.rulehelper = _RuleHelper(rule_string, **helper_kwargs)
    module.init_module()
    descriptions = descriptions or [""] * len(titles)
    torrents = [TorrentInfo(title=t, description=d, site_name="test") for t, d in zip(titles, descriptions)]
    switch = nullcontext() if use_rust else patch("app.modules.filter.rust_accel.is_enabled", return_value=False)
    with switch:
        kept = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)
    return [t.title for t in kept]


ENGINES = pytest.mark.parametrize("use_rust", [True, False], ids=["rust", "python"])


@ENGINES
def test_trusted_matches_groups_only_at_release_positions(use_rust):
    """可信组只在 `-组名`、`[组名]`、`【组名】`、联合组位置命中，片名中的同名单词不算。"""
    trusted = [
        "Inception.2010.1080p.BluRay.x264-CMCTV",
        "Dune.2024.2160p.WEB-DL.DDP5.1.H.265-flux",
        "[LittleBakas!] Frieren - 01 [1080p]",
        "[Little Subbers!] Kusuriya no Hitorigoto - 05",
        "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 - 01 [WebRip 1080p]",
        "【澄空学园】间谍过家家 第02集",
        "Movie.2024.WEB-DL-Prejudice-Studio",
    ]
    untrusted = [
        "Call.of.Duty.Black.Ops.1080p.WEB-DL-ABC",
        "The.Nest.2020.1080p.BluRay-YYY",
        "Sunny.2011.KOREAN.1080p.BluRay-FAKE",
        "Ao.Haru.Ride.2014.1080p.BluRay-XXX",
        "Movie.2024.1080p.BluRay-CMCTVX",
    ]
    assert _filter(trusted + untrusted, "TRUSTED", use_rust) == trusted


@ENGINES
def test_trusted_ignores_group_names_in_description(use_rust):
    """副标题里出现可信组名不能让非可信组资源通过。"""
    kept = _filter(["Movie.2024.1080p.BluRay-XXX"], "TRUSTED", use_rust, descriptions=["压制 -CMCTV Dream"])
    assert kept == []


@ENGINES
def test_trusted_names_are_literal_and_prefer_complete_names(use_rust):
    """组名按字面匹配不区分大小写，正则元字符不生效，前缀组名不能命中更长的组名。"""
    titles = ["Movie-a+b", "Movie-AAB", "Movie-CMCTV", "Movie-cmct"]
    assert _filter(titles, "TRUSTED", use_rust, trusted=["A+B", "CMCT"]) == ["Movie-a+b", "Movie-cmct"]


@ENGINES
def test_empty_trusted_list_matches_nothing(use_rust):
    """名单保存为空时 TRUSTED 不放行任何资源。"""
    assert _filter(["Movie-CMCTV", "[LoliHouse] Show - 01"], "TRUSTED", use_rust, trusted=[]) == []


@ENGINES
def test_custom_rule_with_same_id_overrides_builtin_trusted(use_rust):
    """用户自定义的同名 TRUSTED 规则优先于内置可信组规则。"""
    custom = [CustomRule(id="TRUSTED", name="自定义", include="ONLYME")]
    kept = _filter(["Movie-CMCTV", "Movie-ONLYME"], "TRUSTED", use_rust, custom_rules=custom)
    assert kept == ["Movie-ONLYME"]


@ENGINES
def test_cam_rule_flags_pirated_sources_without_false_positives(use_rust):
    """CAM 识别枪版与抢先版，不误伤 DTS 音轨、小写 .ts 录制文件和正常片源。"""
    pirated = [
        "Movie.2024.HDCAM.x264-ABC",
        "Movie.2024.camrip-ABC",
        "Movie.2024.TS.x264-ABC",
        "Movie.2024.HDTS.x264-ABC",
        "Movie.2024.HDTC-ABC",
        "Movie.2024.DVDSCR-ABC",
        "某电影 2024 枪版",
        "某电影 2024 抢先版",
    ]
    clean = [
        "Movie.2024.1080p.BluRay.DTS-HD.MA.5.1-FRDS",
        "[Snow-Raws] Show 01 (1080i).ts",
        "Movie.2024.1080p.WEB-DL.DDP5.1.Atmos-CMCT",
        "Matchstick.Men.2003.1080p.BluRay-ABC",
    ]
    assert _filter(pirated + clean, "CAM", use_rust) == pirated


@ENGINES
def test_trusted_and_not_cam_rejects_pirated_release_from_trusted_group(use_rust):
    """`TRUSTED & !CAM` 只保留可信组的非枪版资源。"""
    titles = ["Movie.2024.1080p.WEB-DL-CMCTV", "Movie.2024.TS.x264-CMCTV", "Movie.2024.1080p.WEB-DL-XXX"]
    assert _filter(titles, "TRUSTED & !CAM", use_rust) == ["Movie.2024.1080p.WEB-DL-CMCTV"]


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, list(DEFAULT_TRUSTED_RELEASE_GROUPS)),
        ([], []),
        ([" FLUX ", "", "FLUX", "CMCT", 3], ["FLUX", "CMCT"]),
    ],
    ids=["unset_uses_default", "empty_list", "normalized"],
)
def test_rule_helper_resolves_trusted_list(stored, expected):
    """未保存时使用默认名单；保存过的名单去除空白、空行、重复和非文本项。"""
    with patch("app.helper.rule.SystemConfigOper") as oper:
        oper.return_value.get.return_value = stored
        assert RuleHelper.get_trusted_release_groups() == expected
