import re
from typing import TYPE_CHECKING, Iterable, List, Optional

from pyparsing import ParseBaseException

from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import FilterRuleGroup, CustomRule, SearchDiagnosticRuleGroup
from app.schemas.types import SystemConfigKey

if TYPE_CHECKING:
    from app.schemas.search import SearchDiagnosticWarning


class RuleStringValidationError(ValueError):
    """
    不包含解析器输入摘要的规则配置定位信息。

    :ivar code: 区分无效 ID、悬空引用和表达式语法错误的稳定代码
    :ivar rule_ids: 完整的相关 ID，保留旧 ID 中的空白，语法错误时为空
    """

    def __init__(self, code: str, rule_ids: Iterable[str] = ()) -> None:
        """构造可写入日志的中文说明，完整 ID 不依赖解析器截断的异常文本。"""
        self.code = code
        self.rule_ids = tuple(dict.fromkeys(rule_ids))
        labels = ", ".join(repr(rule_id) for rule_id in self.rule_ids)
        messages = {
            "rule_id_invalid": f"引用了无效的规则 ID：{labels}；ID 仅支持英文字母和数字，且至少包含一个字母",
            "rule_id_missing": f"引用了不存在的规则 ID：{labels}",
            "rule_syntax_invalid": "规则表达式语法错误，请检查运算符、括号和优先级层级",
        }
        super().__init__(messages[code])


class RuleHelper:
    """
    规划帮助类
    """

    @staticmethod
    def get_rule_groups() -> List[FilterRuleGroup]:
        """
        获取用户所有规则组
        """
        rule_groups: List[dict] = SystemConfigOper().get(SystemConfigKey.UserFilterRuleGroups)
        if not rule_groups:
            return []
        return [FilterRuleGroup(**group) for group in rule_groups]

    def get_rule_group(self, group_name: str) -> Optional[FilterRuleGroup]:
        """
        获取规则组
        """
        rule_groups = self.get_rule_groups()
        for group in rule_groups:
            if group.name == group_name:
                return group
        return None

    def get_rule_group_by_media(self, media: MediaInfo = None, group_names: list = None) -> List[FilterRuleGroup]:
        """
        根据媒体信息获取规则组
        """
        ret_groups = []
        rule_groups = self.get_rule_groups()
        if group_names:
            rule_groups = [group for group in rule_groups if group.name in group_names]
        for group in rule_groups:
            if not group.media_type:
                ret_groups.append(group)
            elif media and not group.category and media.type and group.media_type == media.type.value:
                ret_groups.append(group)
            elif media and group.category == media.category:
                ret_groups.append(group)
        return ret_groups

    def explain_rule_groups(
            self,
            media: Optional[MediaInfo] = None,
            group_names: Optional[List[str]] = None,
    ) -> List[SearchDiagnosticRuleGroup]:
        """
        使用现有规则组选择策略说明所选规则组的适用范围，不执行资源过滤。

        :param media: 当前媒体上下文
        :param group_names: 本次搜索选择的规则组名称
        :return: 按选择去重顺序排列的规则适用性诊断
        """
        if not group_names:
            return []
        configured = {group.name: group for group in self.get_rule_groups()}
        effective = {
            group.name
            for group in self.get_rule_group_by_media(media=media, group_names=group_names)
        }
        diagnostics = []
        for name in dict.fromkeys(group_names):
            group = configured.get(name)
            if group is None:
                status = "skipped"
                reason = "rule_group_missing"
            elif name not in effective:
                status = "skipped"
                reason = "rule_group_not_applicable"
            else:
                status = "effective"
                reason = None
            diagnostics.append(SearchDiagnosticRuleGroup(
                name=name,
                status=status,
                reason=reason,
                media_type=group.media_type if group else None,
                categories=[group.category] if group and group.category else [],
            ))
        return diagnostics

    def diagnose_rule_groups(
            self, *, media: Optional[MediaInfo], group_names: List[str],
    ) -> List["SearchDiagnosticWarning"]:
        """
        检查本次所选且适用的规则组配置，不将配置告警推测为资源拒绝。

        :param media: 本次过滤使用的媒体上下文，标题搜索传 None
        :param group_names: 本次选择的规则组名称；空列表不检查其它组
        :return: 完整 ID 与规则组定位组成的安全告警，不含原始解析异常
        """
        if not group_names:
            return []
        from app.modules.filter.builtin_rules import BUILTIN_RULE_SET
        from app.schemas.search import SearchDiagnosticWarning

        available_ids = [
            *BUILTIN_RULE_SET,
            *(rule.id for rule in self.get_custom_rules() if rule.id is not None),
        ]
        warnings = []
        for group in self.get_rule_group_by_media(media=media, group_names=group_names):
            issue = describe_rule_string_error(group.rule_string or "", available_ids)
            if issue is not None:
                warnings.append(SearchDiagnosticWarning(
                    code=issue.code, rule_group=group.name, rule_ids=list(issue.rule_ids),
                ))
        return warnings

    @staticmethod
    def get_custom_rules() -> List[CustomRule]:
        """
        获取用户所有自定义规则
        """
        rules: List[dict] = SystemConfigOper().get(SystemConfigKey.CustomFilterRules)
        if not rules:
            return []
        return [CustomRule(**rule) for rule in rules]

    def get_custom_rule(self, rule_id: str) -> Optional[CustomRule]:
        """
        获取自定义规则
        """
        rules = self.get_custom_rules()
        for rule in rules:
            if rule.id == rule_id:
                return rule
        return None


def describe_rule_string_error(
        rule_string: str, available_rule_ids: Iterable[str],
) -> Optional[RuleStringValidationError]:
    """
    按完整操作数诊断规则表达式，避免将含空格的旧 ID 拆成多个错误片段。

    本检查只诊断配置；运行时仍按实际需要延迟解析层级，不能因未执行的坏层级
    改变高优先级命中或后续规则组的短路语义。

    :param rule_string: 需要检查的完整规则表达式
    :param available_rule_ids: 当前内置和自定义 ID，旧 ID 保留原始空白
    :return: 首类配置错误；全部语法和引用有效时返回 None
    """
    available = set(available_rule_ids)
    aliases = {}
    for rule_id in sorted(available):
        aliases.setdefault(rule_id.strip(), []).append(rule_id)
    operands = [span.strip() for span in re.findall(r"[^!&|()>]+", rule_string) if span.strip()]
    invalid_ids = []
    missing_ids = []
    for operand in operands:
        originals = [operand] if operand in available else aliases.get(operand, [operand])
        invalid = [
            rule_id for rule_id in originals
            if not re.fullmatch(r"[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*", rule_id)
        ]
        if invalid:
            invalid_ids.extend(invalid)
        elif operand not in available:
            missing_ids.append(operand)
    if invalid_ids:
        return RuleStringValidationError("rule_id_invalid", invalid_ids)
    if missing_ids:
        return RuleStringValidationError("rule_id_missing", missing_ids)
    # 延迟导入以避免过滤模块加载 RuleHelper 时形成循环导入。
    from app.modules.filter.RuleParser import RuleParser

    parser = RuleParser()
    for level in rule_string.split(">"):
        if not level.strip():
            return RuleStringValidationError("rule_syntax_invalid")
        try:
            parser.parse(level.strip())
        except ParseBaseException:
            return RuleStringValidationError("rule_syntax_invalid")
    return None
