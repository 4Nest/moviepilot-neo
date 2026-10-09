from typing import Optional

import regex as re

from app.helper.words import WordsHelper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class ReleaseGroupsMatcher(metaclass=Singleton):
    """
    识别制作组、字幕组
    """
    # 内置组
    RELEASE_GROUPS: dict = {
        "0ff": ['FF(?:(?:A|WE)B|CD|E(?:DU|B)|TV)'],
        "1pt": [],
        "52pt": [],
        "audiences": ['Audies', 'AD(?:Audio|E(?:book|)|Music|Web)'],
        "azusa": [],
        "beitai": ['BeiTai'],
        "btschool": ['Bts(?:CHOOL|HD|PAD|TV)', 'Zone'],
        "carpt": ['CarPT'],
        "chdbits": ['CHD(?:Bits|PAD|(?:|HK)TV|WEB|)', 'StBOX', 'OneHD', 'Lee', 'xiaopie'],
        "discfan": [],
        "dragonhd": [],
        "eastgame": ['(?:(?:iNT|(?:HALFC|Mini(?:S|H|FH)D))-|)TLF'],
        "filelist": [],
        "gainbound": ['(?:DG|GBWE)B'],
        "hares": ['Hares(?:(?:M|T)V|Web|)'],
        "hd4fans": [],
        "hdarea": ['HDA(?:pad|rea|TV)', 'EPiC'],
        "hdatmos": [],
        "hdbd": [],
        "hdchina": ['HDC(?:hina|TV|)', 'k9611', 'tudou', 'iHD'],
        "hddolby": ['D(?:ream|BTV)', '(?:HD|QHstudI)o'],
        "hdfans": ['beAst(?:TV|)'],
        "hdhome": ['HDH(?:ome|Pad|TV|WEB|)'],
        "hdpt": ['HDPT(?:Web|)'],
        "hdsky": ['HDS(?:ky|TV|Pad|WEB|)', 'AQLJ'],
        "hdtime": [],
        "HDU": [],
        "hdvideo": [],
        "hdzone": ['HDZ(?:one|)'],
        "hhanclub": ['HHWEB'],
        "hitpt": [],
        "htpt": ['HTPT'],
        "iptorrents": [],
        "joyhd": [],
        "keepfrds": ['FRDS', 'Yumi', 'cXcY'],
        "lemonhd": ['L(?:eague(?:(?:C|H)D|(?:M|T)V|NF|WEB)|HD)', 'i18n', 'CiNT'],
        "mteam": ['MTeam(?:TV|)', 'MPAD', 'MWeb'],
        "nanyangpt": [],
        "nicept": [],
        "oshen": [],
        "ourbits": ['Our(?:Bits|TV)', 'FLTTH', 'Ao', 'PbK', 'MGs', 'iLove(?:HD|TV)'],
        "panda": ['Panda', 'AilMWeb'],
        "piggo": ['PiGo(?:NF|(?:H|WE)B)'],
        "ptchina": [],
        "pterclub": ['PTer(?:DIY|Game|(?:M|T)V|WEB|)'],
        "pthome": ['PTH(?:Audio|eBook|music|ome|tv|WEB|)'],
        "ptmsg": [],
        "ptsbao": ['PTsbao', 'OPS', 'F(?:Fans(?:AIeNcE|BD|D(?:VD|IY)|TV|WEB)|HDMv)', 'SGXT'],
        "pttime": [],
        "putao": ['PuTao'],
        "soulvoice": [],
        "springsunday": ['CMCT(?:V|)'],
        "sharkpt": ['Shark(?:WEB|DIY|TV|MV|)'],
        "tccf": [],
        "tjupt": ['TJUPT'],
        "totheglory": ['TTG', 'WiKi', 'NGB', 'DoA', '(?:ARi|ExRE)N'],
        "U2": [],
        "ultrahd": [],
        "others": ['B(?:MDru|eyondHD|TN)', 'C(?:fandora|trlhd|MRG)', 'DON', 'EVO', 'FLUX', 'HONE(?:yG|)',
                   'N(?:oGroup|T(?:b|G))', 'PandaMoon', 'SMURF', 'T(?:EPES|aengoo|rollHD )'],
        "anime": ['ANi', 'HYSUB', 'KTXP', 'LoliHouse', 'MCE', 'Nekomoe kissaten', 'SweetSub', 'MingY',
                  '(?:Lilith|NC)-Raws', '织梦字幕组', '枫叶字幕组', '猎户手抄部', '喵萌奶茶屋', '漫猫字幕社',
                  '霜庭云花Sub', '北宇治字幕组', '氢气烤肉架', '云歌字幕组', '萌樱字幕组', '极影字幕社',
                  '悠哈璃羽字幕社',
                  '❀拨雪寻春❀', '沸羊羊(?:制作|字幕组)', '(?:桜|樱)都字幕组'],
        "forge": ['FROG(?:E|Web|)'],
        "ubits": ['UB(?:its|WEB|TV)'],
    }

    def __init__(self):
        release_groups = []
        for site_groups in self.RELEASE_GROUPS.values():
            for release_group in site_groups:
                release_groups.append(release_group)
        self.__release_groups = '|'.join(release_groups)
        self.__groups_re_cache = {}

    def get_release_groups(self) -> str:
        """
        返回内置与用户自定义制作组组成的匹配规则。
        """
        custom_release_groups = list(filter(None, WordsHelper.get_merged_words(SystemConfigKey.CustomReleaseGroups)))
        if custom_release_groups:
            custom_release_groups_str = '|'.join(custom_release_groups)
            return f"{self.__release_groups}|{custom_release_groups_str}"
        return self.__release_groups

    def __get_groups_re(self, groups: str):
        """
        发布组规则通常很长，按规则文本缓存编译结果，避免每个标题都重复编译。
        """
        groups_re = self.__groups_re_cache.get(groups)
        if not groups_re:
            groups_re = re.compile(r"(?<=[-@\[￡【&])(?:(?:%s))(?=$|[@.\s\]\[】&])" % groups, re.I)
            self.__groups_re_cache[groups] = groups_re
        return groups_re

    def match(self, title: Optional[str] = None, groups: Optional[str] = None) -> str:
        """
        :param title: 资源标题或文件名
        :param groups: 制作组/字幕组
        :return: 匹配结果
        """
        if not title:
            return ""
        if not groups:
            groups = self.get_release_groups()
        title = f"{title} "
        groups_re = self.__get_groups_re(groups)
        unique_groups = []
        resource_team = ""
        previous_end = None
        for match in groups_re.finditer(title):
            item_str = match.group()
            if item_str in unique_groups:
                continue
            if resource_team:
                between_groups = title[previous_end:match.start()]
                # 只有直接相连的组名沿用分隔符，独立标签仍按原有方式合并。
                resource_team += between_groups if between_groups.strip() in ("&", "@") else "@"
            resource_team += item_str
            previous_end = match.end()
            unique_groups.append(item_str)

        return self.original_joint_group(title, resource_team) or resource_team

    @staticmethod
    def original_joint_group(title: Optional[str], resource_team: Optional[str]) -> Optional[str]:
        """
        验证原标题中的联合组标签或连续组名，保留 & 并补足标签中的未知成员。
        """
        if not title or not resource_team or "&" not in title:
            return None
        groups = {group.strip().casefold() for group in re.split(r"[@&]", resource_team) if group.strip()}
        if not groups:
            return None
        for match in re.finditer(r"\[([^\[\]]+&[^\[\]]+)\]|【([^【】]+&[^【】]+)】", title):
            original_group = (match.group(1) or match.group(2)).strip()
            members = [member.strip().casefold() for member in re.split(r"[@&]", original_group)]
            # 标签必须包含全部已识别组名，避免从字幕或技术标签中补出制作组。
            if all(members) and groups.issubset(members) and not re.search(r'[/\\:*?"<>|]', original_group):
                return original_group
        if len(groups) > 1:
            # 文件名末尾的 A&B 没有括号，也须从连续组名中恢复真实分隔符。
            member_pattern = "(?:" + "|".join(re.escape(group) for group in groups) + ")"
            for match in re.finditer(
                rf"(?<!\w){member_pattern}(?:\s*[@&]\s*{member_pattern})+(?![\w-])", title, re.I,
            ):
                original_group = match.group()
                members = {member.strip().casefold() for member in re.split(r"[@&]", original_group)}
                if "&" in original_group and groups.issubset(members):
                    return original_group
        return None
