import pytest

from tests.cases.groups import release_group_cases
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.helper.words import WordsHelper


def test_release_group():
    """各站点内置制作组仍按原标题中的名称识别。"""
    for info in release_group_cases:
        for item in info.get('groups', []):
            assert ReleaseGroupsMatcher().match(item.get("title")) == item.get("group")

def test_custom_release_group_matches_multiple_adjacent_groups(monkeypatch):
    """自定义制作组共用分隔符时，应完整保留组名和原有 &。"""
    monkeypatch.setattr(WordsHelper, "get_merged_words", lambda _key: ["VCB-Studio|hyakuhuyu|DMG|GM-Team"])
    assert ReleaseGroupsMatcher().match(
        "[DMG&VCB-Studio] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e"
    ) == "DMG&VCB-Studio"


@pytest.mark.parametrize("title, expected", [
    ("[三明治摆烂组&LoliHouse] Anime - 01", "三明治摆烂组&LoliHouse"),
    ("[DMG&VCB-Studio] Anime - 01", "DMG&VCB-Studio"),
    ("[Airota&DMG&VCB-Studio] Anime - 01", "Airota&DMG&VCB-Studio"),
    ("[Nest@ADWeb] Anime - 01", "Nest@ADWeb"),
    ("Anime.S01E01.1080p-DMG&VCB-Studio", "DMG&VCB-Studio"),
    ("Anime.S01E01.1080p-DMG&VCB-Studio@ADWeb", "DMG&VCB-Studio@ADWeb"),
    ("[DMG&VCB-Studio] Anime - 01 [ADWeb]", "DMG&VCB-Studio@ADWeb"),
    ("[DMG][VCB-Studio] Anime - 01", "DMG@VCB-Studio"),
    ("[DMG] Anime - 01 [VCB-Studio]", "DMG@VCB-Studio"),
    ("[DMG&未知组] Anime - 01", "DMG&未知组"),
])
def test_release_group_preserves_joint_separators(title, expected):
    """明确相连的联合组保留原标题分隔符，独立标签仍按原有格式合并。"""
    groups = "三明治摆烂组|LoliHouse|DMG|VCB-Studio|Airota|Nest|ADWeb"
    assert ReleaseGroupsMatcher().match(title, groups=groups) == expected
