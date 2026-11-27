from unittest.mock import patch

from app.api.endpoints.search import _parse_rule_groups
from app.chain.search import SearchChain
from app.helper.rule import RuleHelper
from app.schemas import CustomRule, FilterRuleGroup


def test_parse_rule_groups_modes():
    assert _parse_rule_groups(None) is None
    assert _parse_rule_groups("") == []
    assert _parse_rule_groups("   ") == []
    assert _parse_rule_groups("__none__") == []
    assert _parse_rule_groups("优先组, rule:BDRip, 优先组, rule:HQ") == [
        "优先组",
        "rule:BDRip",
        "rule:HQ",
    ]


def test_rule_helper_resolves_single_custom_and_builtin_rules():
    helper = RuleHelper()
    mock_groups = [
        FilterRuleGroup(name="电影优先", rule_string="4K > 1080P", media_type="", category=""),
    ]
    mock_custom_rules = [
        CustomRule(id="BDRip", name="蓝光压制", include="BDRip"),
    ]

    with (
        patch.object(RuleHelper, "get_rule_groups", return_value=mock_groups),
        patch.object(RuleHelper, "get_custom_rules", return_value=mock_custom_rules),
    ):
        resolved = helper.get_rule_group_by_media(
            media=None,
            group_names=["电影优先", "rule:BDRip", "rule:4K", "rule:MissingRule"],
        )
        assert [group.name for group in resolved] == ["电影优先", "蓝光压制", "4K"]
        assert [group.rule_string for group in resolved] == ["4K > 1080P", "BDRip", "4K"]

        explained = helper.explain_rule_groups(
            media=None,
            group_names=["电影优先", "rule:BDRip", "rule:MissingRule"],
        )
        assert [(item.name, item.status, item.reason) for item in explained] == [
            ("电影优先", "effective", None),
            ("蓝光压制", "effective", None),
            ("rule:MissingRule", "skipped", "rule_group_missing"),
        ]


def test_search_chain_stringify_and_normalize_rule_groups():
    assert SearchChain._stringify_rule_groups(None) == ""
    assert SearchChain._stringify_rule_groups([]) == "__none__"
    assert SearchChain._stringify_rule_groups(["默认组", "rule:BDRip"]) == "默认组,rule:BDRip"

    normalized = SearchChain._normalize_search_params(
        {
            "keyword": "tmdb:123",
            "sites": "1,2",
            "rule_groups": "__none__",
        }
    )
    assert normalized is not None
    assert normalized["rule_groups"] == "__none__"
