import re
from copy import deepcopy
from functools import lru_cache
from typing import Iterator, List, Tuple, Union, Dict, Optional

from pyparsing import ParseBaseException

from app.core.context import TorrentInfo, MediaInfo
from app.core.metainfo import MetaInfo, clear_rust_parse_options_cache, _rust_parse_options
from app.helper.rule import RuleHelper, RuleStringValidationError, describe_rule_string_error
from app.log import logger
from app.modules import _ModuleBase
from app.modules.filter.RuleParser import RuleParser
from app.modules.filter.builtin_rules import BUILTIN_RULE_SET
from app.schemas.types import ModuleType, OtherModulesType, SystemConfigKey
from app.utils import rust_accel
from app.utils.string import StringUtils


_SIZE_UNIT = 1024 * 1024


@lru_cache(maxsize=1024)
def _compile_ignorecase(pattern: str) -> re.Pattern:
    """
    编译过滤规则正则。
    过滤规则在搜索/订阅中会被大量种子重复匹配，缓存编译结果能减少热路径开销；
    这里仍保留原有的 IGNORECASE 语义，非法正则也会像原来一样在匹配时抛出异常。
    """
    return re.compile(r"%s" % pattern, re.IGNORECASE)


def _regex_search(pattern: Union[str, int, float], content: str) -> bool:
    """
    按原有字符串插值语义执行正则匹配，同时复用已编译表达式。
    """
    return bool(_compile_ignorecase(str(pattern)).search(content))


@lru_cache(maxsize=256)
def _parse_size_range(size_range: str) -> Tuple[str, float, Optional[float]]:
    """
    解析大小范围，单位为 MB。
    返回值中的操作符只供本模块内部使用，避免每个种子重复拆分同一个规则。
    """
    size_range = size_range.strip()
    if size_range.find("-") != -1:
        size_min, size_max = size_range.split("-")
        return "between", float(size_min.strip()) * _SIZE_UNIT, float(size_max.strip()) * _SIZE_UNIT
    if size_range.startswith(">"):
        return "gte", float(size_range[1:].strip()) * _SIZE_UNIT, None
    if size_range.startswith("<"):
        return "lte", 0, float(size_range[1:].strip()) * _SIZE_UNIT
    return "unknown", 0, None


@lru_cache(maxsize=256)
def _parse_publish_time(publish_time: str) -> Tuple[float, ...]:
    """
    解析发布时间规则，避免同一规则对大量种子反复转换 float。
    """
    return tuple(float(t) for t in publish_time.split("-"))


