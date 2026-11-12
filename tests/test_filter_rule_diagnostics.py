"""验证旧规则 ID 的完整定位、规则配置诊断与过滤器的延迟解析合同。"""

from collections import Counter

import pytest

from app.chain.search import SearchChain
from app.core.context import MediaInfo, TorrentInfo
from app.helper import rule as rule_helper
from app.modules.filter import FilterModule
from app.modules.filter.RuleParser import RuleParser
from app.modules.filter.builtin_rules import BUILTIN_RULE_SET
from app.schemas import CustomRule, FilterRuleGroup
from app.schemas.search import SearchDiagnosticWarning
from app.schemas.types import MediaType
from app.utils import rust_accel


@pytest.mark.parametrize(
    ("expression", "ids", "code", "expected_ids"),
    [
        ("1080P &  Pre Candidate", [" Pre Candidate"], "rule_id_invalid", [" Pre Candidate"]),
        ("!M-Team & 1080P", ["M-Team"], "rule_id_invalid", ["M-Team"]),
        ("!Missing & 1080P", [], "rule_id_missing", ["Missing"]),
        ("1080P >", [], "rule_syntax_invalid", []),
        ("(1080P", [], "rule_syntax_invalid", []),
        ("1080P & 123", ["123"], "rule_id_invalid", ["123"]),
    ],
)
def test_rule_expression_diagnosis_keeps_complete_operands(expression, ids, code, expected_ids) -> None:
    """配置诊断应保留完整 ID，并区分无效 ID、悬空引用和表达式语法错误。"""
    issue = rule_helper.describe_rule_string_error(expression, [*BUILTIN_RULE_SET, *ids])
    assert issue is not None
    assert issue.code == code
    assert list(issue.rule_ids) == expected_ids


def test_valid_rule_expression_has_no_configuration_warning() -> None:
    """括号、否定、多层级和数字开头的合法 ID 不应产生配置告警。"""
    assert rule_helper.describe_rule_string_error("(1080P | 4K) & !BLU > 60FPS", BUILTIN_RULE_SET) is None


def _invalid_filter_module(monkeypatch, expression: str = "1080P &  Pre Candidate") -> FilterModule:
    """构造只含一个旧式非法自定义规则的过滤模块。"""
    monkeypatch.setattr(rust_accel, "filter_torrents", lambda **_kwargs: None)
    module = FilterModule()
    group = FilterRuleGroup(name="国产剧", rule_string=expression)
    module.rulehelper.get_rule_group_by_media = lambda **_kwargs: [group]
    module.rule_set = {**BUILTIN_RULE_SET, " Pre Candidate": {"include": "QHstudIo"}}
    return module


def test_runtime_reports_full_legacy_id_and_rule_group(monkeypatch) -> None:
    """实际解析失败时应报告完整旧 ID 和规则组，而非解析器截断片段。"""
    module = _invalid_filter_module(monkeypatch)
    with pytest.raises(ValueError) as caught:
        module.filter_torrents(
            rule_groups=["国产剧"],
            torrent_list=[TorrentInfo(title="Show S01E01 1080p QHstudIo", description="")],
        )
    message = str(caught.value)
    assert "国产剧" in message
    assert "' Pre Candidate'" in message
    assert "无效的规则 ID" in message
    assert "found 'Candidate'" not in message


def test_unreached_bad_priority_does_not_change_high_priority_match(monkeypatch) -> None:
    """高优先级已命中时不预解析未执行的坏层级，未命中时才报告配置错误。"""
    module = _invalid_filter_module(monkeypatch, "1080P >  Pre Candidate")
    matched = TorrentInfo(title="Show S01E01 1080p", description="")
    assert module.filter_torrents(["国产剧"], [matched]) == [matched]
    with pytest.raises(ValueError, match="Pre Candidate"):
        module.filter_torrents(
            ["国产剧"], [TorrentInfo(title="Show S01E01 720p", description="")]
        )


def test_diagnostics_only_checks_selected_applicable_rule_groups(monkeypatch) -> None:
    """无效电影规则不污染电视剧搜索，未选择的组也不进入当前诊断。"""
    helper = rule_helper.RuleHelper()
    groups = [
        FilterRuleGroup(name="国产剧", media_type="电视剧", rule_string="1080P &  Pre Candidate"),
        FilterRuleGroup(name="电影", media_type="电影", rule_string="!Missing"),
        FilterRuleGroup(name="未选择", rule_string="!OtherMissing"),
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    monkeypatch.setattr(helper, "get_custom_rules", lambda: [CustomRule(id=" Pre Candidate", name="候补组")])
    warnings = helper.diagnose_rule_groups(
        media=MediaInfo(type=MediaType.TV), group_names=["国产剧", "电影", "已删除", "国产剧"],
    )
    assert [warning.model_dump() for warning in warnings] == [{
        "code": "rule_id_invalid", "rule_group": "国产剧", "rule_ids": [" Pre Candidate"],
    }]
    assert helper.diagnose_rule_groups(media=None, group_names=[]) == []


def test_search_diagnostics_contains_rule_configuration_warning(monkeypatch) -> None:
    """完整搜索诊断应合入规则配置告警，且不增加资源拒绝计数。"""
    monkeypatch.setattr(rule_helper.RuleHelper, "get_rule_groups", staticmethod(lambda: [
        FilterRuleGroup(name="国产剧", media_type="电视剧", rule_string="1080P &  Pre Candidate"),
    ]))
    monkeypatch.setattr(rule_helper.RuleHelper, "get_custom_rules", staticmethod(lambda: [
        CustomRule(id=" Pre Candidate", name="候补组"),
    ]))
    diagnostics = SearchChain._build_search_diagnostics(
        observations=[], candidates=1, retained=0, counts=Counter(),
        mediainfo=MediaInfo(type=MediaType.TV), rule_groups=["国产剧"],
    ).model_dump(mode="json")
    assert diagnostics["warnings"] == [{
        "code": "rule_id_invalid", "rule_group": "国产剧", "rule_ids": [" Pre Candidate"],
    }]
    assert diagnostics["stage_counts"] == {"candidates": 1, "retained": 0}
    assert diagnostics["rejection_reasons"] == []


def test_warning_omits_empty_rule_ids() -> None:
    """没有关联 ID 的来源或语法告警不输出空 rule_ids 键。"""
    assert "rule_ids" not in SearchDiagnosticWarning(code="provider_error").model_dump()
    assert SearchDiagnosticWarning(code="rule_id_missing", rule_ids=["Missing"]).model_dump()["rule_ids"] == ["Missing"]


def test_migrated_expression_remains_parseable() -> None:
    """合法自定义 ID 仍沿用现有解析器，不另建语法。"""
    assert RuleParser().parse("1080P & MTeam").as_list()
