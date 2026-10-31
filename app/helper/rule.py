from typing import List, Optional

from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import FilterRuleGroup, CustomRule
from app.schemas.types import SystemConfigKey

# 内置可信制作组/字幕组默认名单：用户从未保存过名单时生效，保存空列表则视为不信任任何组。
DEFAULT_TRUSTED_RELEASE_GROUPS: tuple[str, ...] = (
    "7³ACG", "ADE", "ADWeb", "AilMWeb", "NEST", "Nix-Raws", "ANi", "Ao", "APTX4869", "ARiC",
    "beAst", "BeiTai", "BMDru", "btshd", "BYNDR", "c0kE", "CatEDU", "cfandora", "CHD", "CHDWEB",
    "CMCT", "CMCTV", "DBTV", "Dream", "EDITH", "Erai-raws", "ETHEL", "FLUX", "FraMeSToR", "FRDS",
    "FROGE", "FROGWeb", "GTR", "HDChina", "HDCTV", "HHWEB", "HONE", "jsum", "Kitsune", "LeagueWEB",
    "LHD", "Lilith-Raws", "Little Subbers!", "LittleBakas!", "LoliHouse", "LowPower-Raws", "MCE汉化组",
    "MingYSub", "Moozzi2", "MTeam", "MWeb", "NC-Raws", "NTb", "Oldboys", "OPS", "OurTV", "PbK",
    "Philosophy-raws", "playWEB", "Prejudice-Studio", "PTer", "PTerWEB", "PTH", "QHstudIo", "Rainbaby",
    "ReinForce", "RNML", "RO", "Sentai-Raws", "Snow-Raws", "SubsPlease", "Sunny", "SweetSub", "Telesto",
    "Toonshub", "UHA-WINGS", "VARYG", "VCB-Studio", "WiKi", "OPFans枫雪动漫", "爱恋字幕社", "北宇治字幕组",
    "波洛咖啡厅", "波斯猫字幕组", "拨雪寻春", "银色子弹字幕组", "澄空学园", "动漫国字幕组", "晚街与灯",
    "风车字幕组", "风之圣殿字幕组", "枫叶字幕组", "黑白字幕组", "幻月字幕组", "幻樱字幕组", "幻之字幕组",
    "极影字幕社", "極彩字幕组", "猎户手抄部", "离谱Sub", "绿茶字幕组", "漫猫字幕社", "猫恋汉化组", "毛玉牛乳",
    "萌樱字幕组", "魔星字幕团", "喵萌奶茶屋", "千夏字幕组", "今晚月色真美", "轻之国度字幕组", "樱桃花字幕组",
    "三明治摆烂组", "霜庭云花Sub", "豌豆字幕组", "西农YUI字幕组", "雪飘工作室", "悠哈璃羽字幕社", "云光字幕组",
    "织梦字幕组", "诸神字幕组", "桜都字幕组",
)


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
            elif media and not group.category and group.media_type == media.type.value:
                ret_groups.append(group)
            elif media and group.category == media.category:
                ret_groups.append(group)
        return ret_groups

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

    @staticmethod
    def get_trusted_release_groups() -> List[str]:
        """
        获取可信制作组/字幕组名单

        从未保存过名单时返回内置默认名单；保存过的名单按原顺序去除首尾空白、空行和重复项。
        :return: 可信组名列表，空列表表示不信任任何组
        """
        groups = SystemConfigOper().get(SystemConfigKey.TrustedReleaseGroups)
        if groups is None:
            return list(DEFAULT_TRUSTED_RELEASE_GROUPS)
        return list(dict.fromkeys(name.strip() for name in groups if isinstance(name, str) and name.strip()))
