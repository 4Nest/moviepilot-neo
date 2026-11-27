from collections import OrderedDict
from datetime import datetime
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.helper.discover_helper import is_short_drama_text, parse_discover_title_and_season
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.media import parse_media_key

_CACHE_LOCK = threading.RLock()
_FILTER_UI_CACHE_TTL = 6 * 3600
_PAGE_CTX_CACHE_TTL = 1800
_ITEM_CACHE_MAXSIZE = 3000
_PAGE_CTX_MAXSIZE = 256

# 筛选项 UI 缓存: key -> (expire_ts, filter_ui_list)
_filter_ui_cache: Dict[str, Tuple[float, List[dict]]] = {}
# 媒体条目元数据 LRU 缓存: "prefix:id" -> dict
_discover_item_lru: "OrderedDict[str, dict]" = OrderedDict()
# 腾讯视频分页上下文 LRU 缓存: (channel_id, filter_str, page) -> (expire_ts, page_context)
_tencent_page_ctx_cache: "OrderedDict[Tuple[str, str, int], Tuple[float, dict]]" = OrderedDict()

# 哔哩哔哩 6 大频道映射: key -> (season_type, MediaType, is_animation)
_BILIBILI_CHANNELS: Dict[str, Tuple[int, MediaType, bool]] = {
    "bangumi": (1, MediaType.TV, True),
    "1": (1, MediaType.TV, True),
    "movie": (2, MediaType.MOVIE, False),
    "2": (2, MediaType.MOVIE, False),
    "documentary": (3, MediaType.TV, False),
    "3": (3, MediaType.TV, False),
    "guo": (4, MediaType.TV, True),
    "4": (4, MediaType.TV, True),
    "tv": (5, MediaType.TV, False),
    "5": (5, MediaType.TV, False),
    "variety": (7, MediaType.TV, False),
    "7": (7, MediaType.TV, False),
}

# 腾讯视频 6 大频道映射（严禁接入 110755 短剧频道）
_TENCENT_BLOCKED_CHANNELS = frozenset({"110755", "short_drama", "minidrama"})
_TENCENT_CHANNELS: Dict[str, Tuple[str, MediaType, bool]] = {
    "tv": ("100113", MediaType.TV, False),
    "100113": ("100113", MediaType.TV, False),
    "movie": ("100173", MediaType.MOVIE, False),
    "100173": ("100173", MediaType.MOVIE, False),
    "anime": ("100119", MediaType.TV, True),
    "100119": ("100119", MediaType.TV, True),
    "variety": ("100109", MediaType.TV, False),
    "100109": ("100109", MediaType.TV, False),
    "documentary": ("100105", MediaType.TV, False),
    "100105": ("100105", MediaType.TV, False),
    "children": ("100150", MediaType.TV, True),
    "100150": ("100150", MediaType.TV, True),
}

# 芒果TV 6 大频道映射
_MANGGUO_CHANNELS: Dict[str, Tuple[str, MediaType, bool]] = {
    "variety": ("1", MediaType.TV, False),
    "1": ("1", MediaType.TV, False),
    "tv": ("2", MediaType.TV, False),
    "2": ("2", MediaType.TV, False),
    "movie": ("3", MediaType.MOVIE, False),
    "3": ("3", MediaType.MOVIE, False),
    "anime": ("50", MediaType.TV, True),
    "50": ("50", MediaType.TV, True),
    "documentary": ("51", MediaType.TV, False),
    "51": ("51", MediaType.TV, False),
    "children": ("10", MediaType.TV, True),
    "10": ("10", MediaType.TV, True),
}


def clear_discover_source_caches() -> None:
    """清空探索源内存筛选项缓存、分页上下文缓存与条目 LRU 缓存。"""
    with _CACHE_LOCK:
        _filter_ui_cache.clear()
        _discover_item_lru.clear()
        _tencent_page_ctx_cache.clear()


def cache_discover_item(
    media_key: str,
    *,
    title: Optional[str],
    year: Optional[str] = None,
    mtype: Optional[MediaType] = None,
    season: Optional[int] = None,
    poster: Optional[str] = None,
    overview: Optional[str] = None,
) -> None:
    """
    将探索源返回的媒体条目信息写入内存 LRU 缓存，供后续详情/选季/订阅识别无感补全。

    :param media_key: 带来源前缀的媒体键（如 bilibili:28229420）
    :param title: 原始标题
    :param year: 四位年份字符串或 None
    :param mtype: 媒体类型枚举
    :param season: 解析出的季号
    :param poster: 海报地址
    :param overview: 简介文本
    """
    if not media_key or not title:
        return
    source, media_id = parse_media_key(media_key)
    normalized_key = f"{source}:{media_id}" if source and media_id else media_key
    entry = {
        "title": title,
        "year": _clean_year(year),
        "mtype": mtype,
        "season": season,
        "poster": poster,
        "overview": overview,
    }
    with _CACHE_LOCK:
        _discover_item_lru[normalized_key] = entry
        _discover_item_lru.move_to_end(normalized_key)
        while len(_discover_item_lru) > _ITEM_CACHE_MAXSIZE:
            _discover_item_lru.popitem(last=False)


