# -*- coding: utf-8 -*-
"""二级分类订阅规则与电视剧订阅下载回填。"""
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.types import MediaType, SystemConfigKey
import tests.test_subscribe_chain as subscribe_chain_test

# 以模块属性访问，避免把 TestCase 类导入本模块全局而被 pytest 重复收集
SUBSCRIBE_CHAIN_MODULE = subscribe_chain_test.SUBSCRIBE_CHAIN_MODULE
SubscribeChain = subscribe_chain_test.SubscribeChain
match_rule = SUBSCRIBE_CHAIN_MODULE.match_subscribe_category_rule
build_backfill = SUBSCRIBE_CHAIN_MODULE.build_subscribe_backfill

_ANIME_RULE = {
    "type": "电视剧", "categories": ["日番", "国漫"],
    "include": "ADWeb", "sites": [3], "save_path": "/media/anime/{name}",
}


def _system_config(values: dict):
    """按配置键返回固定值的 SystemConfigOper 替身。"""

    class _SystemConfigOper:
        def get(self, key, *args, **kwargs):
            return values.get(key.value if hasattr(key, "value") else key)

    return _SystemConfigOper


def test_match_rule_by_type_and_category():
    """按媒体类型与分类名匹配，停用规则跳过，多条命中取第一条。"""
    movie_rule = {"type": "电影", "categories": ["日番"], "include": "movie"}
    disabled = {"type": "电视剧", "categories": ["日番"], "include": "off", "enabled": False}
    rules = [movie_rule, disabled, _ANIME_RULE, {"type": "电视剧", "categories": ["日番"], "include": "later"}]

    assert match_rule(MediaType.TV, "日番", rules) is _ANIME_RULE
    assert match_rule(MediaType.MOVIE, "日番", rules) is movie_rule
    assert match_rule(MediaType.TV, "国产剧", rules) is None
    assert match_rule(MediaType.TV, None, rules) is None
    assert match_rule(MediaType.TV, "日番", None) is None


def test_default_kwargs_precedence_explicit_then_rule_then_default():
    """显式传入 > 二级分类规则 > 电视剧默认规则，保存路径中的 {name} 替换为媒体名称。"""
    defaults = {SystemConfigKey.DefaultTvSubscribeConfig.value: {
        "include": "default-include", "exclude": "default-exclude", "resolution": "1080[pi]|x1080",
    }}
    chain = SubscribeChain()
    with patch.object(SUBSCRIBE_CHAIN_MODULE, "SystemConfigOper", _system_config(defaults)):
        result = chain._SubscribeChain__get_default_kwargs(
            MediaType.TV, category_rule=_ANIME_RULE, media_name="孤独摇滚 (2022)",
            resolution="4K|2160p|x2160",
        )

    assert result["resolution"] == "4K|2160p|x2160"          # 显式传入
    assert result["include"] == "ADWeb"                       # 分类规则覆盖默认
    assert result["sites"] == [3]
    assert result["save_path"] == "/media/anime/孤独摇滚 (2022)"
    assert result["exclude"] == "default-exclude"             # 规则未设置，沿用默认


def test_default_kwargs_without_rule_uses_type_default():
    """未命中规则时行为与原来一致。"""
    defaults = {SystemConfigKey.DefaultTvSubscribeConfig.value: {"include": "default-include"}}
    chain = SubscribeChain()
    with patch.object(SUBSCRIBE_CHAIN_MODULE, "SystemConfigOper", _system_config(defaults)):
        result = chain._SubscribeChain__get_default_kwargs(MediaType.TV)

    assert result["include"] == "default-include"
    assert result["save_path"] is None


def _download(pix="2160p", rtype="WEB-DL", effect="DV HDR", team="ADWeb", customization=None, site=3):
    return SimpleNamespace(
        meta_info=SimpleNamespace(resource_pix=pix, resource_type=rtype, resource_effect=effect,
                                  resource_team=team, customization=customization),
        torrent_info=SimpleNamespace(site=site),
    )


def _subscribe(**overrides):
    fields = {"resolution": None, "quality": None, "effect": None, "include": None, "sites": []}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_backfill_maps_resource_to_subscribe_option_values():
    """回填值与订阅编辑页下拉选项取值一致。"""
    update = build_backfill(_subscribe(), _download(), list(SUBSCRIBE_CHAIN_MODULE.SUBSCRIBE_BACKFILL_FIELDS), [3])

    assert update == {
        "resolution": "4K|2160p|x2160",
        "quality": "WEB-?DL|WEB-?RIP",
        "effect": r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+",
        "include": "ADWeb",
        "sites": [3],
    }


def test_backfill_only_fills_enabled_empty_fields():
    """只回填启用且为空的字段，不覆盖用户已有设置。"""
    subscribe = _subscribe(include="MyTeam", resolution="720[pi]|x720")

    update = build_backfill(subscribe, _download(), ["resolution", "include", "quality"], [])

    assert update == {"quality": "WEB-?DL|WEB-?RIP"}
    assert build_backfill(subscribe, _download(), [], []) == {}


def test_backfill_site_limited_to_rss_sites_and_team_escaped():
    """站点须在全局订阅站点内；制作组按字面匹配转义。"""
    download = _download(team="CHD+", customization="HDR10", site=5)

    update = build_backfill(_subscribe(), download, ["include", "sites"], [3])
    assert update == {"include": r"HDR10.+CHD\+"}

    update = build_backfill(_subscribe(), download, ["sites"], [])
    assert update == {"sites": [5]}


def test_backfill_quality_prefers_specific_types():
    """Remux 优先于蓝光；无法识别的属性不回填。"""
    update = build_backfill(_subscribe(), _download(pix="480p", rtype="UHD BluRay Remux", effect=None),
                            ["resolution", "quality", "effect"], [])

    assert update == {"quality": "Remux"}