class FilterModule(_ModuleBase):
    """
    过滤器模块，负责按内置和自定义规则筛选种子资源。
    """

    CONFIG_WATCH = {
        SystemConfigKey.CustomFilterRules.value,
        SystemConfigKey.CustomIdentifiers.value,
        SystemConfigKey.CustomReleaseGroups.value,
        SystemConfigKey.Customization.value,
        # 远程同步词表内容变更后重载,刷新 Rust 解析配置缓存
        SystemConfigKey.SyncedWords.value,
        SystemConfigKey.WordsSyncSources.value,
    }

    # 保留一份只读内置规则定义，方便查询工具准确区分“内置规则”和“自定义规则”。
    builtin_rule_set: Dict[str, dict] = deepcopy(BUILTIN_RULE_SET)
    # 运行期规则集 = 内置规则 + 自定义规则覆盖。
    rule_set: Dict[str, dict] = {}

    def __init__(self) -> None:
        """
        初始化过滤器模块依赖的规则仓库。
        """
        super().__init__()
        self.rulehelper = RuleHelper()

    def init_module(self) -> None:
        """
        初始化过滤规则集，合并内置规则和用户自定义规则。
        """
        # 每次重载都先恢复为纯内置规则，避免旧的自定义规则残留在内存里。
        self.rule_set = deepcopy(self.builtin_rule_set)
        self.__init_custom_rules()

    def __init_custom_rules(self):
        """
        加载用户自定义规则，如跟内置规则冲突，以用户自定义规则为准
        """
        custom_rules = self.rulehelper.get_custom_rules()
        for rule in custom_rules:
            logger.info(f"加载自定义规则 {rule.id} - {rule.name}")
            self.rule_set[rule.id] = rule.model_dump()

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "过滤器"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Filter

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self) -> None:
        """停止模块"""
        clear_rust_parse_options_cache()

    def test(self) -> None:
        """
        测试过滤器模块状态。
        """
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        返回过滤器模块启用配置。
        """
        pass

    def filter_torrents(self, rule_groups: List[str],
                        torrent_list: List[TorrentInfo],
                        mediainfo: MediaInfo = None,
                        rejection_details: Optional[List[dict]] = None) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  媒体信息
        :param rejection_details:  可选的具体排除原因收集列表
        :return: 过滤后的资源列表，添加资源优先级
        """
        if not rule_groups:
            return torrent_list
        # 查询规则表详情
        groups = self.rulehelper.get_rule_group_by_media(media=mediainfo, group_names=rule_groups)
        if groups:
            group_defs = [group.model_dump() if hasattr(group, "model_dump") else vars(group) for group in groups]
            matched_orders = rust_accel.filter_torrents(
                groups=group_defs,
                torrent_list=torrent_list,
                rule_set=self.rule_set,
                mediainfo=mediainfo,
                metainfo_options=_rust_parse_options() if self.__needs_metainfo_options(group_defs) else None,
            )
            if matched_orders is None:
                return self.__filter_torrents_with_python(
                    groups=group_defs,
                    torrent_list=torrent_list,
                    mediainfo=mediainfo,
                    rejection_details=rejection_details,
                )
            matched_orders, traces = self.__parse_rust_filter_result(matched_orders)
            self.__log_rust_filter_traces(traces)
            ret_torrents = []
            for index, pri_order in matched_orders:
                torrent = torrent_list[index]
                torrent.pri_order = pri_order
                ret_torrents.append(torrent)
            if rejection_details is not None and len(ret_torrents) < len(torrent_list):
                matched_indices = {index for index, _ in matched_orders}
                parser = RuleParser()
                parsed_rule_cache = {}
                for index, torrent in enumerate(torrent_list):
                    if index not in matched_indices:
                        self.__collect_rejection_details(
                            groups=group_defs,
                            torrent=torrent,
                            mediainfo=mediainfo,
                            parser=parser,
                            parsed_rule_cache=parsed_rule_cache,
                            rejection_details=rejection_details,
                        )
            return ret_torrents
        return torrent_list

    @staticmethod
    def __parse_rust_filter_result(result) -> Tuple[list, list]:
        """
        兼容新旧 Rust 过滤返回值，统一拆出匹配结果和调试日志。
        """
        if (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[1], list)
        ):
            return result
        return result, []

    @staticmethod
    def __log_rust_filter_traces(traces: list) -> None:
        """
        输出 Rust 过滤路径返回的规则级调试日志。
        """
        for trace in traces:
            logger.debug(trace)

    def __filter_torrents_with_python(self, groups: List[dict],
                                      torrent_list: List[TorrentInfo],
                                      mediainfo: MediaInfo = None,
                                      rejection_details: Optional[List[dict]] = None) -> List[TorrentInfo]:
        """
        使用 Python 旧路径过滤种子，供 Rust 加速关闭或不可用时兜底。
        """
        ret_torrents = torrent_list
        parser = RuleParser()
        parsed_rule_cache = {}
        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            ret_torrents = self.__filter_torrents(
                rule_string=rule_string,
                rule_name=group.get("name") or rule_string,
                torrent_list=ret_torrents,
                mediainfo=mediainfo,
                parser=parser,
                parsed_rule_cache=parsed_rule_cache,
                rejection_details=rejection_details,
            )
            if not ret_torrents:
                break
        return ret_torrents

    def __needs_metainfo_options(self, groups: List[dict]) -> bool:
        """
        判断当前规则链是否会触发 size_range，避免无大小规则时读取 MetaInfo 运行配置。
        """
        rule_ids = set()
        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            rule_ids.update(re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+[A-Za-z][A-Za-z0-9]*", rule_string))
        return any(self.rule_set.get(rule_id, {}).get("size_range") for rule_id in rule_ids)

    def __filter_torrents(self, rule_string: str, rule_name: str,
                          torrent_list: List[TorrentInfo],
                          mediainfo: MediaInfo,
                          parser: RuleParser,
                          parsed_rule_cache: Dict[str, Union[list, str]],
                          rejection_details: Optional[List[dict]] = None) -> List[TorrentInfo]:
        """
        筛选命中任一优先级层级的种子，并保留输入顺序。

        :raises ValueError: 实际执行的层级无法解析，说明中保留规则组及完整无效 ID
        """
        if not torrent_list:
            return []
        # 只拆分一次规则层级；具体层级仍延迟到真正需要匹配时解析。
        rule_groups = [rule_group.strip() for rule_group in rule_string.split('>')]
        ret_torrents = []
        try:
            for torrent in torrent_list:
                if not self.__get_order(torrent, rule_groups, mediainfo, parser, parsed_rule_cache):
                    logger.debug(f"种子 {torrent.site_name} - {torrent.title} {torrent.description or ''} "
                                 f"不匹配 {rule_name} 过滤规则")
                    if rejection_details is not None:
                        rejection_details.extend(self.__explain_rule_group(
                            rule_groups=rule_groups,
                            rule_name=rule_name,
                            torrent=torrent,
                            mediainfo=mediainfo,
                            parser=parser,
                            parsed_rule_cache=parsed_rule_cache,
                        ))
                    continue
                ret_torrents.append(torrent)
        except (ParseBaseException, RuleStringValidationError) as error:
            # 只在实际解析失败后补充定位，不能预解析未执行的低优先级层级。
            issue = error if isinstance(error, RuleStringValidationError) else describe_rule_string_error(
                error.pstr, self.rule_set,
            )
            detail = str(issue) if issue is not None else "规则表达式语法错误"
            raise ValueError(f"过滤规则组 {rule_name!r} 配置错误：{detail}") from error

        return ret_torrents

    def __collect_rejection_details(
            self,
            groups: List[dict],
            torrent: TorrentInfo,
            mediainfo: MediaInfo,
            parser: RuleParser,
            parsed_rule_cache: Dict[str, Union[list, str]],
            rejection_details: List[dict],
    ) -> None:
        """按规则组顺序找到实际排除资源的规则组并收集明细。"""

        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            rule_name = group.get("name") or rule_string
            rule_groups = [rule_group.strip() for rule_group in rule_string.split('>')]
            if self.__get_order(torrent, rule_groups, mediainfo, parser, parsed_rule_cache):
                continue
            rejection_details.extend(self.__explain_rule_group(
                rule_groups=rule_groups,
                rule_name=rule_name,
                torrent=torrent,
                mediainfo=mediainfo,
                parser=parser,
                parsed_rule_cache=parsed_rule_cache,
            ))
            break

    def __explain_rule_group(
            self,
            rule_groups: List[str],
            rule_name: str,
            torrent: TorrentInfo,
            mediainfo: MediaInfo,
            parser: RuleParser,
            parsed_rule_cache: Dict[str, Union[list, str]],
    ) -> List[dict]:
        """解释资源未命中规则组任何优先级的具体条件。"""

        details = []
        for rule_group in rule_groups:
            parsed_group = self.__parse_rule_group(rule_group, parser, parsed_rule_cache)
            if self.__match_group(torrent, parsed_group, mediainfo):
                continue
            group_details = self.__explain_group(
                torrent=torrent,
                rule_group=parsed_group,
                mediainfo=mediainfo,
                expected=True,
            )
            if not group_details:
                group_details = [{"code": "no_priority_match"}]
            for detail in group_details:
                detail.update({"stage": "filter_rules", "rule_group": rule_name, "torrent": torrent})
            details.extend(group_details)
        return details

    def __explain_group(
            self,
            torrent: TorrentInfo,
            rule_group: Union[list, str],
            mediainfo: MediaInfo,
            expected: bool,
    ) -> List[dict]:
        """按布尔表达式语义收集导致表达式不满足期望的规则明细。"""

        if not isinstance(rule_group, list):
            matched = self.__match_rule(torrent, rule_group, mediainfo)
            if matched == expected:
                return []
            if not expected:
                return [{"code": "negated_rule_matched", "rule": rule_group}]
            return self.__explain_rule_mismatches(torrent, rule_group, mediainfo)
        if len(rule_group) == 1:
            return self.__explain_group(torrent, rule_group[0], mediainfo, expected)
        if rule_group[0] == "not":
            return self.__explain_group(torrent, rule_group[1:], mediainfo, not expected)
        if rule_group[1] in {"and", "or"}:
            return [
                *self.__explain_group(torrent, rule_group[0], mediainfo, expected),
                *self.__explain_group(torrent, rule_group[2:], mediainfo, expected),
            ]
        return []

    def __get_order(self, torrent: TorrentInfo, rule_groups: List[str],
                    mediainfo: MediaInfo, parser: RuleParser,
                    parsed_rule_cache: Dict[str, Union[list, str]]) -> Optional[TorrentInfo]:
        """
        获取种子匹配的规则优先级，值越大越优先，未匹配时返回None
        """
        # 优先级
        res_order = 100
        # 是否匹配
        matched = False

        for rule_group in rule_groups:
            # 解析规则组
            parsed_group = self.__parse_rule_group(rule_group, parser, parsed_rule_cache)
            if self.__match_group(torrent, parsed_group, mediainfo):
                # 出现匹配时中断
                matched = True
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 优先级为 {100 - res_order + 1}")
                torrent.pri_order = res_order
                break
            # 优先级降低，继续匹配
            res_order -= 1

        return None if not matched else torrent

    def __parse_rule_group(self, rule_group: str, parser: RuleParser,
                           parsed_rule_cache: Dict[str, Union[list, str]]) -> Union[list, str]:
        """
        解析单个实际执行的优先级层级，并按层级表达式复用结果。

        旧解析器默认允许尾随未消费文本，因此先诊断完整操作数和语法；悬空但合法
        的 ID 仍沿用原有未命中语义，由搜索诊断单独报告。
        """
        if rule_group not in parsed_rule_cache:
            issue = describe_rule_string_error(rule_group, self.rule_set)
            if issue is not None and issue.code != "rule_id_missing":
                raise issue
            parsed_rule_cache[rule_group] = parser.parse(rule_group).as_list()[0]
        return parsed_rule_cache[rule_group]

    def __match_group(self, torrent: TorrentInfo, rule_group: Union[list, str],
                      mediainfo: MediaInfo) -> Optional[bool]:
        """
        判断种子是否匹配规则组
        """
        if not isinstance(rule_group, list):
            # 不是列表，说明是规则名称
            return self.__match_rule(torrent, rule_group, mediainfo)
        elif isinstance(rule_group, list) and len(rule_group) == 1:
            # 只有一个规则项
            return self.__match_group(torrent, rule_group[0], mediainfo)
        elif rule_group[0] == "not":
            # 非操作
            return not self.__match_group(torrent, rule_group[1:], mediainfo)
        elif rule_group[1] == "and":
            # 与操作
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                and self.__match_group(torrent, rule_group[2:], mediainfo)
        elif rule_group[1] == "or":
            # 或操作
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                or self.__match_group(torrent, rule_group[2:], mediainfo)

    def __match_rule(self, torrent: TorrentInfo, rule_name: str,
                     mediainfo: MediaInfo) -> bool:
        """
        判断种子是否匹配规则项
        """
        mismatch = next(self.__iter_rule_mismatches(torrent, rule_name, mediainfo), None)
        if mismatch:
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 未通过 {rule_name}：{mismatch['code']}")
            return False
        return True

    def __explain_rule_mismatches(
            self,
            torrent: TorrentInfo,
            rule_name: str,
            mediainfo: MediaInfo,
    ) -> List[dict]:
        """列出规则项未满足的全部具体条件。"""

        return [
            {"rule": rule_name, **detail}
            for detail in self.__iter_rule_mismatches(torrent, rule_name, mediainfo)
        ]

    def __iter_rule_mismatches(
            self,
            torrent: TorrentInfo,
            rule_name: str,
            mediainfo: MediaInfo,
    ) -> Iterator[dict]:
        """按需生成规则未匹配项，正常过滤仍在首个失败条件处停止。"""

        rule = self.rule_set.get(rule_name)
        if not rule:
            yield {"code": "rule_missing"}
            return
        tmdb = rule.get("tmdb")
        if tmdb and self.__match_tmdb(tmdb, mediainfo):
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 符合 {rule_name} 的TMDB规则，匹配成功")
            return

        content = f"{torrent.title} {torrent.description} {' '.join(torrent.labels or [])}"
        match_content = []
        for match in rule.get("match") or []:
            if not hasattr(torrent, match):
                continue
            match_value = getattr(torrent, match)
            if not match_value:
                continue
            if isinstance(match_value, list):
                match_content.extend(match_value)
            else:
                match_content.append(match_value)
        if match_content:
            content = " ".join(match_content)

        includes = rule.get("include") or []
        if not isinstance(includes, list):
            includes = [includes]
        excludes = rule.get("exclude") or []
        if not isinstance(excludes, list):
            excludes = [excludes]
        size_range = rule.get("size_range")
        seeders = rule.get("seeders")
        downloadvolumefactor = rule.get("downloadvolumefactor")
        pubdate: str = rule.get("publish_time")

        if includes and not any(_regex_search(include, content) for include in includes):
            yield {"code": "include_missed", "expected": "、".join(str(include) for include in includes)}
        for exclude in excludes:
            if _regex_search(exclude, content):
                yield {"code": "exclude_matched", "expected": str(exclude)}
        if size_range and not self.__match_size(torrent, size_range):
            yield {
                "code": "size_mismatch",
                "expected": f"{size_range} MB",
                "actual": StringUtils.str_filesize(torrent.size),
            }
        if seeders and torrent.seeders < int(seeders):
            yield {
                "code": "seeders_mismatch",
                "expected": str(seeders),
                "actual": str(torrent.seeders),
            }
        if downloadvolumefactor is not None and torrent.downloadvolumefactor != downloadvolumefactor:
            yield {
                "code": "free_mismatch",
                "expected": str(downloadvolumefactor),
                "actual": str(torrent.downloadvolumefactor),
            }
        if pubdate:
            pub_minutes = torrent.pub_minutes()
            pub_times = _parse_publish_time(pubdate)
            if len(pub_times) == 1 and pub_minutes < pub_times[0]:
                yield {
                    "code": "publish_time_mismatch",
                    "expected": f">= {pub_times[0]} 分钟",
                    "actual": f"{pub_minutes} 分钟",
                }
            elif len(pub_times) > 1 and not (pub_times[0] <= pub_minutes <= pub_times[1]):
                yield {
                    "code": "publish_time_mismatch",
                    "expected": f"{pub_times[0]}-{pub_times[1]} 分钟",
                    "actual": f"{pub_minutes} 分钟",
                }

    @staticmethod
    def __match_tmdb(tmdb: dict, mediainfo: MediaInfo) -> bool:
        """
        判断种子是否匹配TMDB规则
        """

        def __get_media_value(key: str):
            try:
                return getattr(mediainfo, key)
            except ValueError:
                return ""

        if not mediainfo:
            return False

        for attr, value in tmdb.items():
            if not value:
                continue
            # 获取media信息的值
            info_value = __get_media_value(attr)
            if not info_value:
                # 没有该值，不匹配
                return False
            elif attr == "production_countries":
                # 国家信息
                info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
            else:
                # media信息转化为数组
                if isinstance(info_value, list):
                    info_values = [str(val).upper() for val in info_value]
                else:
                    info_values = [str(info_value).upper()]
            # 过滤值转化为数组
            if value.find(",") != -1:
                values = [str(val).upper() for val in value.split(",") if val]
            else:
                values = [str(value).upper()]
            # 没有交集为不匹配
            if not set(values).intersection(set(info_values)):
                return False

        return True

    @staticmethod
    def __match_size(torrent: TorrentInfo, size_range: str) -> bool:
        """
        判断种子是否匹配大小范围（MB），剧集拆分为每集大小
        """
        if not size_range:
            return True
        # 集数
        meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
        episode_count = meta.total_episode or 1
        # 每集大小
        torrent_size = torrent.size / episode_count
        # 大小范围
        size_rule, size_min, size_max = _parse_size_range(size_range)
        if size_rule == "between":
            # 区间
            if size_min <= torrent_size <= size_max:
                return True
        elif size_rule == "gte":
            # 大于
            if torrent_size >= size_min:
                return True
        elif size_rule == "lte":
            # 小于
            if torrent_size <= size_max:
                return True
        return False