def get_cached_discover_item(media_key: str) -> Optional[dict]:
    """
    从内存 LRU 缓存读取探索源媒体条目元数据。

    :param media_key: 带来源前缀的媒体键
    :return: 命中时返回条目字典副本，未命中返回 None
    """
    if not media_key:
        return None
    source, media_id = parse_media_key(media_key)
    normalized_key = f"{source}:{media_id}" if source and media_id else media_key
    with _CACHE_LOCK:
        item = _discover_item_lru.get(normalized_key)
        if item is not None:
            _discover_item_lru.move_to_end(normalized_key)
            return dict(item)
    return None


def _clean_year(raw_year: Any) -> Optional[str]:
    """提取合法的四位年份字符串，空值或非法值统一返回 None，防止产生 (None)。"""
    if raw_year is None:
        return None
    text = str(raw_year).strip()
    if not text or text.casefold() in {"none", "null", "all", "-1", "0", "未知"}:
        return None
    match = re.search(r"((?:19|20)\d{2})", text)
    return match.group(1) if match else None


def _normalize_image_url(url: Optional[str]) -> str:
    """规范化流媒体封面 URL，补齐协议并将明文 http 升级为 https。"""
    if not url:
        return ""
    cleaned = str(url).strip()
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned.startswith("http://"):
        return f"https://{cleaned[7:]}"
    return cleaned


def _parse_score(raw_score: Any) -> float:
    """解析平台评分字段，非法或空值返回 0.0。"""
    if raw_score is None:
        return 0.0
    text = str(raw_score).strip()
    if not text:
        return 0.0
    try:
        val = round(float(text), 1)
        return val if 0.0 <= val <= 10.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _recent_year_options(count: int = 12) -> List[Tuple[str, str]]:
    """动态生成截至当前年份的倒序年份筛选项。"""
    current_year = datetime.now().year
    return [(str(current_year - idx), str(current_year - idx)) for idx in range(count)]


def _build_chip_row(
    label: str,
    model_key: str,
    options: List[Tuple[str, Any]],
    *,
    mandatory: bool = False,
    show_expr: Optional[str] = None,
) -> dict:
    """构造兼容前端 FormRender 与 ExtraSourceView 样式的单行芯片筛选配置。"""
    row_props: Dict[str, Any] = {
        "class": "d-flex justify-start align-center ga-2",
    }
    if show_expr:
        row_props["show"] = f"{{{{ {show_expr} }}}}"
    chip_group_props: Dict[str, Any] = {"model": model_key}
    if mandatory:
        chip_group_props["mandatory"] = True
    return {
        "component": "div",
        "props": row_props,
        "content": [
            {"component": "VLabel", "text": label},
            {
                "component": "VChipGroup",
                "props": chip_group_props,
                "content": [
                    {
                        "component": "VChip",
                        "props": {
                            "filter": True,
                            "variant": "text",
                            "value": value,
                        },
                        "text": text,
                    }
                    for text, value in options
                ],
            },
        ],
    }


def _build_bilibili_filter_ui() -> List[dict]:
    """构造哔哩哔哩探索源筛选 UI（动态生成年份至当前年）。"""
    years = _recent_year_options(12)
    return [
        _build_chip_row(
            "频道",
            "mtype",
            [
                ("番剧", "bangumi"),
                ("国创", "guo"),
                ("电视剧", "tv"),
                ("电影", "movie"),
                ("纪录片", "documentary"),
                ("综艺", "variety"),
            ],
            mandatory=True,
        ),
        _build_chip_row(
            "排序",
            "order",
            [
                ("播放数量", "2"),
                ("追番人数", "3"),
                ("最高评分", "4"),
                ("更新时间", "0"),
                ("开播时间", "5"),
            ],
            mandatory=True,
        ),
        _build_chip_row(
            "地区",
            "area",
            [("日本", "2"), ("美国", "3")],
            show_expr="mtype == 'bangumi'",
        ),
        _build_chip_row(
            "地区",
            "area",
            [
                ("中国大陆", "1"),
                ("中国港台", "6,7"),
                ("日本", "2"),
                ("美国", "3"),
                ("英国", "4"),
                ("韩国", "8"),
                ("法国", "9"),
            ],
            show_expr="mtype == 'tv' || mtype == 'movie'",
        ),
        _build_chip_row(
            "风格",
            "style_id",
            [
                ("原创", "10010"),
                ("漫画改", "10011"),
                ("小说改", "10012"),
                ("热血", "10016"),
                ("奇幻", "10018"),
                ("战斗", "10020"),
                ("搞笑", "10021"),
                ("日常", "10022"),
                ("科幻", "10023"),
                ("治愈", "10027"),
                ("悬疑", "10030"),
            ],
            show_expr="mtype == 'bangumi' || mtype == 'guo'",
        ),
        _build_chip_row("年份", "year", years),
        _build_chip_row(
            "状态",
            "is_finish",
            [("完结", "1"), ("连载", "0")],
            show_expr="mtype == 'bangumi' || mtype == 'guo' || mtype == 'tv'",
        ),
        _build_chip_row(
            "配音",
            "spoken_language_type",
            [("原声", "1"), ("中文配音", "2")],
            show_expr="mtype == 'bangumi'",
        ),
        _build_chip_row(
            "版权",
            "copyright",
            [("独家", "3"), ("其他", "1,2,4")],
            show_expr="mtype == 'bangumi' || mtype == 'guo'",
        ),
        _build_chip_row(
            "付费",
            "season_status",
            [("免费", "1"), ("大会员", "4,6"), ("付费", "2,6")],
        ),
    ]


