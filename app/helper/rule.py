from typing import List, Optional

from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import FilterRuleGroup, CustomRule, SearchDiagnosticRuleGroup
from app.schemas.types import SystemConfigKey


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
