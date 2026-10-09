from types import SimpleNamespace

import pytest

from app.chain.subscribe import match_version_rule
from app.core.config import settings
from app.core.metainfo import MetaInfo, clear_rust_parse_options_cache
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.helper.message import TemplateContextBuilder


@pytest.mark.parametrize("rule, resource_team, title, expected", [
    ("三明治摆烂组&LoliHouse", "三明治摆烂组@LoliHouse", "", True),
    ("三明治摆烂组&LoliHouse", "LoliHouse@三明治摆烂组", "", True),
    ("三明治摆烂组 & LoliHouse", "三明治摆烂组@LoliHouse", "", True),
    ("三明治摆烂组@LoliHouse", "LoliHouse&三明治摆烂组", "", True),
    ("DMG&VCB-Studio", "DMG@VCB-Studio@LoliHouse", "", True),
    ("DMG&VCB-Studio", "VCB-Studio@DMG", "", True),
    ("三明治摆烂组&lolihouse", "三明治摆烂组@LoliHouse", "", True),
    ("三明治摆烂组&LoliHouse", "三明治摆烂组", "", False),
    ("三明治摆烂组&LoliHouse", "LoliHouse", "", False),
    ("DMG&VCB-Studio", "DMG2@VCB-Studio", "", False),
    ("三明治摆烂组&LoliHouse", "LoliHouse", "[三明治摆烂组&LoliHouse] Anime - 01", True),
    ("三明治摆烂组&LoliHouse", "三明治摆烂组", "[三明治摆烂组 & LoliHouse] Anime - 01", True),
    ("三明治摆烂组&LoliHouse", "LoliHouse", "【LoliHouse&三明治摆烂组】 Anime - 01", True),
    ("三明治摆烂组&LoliHouse", "三明治摆烂组@LoliHouse", "[三明治摆烂组][LoliHouse] Anime - 01", True),
    ("三明治摆烂组&LoliHouse", "其他组", "[三明治摆烂组&LoliHouse] Anime - 01", False),
    ("CHS&CHT", "LoliHouse", "[LoliHouse] Anime - 01 [CHS&CHT]", False),
    ("三明治摆烂组&LoliHouse", None, "[三明治摆烂组&LoliHouse] Anime - 01", False),
    ("三明治摆烂组|LoliHouse", "LoliHouse", "", True),
    ("LoliHouse", "三明治摆烂组@LoliHouse", "", True),
    ("(?=.*三明治摆烂组)(?=.*LoliHouse)", "LoliHouse@三明治摆烂组", "", True),
    ("三明治摆烂组[@&]LoliHouse", "三明治摆烂组@LoliHouse", "", True),
    ("^三明治摆烂组@LoliHouse$", "LoliHouse@三明治摆烂组", "", False),
    ("^三明治摆烂组&LoliHouse$", "三明治摆烂组@LoliHouse", "", False),
    ("[", "LoliHouse", "", False),
    ("三明治摆烂组&&LoliHouse", "三明治摆烂组@LoliHouse", "", False),
    ("三明治摆烂组& &LoliHouse", "三明治摆烂组@LoliHouse", "", False),
    ("", None, "", True),
])
def test_subscribe_release_group_compatibility(rule, resource_team, title, expected):
    """联合组匹配兼容分隔符、顺序和原标题证据，正则约束保持原义。"""
    context = SimpleNamespace(
        meta_info=SimpleNamespace(resource_team=resource_team, title=title), torrent_info=None,
    )
    assert match_version_rule(context, {"release_group": rule}) is expected


def test_subscribe_release_group_uses_torrent_fallback():
    """缺少元数据制作组时可以使用种子提供的组名和原始标签。"""
    context = SimpleNamespace(
        meta_info=None,
        torrent_info=SimpleNamespace(resource_team="LoliHouse", title="[三明治摆烂组&LoliHouse] Anime - 01"),
    )
    assert match_version_rule(context, {"release_group": "三明治摆烂组&LoliHouse"})