def _build_tencent_static_filter_ui() -> List[dict]:
    """构造腾讯视频探索源各频道官方精确筛选项（不含短剧频道）。"""
    years = _recent_year_options(10)
    return [
        _build_chip_row(
            "频道",
            "mtype",
            [
                ("电视剧", "tv"),
                ("电影", "movie"),
                ("动漫", "anime"),
                ("综艺", "variety"),
                ("纪录片", "documentary"),
                ("少儿", "children"),
            ],
            mandatory=True,
        ),
        _build_chip_row(
            "排序",
            "sort",
            [("最热", "75"), ("最新", "79"), ("好评", "85")],
            mandatory=True,
            show_expr="mtype == 'tv' || mtype == 'variety' || mtype == 'documentary' || mtype == 'children'",
        ),
        _build_chip_row(
            "排序",
            "sort",
            [("最热", "75"), ("最新", "83"), ("好评", "81")],
            mandatory=True,
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "排序",
            "sort",
            [("最热", "75"), ("最近更新", "23"), ("好评", "85")],
            mandatory=True,
            show_expr="mtype == 'anime'",
        ),
        _build_chip_row(
            "类型",
            "itype",
            [
                ("爱情", "1"),
                ("都市", "2"),
                ("青春", "3"),
                ("奇幻", "4"),
                ("武侠", "5"),
                ("古装", "6"),
                ("科幻", "7"),
                ("猎奇", "8"),
                ("竞技", "9"),
                ("传奇", "10"),
                ("军旅", "11"),
                ("家庭", "12"),
                ("喜剧", "13"),
            ],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "类型",
            "itype",
            [
                ("动作", "4"),
                ("喜剧", "3"),
                ("爱情", "5"),
                ("科幻", "12"),
                ("犯罪", "6"),
                ("冒险", "7"),
                ("恐怖", "11"),
                ("动画", "15"),
                ("战争", "8"),
                ("悬疑", "10"),
                ("灾难", "25"),
                ("青春", "26"),
            ],
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "类型",
            "itype",
            [
                ("搞笑", "1"),
                ("冒险", "2"),
                ("科幻", "4"),
                ("恋爱", "7"),
                ("玄幻", "9"),
                ("武侠", "13"),
                ("日常", "15"),
                ("悬疑", "17"),
                ("奇幻", "21"),
                ("仙侠", "23"),
                ("都市", "24"),
                ("治愈", "25"),
            ],
            show_expr="mtype == 'anime'",
        ),
        _build_chip_row(
            "地区",
            "iarea",
            [
                ("内地", "0"),
                ("中国香港", "14"),
                ("中国台湾", "4"),
                ("美国", "8"),
                ("英国", "1"),
                ("韩国", "5"),
                ("日本", "10"),
                ("泰国", "9"),
            ],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "地区",
            "iarea",
            [
                ("内地", "100024"),
                ("中国香港", "100025"),
                ("中国台湾", "100026"),
                ("美国", "100029"),
                ("日本", "100027"),
                ("韩国", "100028"),
                ("泰国", "100031"),
                ("英国", "15"),
                ("法国", "16"),
            ],
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "地区",
            "iarea",
            [
                ("内地", "1"),
                ("日本", "2"),
                ("欧美", "3"),
                ("其他", "4"),
            ],
            show_expr="mtype == 'anime'",
        ),
        _build_chip_row(
            "年份",
            "iyear",
            [
                ("2026", "2026"),
                ("2025", "2025"),
                ("2024", "2"),
                ("2023", "3"),
                ("2022", "4"),
                ("2021", "5"),
                ("2020-2016", "6"),
                ("2015-2011", "7"),
                ("2010-2000", "8"),
            ],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "年份",
            "iyear",
            years,
            show_expr="mtype != 'tv'",
        ),
        _build_chip_row(
            "资费",
            "ipay",
            [("免费", "1"), ("限免", "2"), ("VIP", "3")],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "资费",
            "ipay",
            [("免费", "1"), ("VIP", "8"), ("付费", "4")],
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "资费",
            "ipay",
            [("免费", "867"), ("VIP", "6")],
            show_expr="mtype == 'anime'",
        ),
    ]


