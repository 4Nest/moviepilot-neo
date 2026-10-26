from dataclasses import dataclass
from typing import Union, Optional, List, Self

import cn2an
import regex as re

from app.log import logger
from app.schemas.types import MediaType
from app.utils.string import StringUtils


TITLE_EPISODE_RE = re.compile(
    r"\bEpisode\s+(\d{1,4})(?!\d)",
    re.IGNORECASE,
)
SUBTITLE_SEASON_RE = re.compile(
    r"(?<![全共]\s*)(?<!(?:不包含|不包括|不含)\s*)(?:第|[\s\[【])\s*([0-9一二三四五六七八九十百零]+)\s*(?:季?\s*[-~～–—至]\s*第?\s*([0-9一二三四五六七八九十百零]+)\s*)?季",
    re.IGNORECASE,
)
SUBTITLE_SEASON_ALL_RE = re.compile(
    r"[全共]\s*([0-9一二三四五六七八九十百零]+)\s*季",
    re.IGNORECASE,
)
SUBTITLE_EPISODE_RE = re.compile(
    r"(?<![全共]\s*)(?<!(?:不包含|不包括|不含)\s*)(?:第|[\s\[【])\s*(?:EP?)?([0-9一二三四五六七八九十百零]+)\s*(?:[集话話期幕]?\s*[-~～–—至]\s*第?\s*([0-9一二三四五六七八九十百零]+)\s*)?([集话話期幕])(?!\s*全(?![0-9一二三四五六七八九十百零]))",
    re.IGNORECASE,
)
SUBTITLE_EPISODE_ALL_RE = re.compile(
    r"([0-9一二三四五六七八九十百零]+)\s*集\s*全|[全共]\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]",
    re.IGNORECASE,
)
SUBTITLE_EPISODE_RANGE_FIN_RE = re.compile(
    r"(?<!\d)\[?\s*(\d{1,4})\s*[-~～–—至]\s*(\d{1,4})\s*(?:(?:Fin|End)(?![a-z0-9])|完结(?![\u4e00-\u9fff]))(?:\s*\](?!\d)|(?!\s*(?:\]\d|\d))\s*)",
    re.IGNORECASE,
)
DESCRIPTION_INFO_RE = re.compile(
    r"(?:剧情简介|劇情簡介|类型|類型|主演|演员|演員|导演|導演|编剧|編劇|简介|簡介|剧情|劇情|备注|備註|豆瓣|上映|首播)\s*[:：]",
    re.IGNORECASE,
)
DESCRIPTION_NOISE_RE = re.compile(
    r"预计|預計|计划|計劃|每周|每週|将于|將於|下周|下週|预告|預告|另见|另見|另附|附赠|附贈|前情|回顾|回顧|剧情|劇情|简介|簡介|播出进度|更新至|更新到",
    re.IGNORECASE,
)
VIDEO_BIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<bit>8|10|12|16)[\s._-]*bits?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _subtitle_number(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(cn2an.cn2an(value.strip(), mode="smart"))
    except (ValueError, TypeError) as err:
        logger.debug(f"副标题数字无效：{value} - {err}")
        return None


@dataclass
class MetaBase(object):
    """
    媒体信息基类
    """
    # 是否处理的文件
    isfile: bool = False
    # 原标题字符串（未经过识别词处理）
    title: str = ""
    # 识别用字符串（经过识别词处理后）
    org_string: Optional[str] = None
    # 副标题
    subtitle: Optional[str] = None
    # 类型 电影、电视剧
    type: MediaType = MediaType.UNKNOWN
    # 识别的中文名
    cn_name: Optional[str] = None
    # 识别的英文名
    en_name: Optional[str] = None
    # 未应用识别词时识别出的名称
    original_name: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 总季数
    total_season: int = 0
    # 识别的开始季 数字
    begin_season: Optional[int] = None
    # 识别的结束季 数字
    end_season: Optional[int] = None
    # 总集数
    total_episode: int = 0
    # 识别的开始集
    begin_episode: Optional[int] = None
    # 识别的结束集
    end_episode: Optional[int] = None
    # Partx Cd Dvd Disk Disc
    part: Optional[str] = None
    # 识别的资源类型
    resource_type: Optional[str] = None
    # 识别的效果
    resource_effect: Optional[str] = None
    # 识别的分辨率
    resource_pix: Optional[str] = None
    # 识别的制作组/字幕组
    resource_team: Optional[str] = None
    # 识别的自定义占位符
    customization: Optional[str] = None
    # 识别的流媒体平台
    web_source: Optional[str] = None
    # 视频编码
    video_encode: Optional[str] = None
    # 视频位深
    video_bit: Optional[str] = None
    # 音频编码
    audio_encode: Optional[str] = None
    # 应用的识别词信息
    apply_words: Optional[List[str]] = None
    # 附加信息
    tmdbid: int = None
    doubanid: str = None
    bangumiid: int = None
    anilistid: int = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None
    episode_group: Optional[str] = None
    # 帧率信息（纯数值）
    fps: Optional[int] = None


    # 副标题解析
    _subtitle_flag = False

    def __init__(self, title: str, subtitle: str = None, isfile: bool = False):
        if not title:
            return
        self.org_string = title.strip() if title else None
        self.subtitle = subtitle.strip() if subtitle else None
        self.isfile = isfile

    @property
    def name(self) -> str:
        """
        返回名称
        """
        if self.cn_name and StringUtils.is_all_chinese(self.cn_name):
            return self.cn_name
        elif self.en_name:
            return self.en_name
        elif self.cn_name:
            return self.cn_name
        return ""

    @name.setter
    def name(self, name: str):
        """
        设置名称
        """
        if StringUtils.is_all_chinese(name):
            self.cn_name = name
        else:
            self.en_name = name
            self.cn_name = None

    def init_description(self, description: str) -> None:
        """只解析副标题中的资源信息，隔离剧情、更新计划与其他资源说明。"""
        for section in re.split(r"[|｜\n]", description):
            header = DESCRIPTION_INFO_RE.split(section, maxsplit=1)[0]
            for alias in header.split("/"):
                if not DESCRIPTION_NOISE_RE.search(alias):
                    self.init_subtitle(alias)


    def init_subtitle(self, title_text: str) -> None:
        """按字段补充季集，已确定的单集或范围不与其他描述拼接。"""
        if not title_text:
            return
        title_text = f" {title_text} "
        season = SUBTITLE_SEASON_RE.search(title_text)
        if season:
            begin = _subtitle_number(season.group(1))
            end = _subtitle_number(season.group(2))
            if begin is None or not 0 <= begin <= 100 or (end is not None and not begin <= end <= 100):
                return
            if self.begin_season is not None and self.begin_season != begin:
                return
            if self.begin_season is None:
                self.begin_season, self.end_season = begin, end
                self.total_season = end - begin + 1 if end is not None else 1
                self.type = MediaType.TV
                self._subtitle_flag = True
        else:
            season_all = SUBTITLE_SEASON_ALL_RE.search(title_text)
            total = _subtitle_number(season_all.group(1)) if season_all else None
            if total is not None and 1 <= total <= 100 \
                    and self.begin_season is None and self.begin_episode is None:
                self.begin_season, self.end_season = 1, total
                self.total_season = total
                self.type = MediaType.TV
                self._subtitle_flag = True
        if self.begin_episode is not None or self.total_episode > 0:
            return

        # 集/话优先于日文别名中的期数，同类标记优先读取前面的资源编号。
        candidates = []
        for match in SUBTITLE_EPISODE_RE.finditer(title_text):
            begin = _subtitle_number(match.group(1))
            end = _subtitle_number(match.group(2))
            if begin is None or begin >= 10000 \
                    or (end is not None and not 1 <= begin <= end < 10000):
                continue
            candidates.append((int(match.group(3) == "期"), match.start(), begin, end,
                               end - begin + 1 if end is not None else 1))
        for match in TITLE_EPISODE_RE.finditer(title_text):
            candidates.append((0, match.start(), int(match.group(1)), None, 1))
        for match in SUBTITLE_EPISODE_RANGE_FIN_RE.finditer(title_text):
            begin, end = int(match.group(1)), int(match.group(2))
            if 1 <= begin <= end < 10000 and not (begin >= 1900 and end <= 2155):
                candidates.append((0, match.start(), begin, end, end - begin + 1))
        for match in SUBTITLE_EPISODE_ALL_RE.finditer(title_text):
            total = _subtitle_number(match.group(1) or match.group(2))
            if total is not None and 1 <= total < 10000:
                candidates.append((2, match.start(), None, None, total))
        if candidates:
            _, _, begin, end, total = min(candidates, key=lambda item: item[:2])
            self.begin_episode, self.end_episode, self.total_episode = begin, end, total
            self.type = MediaType.TV
            self._subtitle_flag = True

    @property
    def season(self) -> str:
        """
        返回开始季、结束季字符串，确定是剧集没有季的返回S01
        """
        if self.begin_season is not None:
            return "S%s" % str(self.begin_season).rjust(2, "0") \
                if self.end_season is None \
                else "S%s-S%s" % \
                     (str(self.begin_season).rjust(2, "0"),
                      str(self.end_season).rjust(2, "0"))
        else:
            if self.type == MediaType.TV:
                return "S01"
            else:
                return ""

    @property
    def sea(self) -> str:
        """
        返回开始季字符串，确定是剧集没有季的返回空
        """
        if self.begin_season is not None:
            return self.season
        else:
            return ""

    @property
    def season_seq(self) -> str:
        """
        返回begin_season 的数字，电视剧没有季的返回1
        """
        if self.begin_season is not None:
            return str(self.begin_season)
        else:
            if self.type == MediaType.TV:
                return "1"
            else:
                return ""

    @property
    def season_list(self) -> List[int]:
        """
        返回季的数组
        """
        if self.begin_season is None:
            if self.type == MediaType.TV:
                return [1]
            else:
                return []
        elif self.end_season is not None:
            return [season for season in range(self.begin_season, self.end_season + 1)]
        else:
            return [self.begin_season]

    @property
    def episode(self) -> str:
        """
        返回开始集、结束集字符串
        """
        if self.begin_episode is not None:
            return "E%s" % str(self.begin_episode).rjust(2, "0") \
                if self.end_episode is None \
                else "E%s-E%s" % \
                     (
                         str(self.begin_episode).rjust(2, "0"),
                         str(self.end_episode).rjust(2, "0"))
        else:
            return ""

    @property
    def episode_list(self) -> List[int]:
        """
        返回集的数组
        """
        if self.begin_episode is None:
            return []
        elif self.end_episode is not None:
            return [episode for episode in range(self.begin_episode, self.end_episode + 1)]
        else:
            return [self.begin_episode]

    @property
    def episodes(self) -> str:
        """
        返回集的并列表达方式，用于支持单文件多集
        """
        return "E%s" % "E".join(str(episode).rjust(2, '0') for episode in self.episode_list)

    @property
    def episode_seqs(self) -> str:
        """
        返回单文件多集的集数表达方式，用于支持单文件多集
        """
        episodes = self.episode_list
        if episodes:
            # 集 xx
            if len(episodes) == 1:
                return str(episodes[0])
            else:
                return "%s-%s" % (episodes[0], episodes[-1])
        else:
            return ""

    @property
    def episode_seq(self) -> str:
        """
        返回begin_episode 的数字
        """
        episodes = self.episode_list
        if episodes:
            return str(episodes[0])
        else:
            return ""

    @property
    def season_episode(self) -> str:
        """
        返回季集字符串
        """
        if self.type == MediaType.TV:
            seaion = self.season
            episode = self.episode
            if seaion and episode:
                return "%s %s" % (seaion, episode)
            elif seaion:
                return "%s" % seaion
            elif episode:
                return "%s" % episode
        else:
            return ""
        return ""

    @property
    def resource_term(self) -> str:
        """
        返回资源类型字符串，含分辨率
        """
        ret_string = ""
        if self.resource_type:
            ret_string = f"{ret_string} {self.resource_type}"
        if self.resource_effect:
            ret_string = f"{ret_string} {self.resource_effect}"
        if self.resource_pix:
            ret_string = f"{ret_string} {self.resource_pix}"
        return ret_string

    @property
    def edition(self) -> str:
        """
        返回资源类型字符串，不含分辨率
        """
        ret_string = ""
        if self.resource_type:
            ret_string = f"{ret_string} {self.resource_type}"
        if self.resource_effect:
            ret_string = f"{ret_string} {self.resource_effect}"
        return ret_string.strip()

    @property
    def release_group(self) -> str:
        """
        返回发布组/字幕组字符串
        """
        if self.resource_team:
            return self.resource_team
        else:
            return ""

    @property
    def video_term(self) -> str:
        """
        返回视频编码
        """
        return self.video_encode or ""

    @property
    def audio_term(self) -> str:
        """
        返回音频编码
        """
        return self.audio_encode or ""

    @property
    def frame_rate(self) -> int:
        """
        返回帧率信息
        """
        return self.fps or None

    @staticmethod
    def extract_video_bit(value: Optional[str]) -> Optional[str]:
        """
        从标题或编码文本中提取视频位深标签。
        """
        if not value:
            return None
        bit_match = VIDEO_BIT_RE.search(value)
        if not bit_match:
            return None
        return f"{bit_match.group('bit')}bit"

    def is_in_season(self, season: Union[list, int, str]) -> bool:
        """
        是否包含季
        """
        if isinstance(season, list):
            if self.end_season is not None:
                meta_season = list(range(self.begin_season, self.end_season + 1))
            else:
                if self.begin_season is not None:
                    meta_season = [self.begin_season]
                else:
                    meta_season = [1]

            return set(meta_season).issuperset(set(season))
        else:
            if self.end_season is not None:
                return self.begin_season <= int(season) <= self.end_season
            else:
                if self.begin_season is not None:
                    return int(season) == self.begin_season
                else:
                    return int(season) == 1

    def is_in_episode(self, episode: Union[list, int, str]) -> bool:
        """
        是否包含集
        """
        if isinstance(episode, list):
            if self.end_episode is not None:
                meta_episode = list(range(self.begin_episode, self.end_episode + 1))
            else:
                meta_episode = [self.begin_episode]
            return set(meta_episode).issuperset(set(episode))
        else:
            if self.end_episode is not None:
                return self.begin_episode <= int(episode) <= self.end_episode
            else:
                return int(episode) == self.begin_episode

    def set_season(self, sea: Union[list, int, str]):
        """
        更新季
        """
        if not sea:
            return
        if isinstance(sea, list):
            if len(sea) == 1 and str(sea[0]).isdigit():
                self.begin_season = int(sea[0])
                self.end_season = None
            elif len(sea) > 1 and str(sea[0]).isdigit() and str(sea[-1]).isdigit():
                self.begin_season = int(sea[0])
                self.end_season = int(sea[-1])
        elif str(sea).isdigit():
            self.begin_season = int(sea)
            self.end_season = None

    def set_episode(self, ep: Union[list, int, str]):
        """
        更新集
        """
        if not ep:
            return
        if isinstance(ep, list):
            if len(ep) == 1 and str(ep[0]).isdigit():
                self.begin_episode = int(ep[0])
                self.end_episode = None
            elif len(ep) > 1 and str(ep[0]).isdigit() and str(ep[-1]).isdigit():
                self.begin_episode = int(ep[0])
                self.end_episode = int(ep[-1])
                self.total_episode = (self.end_episode - self.begin_episode) + 1
        elif str(ep).isdigit():
            self.begin_episode = int(ep)
            self.end_episode = None

    def set_episodes(self, begin: int, end: int):
        """
        设置开始集结束集
        """
        if begin:
            self.begin_episode = begin
        if end:
            self.end_episode = end
        if self.begin_episode and self.end_episode:
            self.total_episode = (self.end_episode - self.begin_episode) + 1

    def merge(self, meta: Self):
        """
        合并Meta信息
        """
        # 类型
        if self.type == MediaType.UNKNOWN \
                and meta.type != MediaType.UNKNOWN:
            self.type = meta.type
        # 名称
        if not self.name:
            self.cn_name = meta.cn_name
            self.en_name = meta.en_name
        # 未应用识别词时识别出的名称
        if not self.original_name:
            self.original_name = meta.original_name
        # 年份
        if not self.year:
            self.year = meta.year
        # 季
        if (self.type == MediaType.TV
                and self.begin_season is None):
            self.begin_season = meta.begin_season
            self.end_season = meta.end_season
            self.total_season = meta.total_season
        # 开始集
        if (self.type == MediaType.TV
                and self.begin_episode is None):
            self.begin_episode = meta.begin_episode
            self.end_episode = meta.end_episode
            self.total_episode = meta.total_episode
        # 版本
        if not self.resource_type:
            self.resource_type = meta.resource_type
        # 分辨率
        if not self.resource_pix:
            self.resource_pix = meta.resource_pix
        # 制作组/字幕组
        if not self.resource_team:
            self.resource_team = meta.resource_team
        # 自定义占位符
        if not self.customization:
            self.customization = meta.customization
        # 特效
        if not self.resource_effect:
            self.resource_effect = meta.resource_effect
        # 视频编码
        if not self.video_encode:
            self.video_encode = meta.video_encode
        # 视频位深
        if not self.video_bit:
            self.video_bit = meta.video_bit
        # 音频编码
        if not self.audio_encode:
            self.audio_encode = meta.audio_encode
        # 帧率信息
        if not self.fps:
            self.fps = meta.fps
        # Part
        if not self.part:
            self.part = meta.part
        # tmdbid
        if not self.tmdbid and meta.tmdbid:
            self.tmdbid = meta.tmdbid
        # doubanid
        if not self.doubanid and meta.doubanid:
            self.doubanid = meta.doubanid
        # 通用媒体来源与ID
        if not self.media_source and meta.media_source:
            self.media_source = meta.media_source
        if not self.media_id and meta.media_id:
            self.media_id = meta.media_id
        # 剧集组
        if not self.episode_group and meta.episode_group:
            self.episode_group = meta.episode_group

    def to_dict(self):
        """
        转为字典
        """
        dicts = vars(self).copy()
        dicts["type"] = self.type.value if self.type else None
        dicts["season_episode"] = self.season_episode
        dicts["edition"] = self.edition
        dicts["name"] = self.name
        dicts["episode_list"] = self.episode_list
        return dicts