def test_subscribe_release_group_does_not_combine_unrelated_labels():
    """不能把不同来源标签各自包含的组名拼成没有实际发布的联合组。"""
    context = SimpleNamespace(
        meta_info=SimpleNamespace(resource_team="LoliHouse", title="[三明治摆烂组&LoliHouse] Anime - 01"),
        torrent_info=SimpleNamespace(title="[VCB-Studio&LoliHouse] Anime - 01"),
    )
    assert not match_version_rule(context, {"release_group": "三明治摆烂组&VCB-Studio"})


@pytest.mark.parametrize("rust_enabled", [False, True])
@pytest.mark.parametrize("prefix", [
    "[三明治摆烂组&LoliHouse]", "[LoliHouse&三明治摆烂组]", "[三明治摆烂组 & LoliHouse]",
])
def test_real_anime_subscription_and_rename_agree(monkeypatch, rust_enabled, prefix):
    """Python 与 Rust 元数据入口都能匹配实际联合组标题，并原样输出组名。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", rust_enabled)
    meta = MetaInfo(f"{prefix} Kyouran Reijou Nia Liston - 01 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕].mkv")
    context = SimpleNamespace(meta_info=meta, torrent_info=None)

    assert match_version_rule(context, {"release_group": "三明治摆烂组&LoliHouse"})
    assert meta.resource_team == prefix[1:-1]
    assert TemplateContextBuilder().build(meta=meta)["releaseGroup"] == prefix[1:-1]


@pytest.mark.parametrize("rust_enabled", [False, True])
@pytest.mark.parametrize("known_alias", [False, True])
@pytest.mark.parametrize("prefix, expected", [
    ("[smzase&LoliHouse]", "三明治摆烂组&LoliHouse"),
    ("[smzase & LoliHouse]", "三明治摆烂组 & LoliHouse"),
    ("【smzase&LoliHouse】", "三明治摆烂组&LoliHouse"),
])
def test_joint_group_alias_replacement_reaches_subscription_and_rename(
    monkeypatch, rust_enabled, known_alias, prefix, expected,
):
    """识别词转换制作组别名后，订阅和重命名应使用转换后的联合组标签。"""
    groups = ReleaseGroupsMatcher().get_release_groups()
    if known_alias:
        groups += "|三明治摆烂组"
    monkeypatch.setattr(ReleaseGroupsMatcher, "get_release_groups", lambda _self: groups)
    monkeypatch.setattr(settings, "RUST_ACCEL", rust_enabled)
    clear_rust_parse_options_cache()
    try:
        title = f"{prefix} Kyouran Reijou Nia Liston - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
        meta = MetaInfo(title, custom_words=["smzase => 三明治摆烂组"])
        context = SimpleNamespace(meta_info=meta, torrent_info=None)

        assert meta.title == title
        assert expected in meta.org_string
        assert meta.resource_team == expected
        resource_team = meta.resource_team
        assert match_version_rule(context, {"release_group": "三明治摆烂组&LoliHouse"})
        assert TemplateContextBuilder().build(meta=meta)["releaseGroup"] == expected
        assert meta.resource_team == resource_team
    finally:
        clear_rust_parse_options_cache()


@pytest.mark.parametrize("rust_enabled", [False, True])
@pytest.mark.parametrize("title, expected", [
    ("[SweetSub&LoliHouse] Anime - 01 [ANi]", "SweetSub&LoliHouse@ANi"),
    ("Anime.S01E01.1080p-SweetSub&LoliHouse", "SweetSub&LoliHouse"),
    ("[SweetSub@LoliHouse] Anime - 01", "SweetSub@LoliHouse"),
])
def test_metadata_preserves_group_separators_across_parsers(monkeypatch, rust_enabled, title, expected):
    """方括号标签、文件名尾缀及独立参与组在两个解析入口中保持相同分隔符。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", rust_enabled)
    meta = MetaInfo(title, custom_words=["#"])

    assert meta.resource_team == expected
    assert TemplateContextBuilder().build(meta=meta)["releaseGroup"] == expected
    assert match_version_rule(SimpleNamespace(meta_info=meta, torrent_info=None), {
        "release_group": "SweetSub&LoliHouse",
    })