def _build_mangguo_static_filter_ui() -> List[dict]:
    """构造芒果TV探索源各频道官方精确筛选项。"""
    years = _recent_year_options(10)
    return [
        _build_chip_row(
            "频道",
            "mtype",
            [
                ("电视剧", "tv"),
                ("综艺", "variety"),
                ("电影", "movie"),
                ("动漫", "anime"),
                ("纪录片", "documentary"),
                ("少儿", "children"),
            ],
            mandatory=True,
        ),
        _build_chip_row(
            "排序",
            "sort",
            [("最热", "c2"), ("最新", "c1")],
            mandatory=True,
        ),
        _build_chip_row(
            "类型",
            "kind",
            [
                ("爱情", "3094"),
                ("都市", "3093"),
                ("青春", "3003"),
                ("奇幻", "3092"),
                ("古装", "148"),
                ("军旅", "3091"),
                ("武侠", "3090"),
                ("科幻", "3088"),
                ("竞技", "3087"),
            ],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "类型",
            "kind",
            [
                ("音乐", "8"),
                ("推理", "3075"),
                ("生活", "9"),
                ("情感", "7"),
                ("旅游", "170"),
                ("文化", "3143"),
                ("美食", "3011"),
                ("语言", "3152"),
            ],
            show_expr="mtype == 'variety'",
        ),
        _build_chip_row(
            "类型",
            "kind",
            [
                ("爱情", "175"),
                ("喜剧", "176"),
                ("动作", "177"),
                ("科幻", "178"),
                ("剧情", "3028"),
                ("奇幻", "3027"),
                ("青春", "39"),
                ("国漫", "3096"),
                ("励志", "34"),
            ],
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "类型",
            "kind",
            [
                ("恋爱", "84"),
                ("冒险", "87"),
                ("魔法", "64"),
                ("青春", "63"),
                ("搞笑", "62"),
                ("玄幻", "3150"),
                ("武侠", "195"),
                ("科幻", "90"),
                ("经典", "71"),
            ],
            show_expr="mtype == 'anime'",
        ),
        _build_chip_row(
            "地区",
            "area",
            [
                ("内地", "10"),
                ("港台", "12"),
                ("泰国", "193"),
            ],
            show_expr="mtype == 'tv'",
        ),
        _build_chip_row(
            "地区",
            "area",
            [
                ("内地", "27"),
                ("中国香港", "3036"),
                ("欧美", "3073"),
                ("其他", "other"),
            ],
            show_expr="mtype == 'movie'",
        ),
        _build_chip_row(
            "地区",
            "area",
            [
                ("内地", "52"),
                ("欧美", "53"),
                ("其他", "55"),
            ],
            show_expr="mtype == 'anime'",
        ),
        _build_chip_row("年份", "year", years, show_expr="mtype == 'tv' || mtype == 'movie'"),
        _build_chip_row(
            "付费",
            "chargeInfo",
            [("免费", "b1"), ("VIP", "b2")],
            show_expr="mtype == 'tv' || mtype == 'variety'",
        ),
        _build_chip_row(
            "付费",
            "chargeInfo",
            [("免费", "b1"), ("VIP", "b2"), ("VIP用券", "b3"), ("付费点播", "b4")],
            show_expr="mtype == 'movie'",
        ),
    ]


def _get_cached_filter_ui(cache_key: str, fallback_builder) -> List[dict]:
    """获取带 6 小时 TTL 的筛选项配置，未命中或过期时使用静态构建器兜底。"""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_entry = _filter_ui_cache.get(cache_key)
        if cached_entry and cached_entry[0] > now:
            return list(cached_entry[1])
    ui = fallback_builder()
    with _CACHE_LOCK:
        _filter_ui_cache[cache_key] = (now + _FILTER_UI_CACHE_TTL, ui)
    return list(ui)


def get_builtin_discover_sources() -> List[schemas.DiscoverMediaSource]:
    """
    获取内置的三大流媒体探索数据源配置（哔哩哔哩、腾讯视频、芒果TV）。
    采用零阻塞懒加载与 6 小时缓存，任何网络环境下均稳定可用。
    """
    bilibili_source = schemas.DiscoverMediaSource(
        name="哔哩哔哩",
        mediaid_prefix="bilibili",
        api_path="discover/bilibili",
        filter_params={
            "mtype": "bangumi",
            "order": "2",
            "area": None,
            "style_id": None,
            "season_version": None,
            "spoken_language_type": None,
            "season_status": None,
            "is_finish": None,
            "copyright": None,
            "year": None,
            "exclude_short_drama": True,
        },
        filter_ui=_get_cached_filter_ui("bilibili", _build_bilibili_filter_ui),
        depends={
            "area": ["mtype"],
            "style_id": ["mtype"],
            "season_version": ["mtype"],
            "spoken_language_type": ["mtype"],
            "is_finish": ["mtype"],
            "copyright": ["mtype"],
        },
    )

    tencent_source = schemas.DiscoverMediaSource(
        name="腾讯视频",
        mediaid_prefix="tencentvideo",
        api_path="discover/tencentvideo",
        filter_params={
            "mtype": "tv",
            "sort": "75",
            "itype": None,
            "iarea": None,
            "iyear": None,
            "ipay": None,
            "exclude_short_drama": True,
        },
        filter_ui=_get_cached_filter_ui("tencentvideo", _build_tencent_static_filter_ui),
        depends={
            "sort": ["mtype"],
            "itype": ["mtype"],
            "iarea": ["mtype"],
            "iyear": ["mtype"],
            "ipay": ["mtype"],
        },
    )

    mangguo_source = schemas.DiscoverMediaSource(
        name="芒果TV",
        mediaid_prefix="mangguo",
        api_path="discover/mangguo",
        filter_params={
            "mtype": "tv",
            "sort": "c2",
            "kind": None,
            "area": None,
            "year": None,
            "chargeInfo": None,
            "exclude_short_drama": True,
        },
        filter_ui=_get_cached_filter_ui("mangguo", _build_mangguo_static_filter_ui),
        depends={
            "kind": ["mtype"],
            "area": ["mtype"],
            "year": ["mtype"],
            "chargeInfo": ["mtype"],
        },
    )

    return [bilibili_source, tencent_source, mangguo_source]


