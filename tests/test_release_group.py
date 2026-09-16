from unittest import TestCase
from unittest.mock import patch

from tests.cases.groups import release_group_cases
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.helper.words import WordsHelper


class MetaInfoTest(TestCase):
    def test_release_group(self):
        for info in release_group_cases:
            print(f"开始测试 {info.get('domain')}")
            for item in info.get('groups', []):
                release_group = ReleaseGroupsMatcher().match(item.get("title"))
                print(f"\tmatch release group {release_group}, should be: {item.get('group')}")
                self.assertEqual(item.get("group"), release_group)
            print(f"完成 {info.get('domain')}")

    def test_custom_release_group_matches_multiple_adjacent_groups(self):
        """自定义制作组共用分隔符时，应完整保留所有命中项。"""
        matcher = ReleaseGroupsMatcher()

        with patch.object(
            WordsHelper,
            "get_merged_words",
            return_value=["VCB-Studio|hyakuhuyu|DMG|GM-Team"],
        ):
            release_group = matcher.match(
                "[DMG&VCB-Studio] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e"
            )

        self.assertEqual("DMG@VCB-Studio", release_group)