def _clean_token_list(raw: Any) -> List[str]:
    """将列表或可能带方括号/引号包裹的分隔字符串清洗为去重词项列表。"""
    if not raw:
        return []
    if isinstance(raw, list):
        candidates: List[str] = []
        for entry in raw:
            if isinstance(entry, str):
                candidates.extend(re.split(r"[,，、;/|\s]+", entry.strip().strip("[]（）()")))
            elif entry is not None:
                candidates.append(str(entry))
    else:
        text = str(raw).strip().strip("[]（）()")
        if not text:
            return []
        candidates = re.split(r"[,，、;/|\s]+", text)

    cleaned: List[str] = []
    for item in candidates:
        token = item.strip().strip("[]（）()\"'“”‘’")
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned


_BILIBILI_CATEGORY_LABELS: Dict[int, str] = {
    1: "番剧",
    2: "电影",
    3: "纪录片",
    4: "国创",
    5: "电视剧",
    7: "综艺",
}


def _format_bilibili_year_param(year: Optional[str], season_type: int) -> Tuple[Optional[str], Optional[str]]:
    """根据哔哩哔哩频道类型格式化年份筛选参数（番剧/国创用 year，影视用 release_date）。"""
    if not year or str(year).strip() in {"", "-1", "None"}:
        return None, None
    raw = str(year).strip()
    if raw.startswith("["):
        return (raw, None) if season_type in (1, 4) else (None, raw)
    if re.fullmatch(r"\d{4}", raw):
        yr = int(raw)
        if season_type in (1, 4):
            return f"[{yr},{yr + 1})", None
        return None, f"[{yr}-01-01 00:00:00,{yr}-12-31 23:59:59]"
    return raw, None


async def async_discover_bilibili(
    mtype: Optional[str] = "bangumi",
    order: Optional[str] = "2",
    area: Optional[str] = None,
    style_id: Optional[str] = None,
    season_version: Optional[str] = None,
    spoken_language_type: Optional[str] = None,
    season_status: Optional[str] = None,
    is_finish: Optional[str] = None,
    copyright_val: Optional[str] = None,
    year: Optional[str] = None,
    exclude_short_drama: bool = False,
    page: int = 1,
    count: int = 30,
) -> List[MediaInfo]:
    """
    异步请求哔哩哔哩 PGC 索引接口并转换为统一 MediaInfo 列表。

    :param mtype: 频道标识（bangumi/guo/tv/movie/documentary/variety）
    :param order: 排序方式
    :param area: 地区筛选
    :param style_id: 风格筛选
    :param season_version: 版本筛选
    :param spoken_language_type: 配音筛选
    :param season_status: 付费状态
    :param is_finish: 完结状态
    :param copyright_val: 版权筛选（兼容 copyright 与 _copyright）
    :param year: 年份筛选
    :param exclude_short_drama: 是否排除短剧
    :param page: 页码（从 1 开始）
    :param count: 每页条数
    :return: MediaInfo 列表
    """
    channel_key = str(mtype or "bangumi").strip().casefold()
    season_type, media_type, is_anim = _BILIBILI_CHANNELS.get(
        channel_key, (1, MediaType.TV, True)
    )

    params: Dict[str, Any] = {
        "st": season_type,
        "season_type": season_type,
        "type": 1,
        "order": order if order not in (None, "") else "2",
        "area": area or "-1",
        "style_id": style_id or "-1",
        "season_version": season_version or "-1",
        "spoken_language_type": spoken_language_type or "-1",
        "season_status": season_status or "-1",
        "is_finish": is_finish or "-1",
        "copyright": copyright_val or "-1",
        "sort": 0,
        "page": max(int(page or 1), 1),
        "pagesize": max(min(int(count or 30), 50), 1),
    }
    bili_year, bili_release_date = _format_bilibili_year_param(year, season_type)
    params["year"] = bili_year or "-1"
    if bili_release_date:
        params["release_date"] = bili_release_date

    req = AsyncRequestUtils(
        ua=settings.NORMAL_USER_AGENT,
        referer="https://www.bilibili.com/",
        timeout=15,
    )
    payload = await req.get_json(
        "https://api.bilibili.com/pgc/season/index/result", params=params
    )
    if not isinstance(payload, dict):
        return []
    container = payload.get("data") or payload.get("result") or {}
    items = container.get("list") if isinstance(container, dict) else None
    if not items:
        return []

    results: List[MediaInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("media_id") or item.get("season_id")
        raw_title = str(item.get("title") or "").strip()
        if not item_id or not raw_title:
            continue

        sub_title = str(item.get("subTitle") or item.get("subtitle") or "").strip()
        index_show = str(item.get("index_show") or "").strip()
        overview_parts = [part for part in (sub_title, index_show) if part]
        overview = " · ".join(overview_parts)

        if exclude_short_drama:
            if is_short_drama_text(title=raw_title, subtitle=overview, is_animation=is_anim):
                continue

        item_year = (
            _clean_year(item.get("release_date"))
            or _clean_year(item.get("pub_date"))
            or _clean_year(item.get("first_ep_pub_time"))
        )
        _, parsed_season, _ = parse_discover_title_and_season(raw_title)
        poster = _normalize_image_url(item.get("cover"))

        media = MediaInfo()
        media.source = "bilibili"
        media.media_id = str(item_id)
        media.type = media_type
        media.category = _BILIBILI_CATEGORY_LABELS.get(season_type, "")
        media.title = raw_title
        media.year = item_year
        if media_type == MediaType.TV and parsed_season is not None:
            media.season = parsed_season
        media.poster_path = poster
        media.overview = overview
        media.vote_average = _parse_score(item.get("score"))
        media.detail_link = str(item.get("link") or f"https://www.bilibili.com/bangumi/media/md{item_id}")

        cache_discover_item(
            f"bilibili:{item_id}",
            title=raw_title,
            year=item_year,
            mtype=media_type,
            season=media.season,
            poster=poster,
            overview=overview,
        )
        results.append(media)

    return results


def _get_tencent_page_context(channel_id: str, filter_str: str, page: int) -> dict:
    """读取缓存的腾讯视频分页上下文；未命中时退化构造 page_index 偏移。"""
    if page <= 1:
        return {}
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _tencent_page_ctx_cache.get((channel_id, filter_str, page))
        if cached and cached[0] > now:
            _tencent_page_ctx_cache.move_to_end((channel_id, filter_str, page))
            return dict(cached[1])
    return {"page_index": str(page - 1)}


def _save_tencent_next_page_context(
    channel_id: str, filter_str: str, next_page: int, next_ctx: Any
) -> None:
    """缓存腾讯视频响应返回的下一页上下文。"""
    if not isinstance(next_ctx, dict) or not next_ctx:
        return
    now = time.monotonic()
    with _CACHE_LOCK:
        _tencent_page_ctx_cache[(channel_id, filter_str, next_page)] = (
            now + _PAGE_CTX_CACHE_TTL,
            dict(next_ctx),
        )
        _tencent_page_ctx_cache.move_to_end((channel_id, filter_str, next_page))
        while len(_tencent_page_ctx_cache) > _PAGE_CTX_MAXSIZE:
            _tencent_page_ctx_cache.popitem(last=False)


async def async_discover_tencentvideo(
    mtype: Optional[str] = "tv",
    sort: Optional[str] = "75",
    itype: Optional[str] = None,
    iarea: Optional[str] = None,
    iyear: Optional[str] = None,
    ipay: Optional[str] = None,
    characteristic: Optional[str] = None,
    exclude_short_drama: bool = False,
    page: int = 1,
    count: int = 30,
) -> List[MediaInfo]:
    """
    异步请求腾讯视频频道页数据并转换为统一 MediaInfo 列表（严禁接入 110755 短剧频道）。

    :param mtype: 频道标识（tv/movie/anime/variety/documentary/children）
    :param sort: 排序参数
    :param itype: 类型筛选
    :param iarea: 地区筛选
    :param iyear: 年份筛选
    :param ipay: 资费筛选
    :param characteristic: 特色筛选
    :param exclude_short_drama: 是否排除短剧
    :param page: 页码（从 1 开始）
    :param count: 单页最大返回数量
    :return: MediaInfo 列表
    """
    channel_key = str(mtype or "tv").strip().casefold()
    if channel_key in _TENCENT_BLOCKED_CHANNELS or channel_key not in _TENCENT_CHANNELS:
        return []

    channel_id, media_type, is_anim = _TENCENT_CHANNELS[channel_key]
    filter_pairs = [
        f"sort={sort or '75'}",
        f"itype={itype or '-1'}",
        f"iarea={iarea or '-1'}",
        f"iyear={iyear or '-1'}",
        f"ipay={ipay or '-1'}",
    ]
    if characteristic:
        filter_pairs.append(f"characteristic={characteristic}")
    filter_str = "&".join(filter_pairs)

    page_num = max(int(page or 1), 1)
    page_context = _get_tencent_page_context(channel_id, filter_str, page_num)
    body = {
        "page_params": {
            "channel_id": channel_id,
            "filter_params": filter_str,
            "page_type": "channel_operation",
            "page_id": "channel_list_second_page",
        },
        "page_context": page_context,
    }

    req = AsyncRequestUtils(
        headers={
            "User-Agent": settings.NORMAL_USER_AGENT,
            "Referer": "https://v.qq.com/",
            "Origin": "https://v.qq.com",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    payload = await req.post_json(
        "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData"
        "?video_appid=1000005&vplatform=2",
        json=body,
    )
    if not isinstance(payload, dict):
        return []

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return []

    if data.get("next_page_context"):
        _save_tencent_next_page_context(
            channel_id, filter_str, page_num + 1, data.get("next_page_context")
        )

    results: List[MediaInfo] = []
    limit = max(int(count or 30), 1)
    for mod_list in data.get("module_list_datas") or []:
        if not isinstance(mod_list, dict):
            continue
        for mod in mod_list.get("module_datas") or []:
            if not isinstance(mod, dict):
                continue
            item_datas = (mod.get("item_data_lists") or {}).get("item_datas") or []
            for entry in item_datas:
                if not isinstance(entry, dict):
                    continue
                item_params = entry.get("item_params") or {}
                if not isinstance(item_params, dict):
                    continue
                cid = str(
                    item_params.get("cid") or entry.get("item_id") or ""
                ).strip()
                # 排除筛选器项或无有效 cid 的运营卡片
                if not cid or entry.get("item_type") not in (None, "1", 1, "2", 2):
                    if not item_params.get("title"):
                        continue
                raw_title = str(
                    item_params.get("title") or item_params.get("mz_title") or ""
                ).strip()
                if not cid or not raw_title:
                    continue

                second_title = str(item_params.get("second_title") or "").strip()
                timelong = str(item_params.get("timelong") or "").strip()
                overview = " · ".join(p for p in (second_title, timelong) if p)

                genre_tags = _clean_token_list(
                    [
                        item_params.get("area_name") or item_params.get("areaName") or "",
                        item_params.get("main_genre") or "",
                        item_params.get("sub_genre") or "",
                    ]
                )
                if exclude_short_drama:
                    if is_short_drama_text(
                        title=raw_title,
                        subtitle=overview,
                        tags=genre_tags,
                        is_animation=is_anim,
                    ):
                        continue

                item_year = _clean_year(item_params.get("year")) or _clean_year(
                    item_params.get("publish_date")
                )
                _, parsed_season, _ = parse_discover_title_and_season(raw_title)
                poster = _normalize_image_url(
                    item_params.get("new_pic_vt")
                    or item_params.get("pic_vertial")
                    or item_params.get("new_pic_hz")
                )

                media = MediaInfo()
                media.source = "tencentvideo"
                media.media_id = cid
                media.type = media_type
                media.title = raw_title
                media.year = item_year
                if media_type == MediaType.TV and parsed_season is not None:
                    media.season = parsed_season
                media.poster_path = poster
                media.overview = overview
                media.vote_average = _parse_score(
                    item_params.get("opinion_score") or item_params.get("uni_score")
                )
                if genre_tags:
                    media.genres = [{"id": g, "name": g} for g in genre_tags]
                actor_names = _clean_token_list(
                    item_params.get("leading_actor") or item_params.get("sub_title")
                )
                if actor_names:
                    media.actors = [{"name": actor} for actor in actor_names]

                cache_discover_item(
                    f"tencentvideo:{cid}",
                    title=raw_title,
                    year=item_year,
                    mtype=media_type,
                    season=media.season,
                    poster=poster,
                    overview=overview,
                )
                results.append(media)
                if len(results) >= limit:
                    return results

    return results


async def async_discover_mangguo(
    mtype: Optional[str] = "tv",
    sort: Optional[str] = "c2",
    kind: Optional[str] = None,
    area: Optional[str] = None,
    year: Optional[str] = None,
    charge_info: Optional[str] = None,
    exclude_short_drama: bool = False,
    page: int = 1,
    count: int = 30,
) -> List[MediaInfo]:
    """
    异步请求芒果TV片库接口并转换为统一 MediaInfo 列表。

    :param mtype: 频道标识（variety/tv/movie/anime/documentary/children）
    :param sort: 排序方式（c1 最新, c2 最热, c4 好评）
    :param kind: 类型筛选
    :param area: 地区筛选
    :param year: 年份筛选
    :param charge_info: 付费类型
    :param exclude_short_drama: 是否排除短剧
    :param page: 页码（从 1 开始）
    :param count: 每页条数
    :return: MediaInfo 列表
    """
    channel_key = str(mtype or "tv").strip().casefold()
    channel_id, media_type, is_anim = _MANGGUO_CHANNELS.get(
        channel_key, ("2", MediaType.TV, False)
    )

    params: Dict[str, Any] = {
        "allowedRC": "1",
        "platform": "pcweb",
        "channelId": channel_id,
        "pn": max(int(page or 1), 1),
        "pc": max(min(int(count or 30), 80), 1),
        "hudong": "1",
        "_support": "10000000",
        "kind": kind or "a1",
        "area": area or "a1",
        "year": year or "all",
        "chargeInfo": charge_info or "a1",
        "sort": sort or "c2",
    }

    req = AsyncRequestUtils(
        ua=settings.NORMAL_USER_AGENT,
        referer="https://www.mgtv.com/",
        timeout=15,
    )
    payload = await req.get_json(
        "https://pianku.api.mgtv.com/rider/list/pcweb/v3", params=params
    )
    if not isinstance(payload, dict):
        return []

    data = payload.get("data") or {}
    docs = data.get("hitDocs") if isinstance(data, dict) else None
    if not docs:
        return []

    results: List[MediaInfo] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        clip_id = str(doc.get("clipId") or doc.get("playPartId") or "").strip()
        raw_title = str(doc.get("title") or "").strip()
        if not clip_id or not raw_title:
            continue

        story = str(doc.get("story") or "").strip()
        subtitle = str(doc.get("subtitle") or "").strip()
        update_info = str(doc.get("updateInfo") or "").strip()
        overview = story or " · ".join(p for p in (subtitle, update_info) if p)

        kind_tags = _clean_token_list(doc.get("kind"))

        if exclude_short_drama:
            if is_short_drama_text(
                title=raw_title,
                subtitle=f"{story} {subtitle}",
                tags=kind_tags,
                is_animation=is_anim,
            ):
                continue
            # 芒果TV 电视剧频道（channelId=2）买量微短剧通常缺失主演 subtitle 与 leader 字段，正剧与芒果自制保留
            if (
                channel_id == "2"
                and "subtitle" in doc
                and not subtitle
                and not doc.get("leader")
                and "芒果出品" not in kind_tags
            ):
                continue

        item_year = _clean_year(doc.get("year")) or _clean_year(doc.get("releaseTime"))
        _, parsed_season, _ = parse_discover_title_and_season(raw_title)
        poster = _normalize_image_url(doc.get("img"))

        media = MediaInfo()
        media.source = "mangguo"
        media.media_id = clip_id
        media.type = media_type
        media.title = raw_title
        media.year = item_year
        if media_type == MediaType.TV and parsed_season is not None:
            media.season = parsed_season
        media.poster_path = poster
        media.overview = overview
        media.vote_average = _parse_score(doc.get("zhihuScore") or doc.get("score"))
        if kind_tags:
            media.genres = [{"id": k, "name": k} for k in kind_tags]
        actor_names = _clean_token_list(doc.get("leader"))
        if not actor_names and subtitle and any(sep in subtitle for sep in (",", "，", "、")):
            actor_names = _clean_token_list(subtitle)
        if actor_names:
            media.actors = [{"name": a} for a in actor_names]

        cache_discover_item(
            f"mangguo:{clip_id}",
            title=raw_title,
            year=item_year,
            mtype=media_type,
            season=media.season,
            poster=poster,
            overview=overview,
        )
        results.append(media)

    return results


def _parse_bilibili_review_detail(payload: Optional[dict], media_id: str) -> Optional[dict]:
    """解析哔哩哔哩单条媒体详情回查数据并写入 LRU 缓存。"""
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") or payload.get("data") or {}
    media = result.get("media") if isinstance(result, dict) else None
    if not isinstance(media, dict):
        return None
    title = str(media.get("title") or "").strip()
    if not title:
        return None
    publish = media.get("publish") or {}
    year = _clean_year(
        publish.get("pub_date") or publish.get("release_date_show") or media.get("year")
    )
    type_name = str(media.get("type_name") or "")
    mtype = (
        MediaType.MOVIE
        if media.get("type") == 2 or "电影" in type_name
        else MediaType.TV
    )
    _, parsed_season, _ = parse_discover_title_and_season(title)
    season = parsed_season if mtype == MediaType.TV else None
    poster = _normalize_image_url(media.get("cover"))
    entry = {
        "title": title,
        "year": year,
        "mtype": mtype,
        "season": season,
        "poster": poster,
    }
    cache_discover_item(f"bilibili:{media_id}", **entry)
    return entry


def _parse_tencent_float_vinfo(raw_text: Optional[str], cid: str) -> Optional[dict]:
    """解析腾讯视频 float_vinfo2 单条详情（兼容纯 JSON 与 QZOutputJson 包裹）。"""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    if cleaned.startswith("QZOutputJson="):
        cleaned = cleaned[len("QZOutputJson="):].rstrip(";").strip()
    try:
        payload = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    cover_info = payload.get("c") if isinstance(payload.get("c"), dict) else payload
    title = str(cover_info.get("title") or "").strip()
    if not title:
        return None
    year = _clean_year(cover_info.get("year"))
    mtype = MediaType.MOVIE if cover_info.get("type") == 1 else MediaType.TV
    _, parsed_season, _ = parse_discover_title_and_season(title)
    season = parsed_season if mtype == MediaType.TV else None
    poster = _normalize_image_url(cover_info.get("pic"))
    entry = {
        "title": title,
        "year": year,
        "mtype": mtype,
        "season": season,
        "poster": poster,
    }
    cache_discover_item(f"tencentvideo:{cid}", **entry)
    return entry


def fetch_discover_item_detail(source: str, media_id: str) -> Optional[dict]:
    """
    同步回查内置流媒体探索源单条详情（用于仅传入 mediaid 未带 title 时的元数据补全）。

    :param source: 数据源标识（bilibili/tencentvideo）
    :param media_id: 平台原生条目 ID
    :return: 包含 title/year/mtype/season 的字典或 None
    """
    try:
        req = RequestUtils(ua=settings.NORMAL_USER_AGENT, timeout=10)
        if source == "bilibili":
            resp = req.get_res(
                "https://api.bilibili.com/pgc/review/user",
                params={"media_id": media_id},
            )
            if resp and resp.status_code == 200:
                return _parse_bilibili_review_detail(resp.json(), media_id)
        elif source == "tencentvideo":
            text = req.get(
                "https://node.video.qq.com/x/api/float_vinfo2",
                params={"cid": media_id},
            )
            return _parse_tencent_float_vinfo(text, media_id)
    except Exception as err:
        logger.debug(f"同步回查探索源详情失败 {source}:{media_id} - {err}")
    return None


async def async_fetch_discover_item_detail(
    source: str, media_id: str
) -> Optional[dict]:
    """
    异步回查内置流媒体探索源单条详情（用于仅传入 mediaid 未带 title 时的元数据补全）。

    :param source: 数据源标识（bilibili/tencentvideo）
    :param media_id: 平台原生条目 ID
    :return: 包含 title/year/mtype/season 的字典或 None
    """
    try:
        req = AsyncRequestUtils(ua=settings.NORMAL_USER_AGENT, timeout=10)
        if source == "bilibili":
            payload = await req.get_json(
                "https://api.bilibili.com/pgc/review/user",
                params={"media_id": media_id},
            )
            return _parse_bilibili_review_detail(payload, media_id)
        if source == "tencentvideo":
            text = await req.get(
                "https://node.video.qq.com/x/api/float_vinfo2",
                params={"cid": media_id},
            )
            return _parse_tencent_float_vinfo(text, media_id)
    except Exception as err:
        logger.debug(f"异步回查探索源详情失败 {source}:{media_id} - {err}")
    return None
