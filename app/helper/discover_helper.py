import re
from typing import Any, Iterable, List, Optional, Tuple

import cn2an

from app.chain.media import MediaChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.schemas import MediaRecognizeConvertEventData
from app.schemas.types import ChainEventType, MediaType
from app.utils.media import MEDIA_SOURCE_ID_FIELDS, normalize_media_source, parse_media_key

# 实测确认的 TMDB 短剧专属关键词 ID 列表（用于 discover without_keywords 参数过滤）
TMDB_SHORT_DRAMA_WITHOUT_KEYWORDS = (
    "370469,382841,383994,381124,356983,386442,"
    "382404,307340,384171,376868,380786,359181"
)

_TMDB_ANIMATION_GENRE_ID = 16
_TMDB_SHORT_DRAMA_NETWORK_IDS = frozenset({8020})
_TMDB_SHORT_DRAMA_NETWORK_KEYWORDS = (
    "short drama",
    "短剧",
    "红果",
    "reelshort",
    "dramabox",
    "shortmax",
    "flextv",
)
_TMDB_SHORT_DRAMA_TEXT_MARKERS = (
    "微短剧",
    "竖屏短剧",
    "抖音短剧",
    "快手短剧",
    "红果短剧",
    "小程序短剧",
    "精品短剧",
    "网络微短剧",
    "short drama",
    "micro-drama",
    "micro drama",
    "vertical drama",
)
_GENERIC_SHORT_DRAMA_TEXT_MARKERS = (
    *_TMDB_SHORT_DRAMA_TEXT_MARKERS,
    "短剧",
    "竖屏剧",
    "十分剧场",
)
_AI_COMIC_SHORT_MARKERS = (
    "动态漫",
    "漫剧",
    "沙雕动画",
    "ai漫",
)
_NON_FICTION_EXEMPT_MARKERS = (
    "纪录片",
    "真人秀",
    "脱口秀",
    "歌舞",
    "音乐",
    "新闻",
    "晚会",
)
_SHORT_DRAMA_PLOT_RE = re.compile(
    r"(?:"
    r"霸总|霸道总裁|娇妻|甜妻|萌宝|奶爸|闪婚|替嫁|退婚|休夫|弃妃|毒妃|医妃|狂妃|世子妃|王妃又|"
    r"战神|龙王|神豪|赘婿|绑错系统|系统奖励|狂飙系统|签到系统|开局.{0,6}(?:奖励|无敌|觉醒|签到)|"
    r"炮灰|恶役女配|穿书|穿成.{0,6}(?:庶女|恶毒|炮灰|反派|后妈)|重生之|逆袭打脸|虐渣|团宠|真千金|假千金|"
    r"诱哄|偷吻|契约婚|年代当后妈|杀疯全|修仙归来|老祖归来|全族成帝|千古一帝|千古第[一二三]阳谋|"
    r"文盲修仙|突破炼气期|闯仙界|小孩哥.{0,6}拿捏|749局太子爷"
    r")"
)
_ANIMATION_TAG_MARKERS = (
    "动画",
    "番剧",
    "国创",
    "动漫",
    "少儿",
    "animation",
    "anime",
)
_SHORT_DRAMA_TAG_MARKERS = (
    "短剧",
    "微短剧",
    "竖屏短剧",
    "竖屏",
    "短片",
    "十分剧场",
    "动态漫",
    "漫剧",
    "short drama",
)
_SHORT_FILM_TOKEN_RE = re.compile(r"(?:^|[\s/、，,|·])短片(?:$|[\s/、，,|·])")

# 流媒体平台常见括号及尾缀修饰词
_LEADING_BRACKET_TAG_RE = re.compile(
    r"^\s*(?:[\[【(（]\s*(?:独播|自制|限免|VIP|会员|4K|超清|首播|热播|更新|出品)[^\]】)）]{0,8}[\]】)）]\s*)+",
    re.IGNORECASE,
)
_PLATFORM_MODIFIER_BRACKET_RE = re.compile(
    r"\s*[\[【(（]\s*(?:"
    r"中配(?:版)?|国配(?:版)?|国语(?:版|配音)?|普通话(?:版)?|日语(?:版|配音)?|日配(?:版)?|"
    r"粤语(?:版|配音)?|英语(?:版|配音)?|原声(?:版)?|台配(?:版)?|闽南语(?:版)?|川渝(?:版|方言版)?|"
    r"方言(?:版)?|多语(?:版)?|双语(?:版)?|无修(?:版|改)?|未删减(?:版)?|纯享(?:版)?|精编(?:版)?|"
    r"加长(?:版)?|剪辑(?:版)?|特别剪辑(?:版)?|会员(?:版|Plus版|专享(?:版)?)?|竖屏(?:版)?|横屏(?:版)?|"
    r"动态漫(?:画)?|4K(?:版|修复(?:版)?|超清)?|超清(?:版)?|高清(?:版)?|合集(?:版)?|全集(?:版)?|限免|独播"
    r")\s*[\]】)）]\s*",
    re.IGNORECASE,
)
_PLATFORM_TRAILING_MODIFIER_RE = re.compile(
    r"(?:[\s·\-—_:：]+|\b)(?:"
    r"动态漫(?:画)?|精编版|纯享版|中配版|国配版|国语版|粤语版|日语版|普通话版|原声版|加长版|未删减版|合集版"
    r")\s*$",
    re.IGNORECASE,
)

# 明确季号正则：支持“第二季”、“第2季”、“Season 2”、“S2”、“S02”等
_EXPLICIT_CN_SEASON_RE = re.compile(
    r"(?:[\s·\-—_:：]*|^)第\s*([0-9]{1,2}|[一二三四五六七八九十百零两]{1,4})\s*季(?=$|[\s·\-—_:：]|[^\w\u4e00-\u9fff])"
)
_EXPLICIT_EN_SEASON_RE = re.compile(
    r"(?:[\s·\-—_:：]+)(?:Season\s*0?([1-9][0-9]?)|S0?([1-9][0-9]?))(?=$|[\s·\-—_:：])",
    re.IGNORECASE,
)
# 末尾辅助单数字季号：前接至少 2 个汉字且前一位不是数字，范围 2~19（排除 1988 / 2049 等年份）
_TRAILING_NUM_SEASON_RE = re.compile(
    r"^(?P<base>.*?[\u4e00-\u9fff]{2,})\s*(?P<num>[2-9]|1[0-9])$"
)
_NON_SEASON_NUMERIC_TITLES = frozenset({
    "零零7",
})


def is_short_drama_tmdb_item(info: dict) -> bool:
    """
    判断 TMDB 发现接口返回的原始条目是否为微短剧/竖屏短剧。

    动画分类（Genre ID 16）一律豁免，避免误杀泡面番与国漫；
    未录入单集时长（空列表或全 0）的新开播剧集绝不按时长误判。

    :param info: TMDB 原始条目字典
    :return: 命中短剧特征时返回 True，否则返回 False
    """
    if not info or not isinstance(info, dict):
        return False

    genre_ids = set()
    for raw_gid in info.get("genre_ids") or []:
        try:
            genre_ids.add(int(raw_gid))
        except (TypeError, ValueError):
            continue
    for genre in info.get("genres") or []:
        if isinstance(genre, dict) and genre.get("id") is not None:
            try:
                genre_ids.add(int(genre.get("id")))
            except (TypeError, ValueError):
                continue

    if _TMDB_ANIMATION_GENRE_ID in genre_ids:
        return False

    for network in info.get("networks") or []:
        if not isinstance(network, dict):
            continue
        net_id = network.get("id")
        try:
            if net_id is not None and int(net_id) in _TMDB_SHORT_DRAMA_NETWORK_IDS:
                return True
        except (TypeError, ValueError):
            pass
        net_name = str(network.get("name") or "").casefold()
        if net_name and any(kw in net_name for kw in _TMDB_SHORT_DRAMA_NETWORK_KEYWORDS):
            return True

    run_times = info.get("episode_run_time") or []
    positive_runtimes = [
        int(val)
        for val in run_times
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0
    ]
    if positive_runtimes and max(positive_runtimes) <= 12:
        return True

    combined_text = " ".join(
        str(info.get(field) or "")
        for field in ("name", "original_name", "title", "original_title", "overview")
    ).casefold()
    return any(marker in combined_text for marker in _TMDB_SHORT_DRAMA_TEXT_MARKERS)


def is_short_drama_text(
    title: str = "",
    subtitle: str = "",
    tags: Iterable[str] = (),
    is_animation: bool = False,
) -> bool:
    """
    根据标题、副标题/简介及类型标签判断是否为微短剧或动态漫/AI漫剧（供豆瓣及国内流媒体探索源复用）。

    正经动画/番剧豁免常规“短片”判断，但显式标注“动态漫/漫剧/沙雕动画”的条目仍予拦截。

    :param title: 媒体标题
    :param subtitle: 副标题、推荐语或豆瓣分栏简介
    :param tags: 类型或风格标签集合
    :param is_animation: 是否已知为动画/番剧/国创/少儿分类
    :return: 属于短剧/漫剧时返回 True，否则返回 False
    """
    subtitle_text = str(subtitle or "")
    combined = f"{title or ''} {subtitle_text}".casefold()

    # 即使是动画频道，显式动态漫 / AI 漫剧 / 沙雕动画短剧一律拦截
    if any(marker in combined for marker in _AI_COMIC_SHORT_MARKERS):
        return True

    normalized_tags = [
        str(tag).strip().casefold()
        for tag in (tags or ())
        if tag is not None and str(tag).strip()
    ]
    if any(
        comic_kw in tag
        for tag in normalized_tags
        for comic_kw in _AI_COMIC_SHORT_MARKERS
    ):
        return True

    has_anim_tag = is_animation or any(
        anim_kw in tag
        for tag in normalized_tags
        for anim_kw in _ANIMATION_TAG_MARKERS
    )
    if not has_anim_tag and "/" in subtitle_text:
        segments = [seg.strip() for seg in subtitle_text.split("/")]
        for seg in segments[:4]:
            if any(anim_kw in seg.casefold() for anim_kw in _ANIMATION_TAG_MARKERS):
                has_anim_tag = True
                break

    if has_anim_tag:
        return False

    if any(
        short_kw == tag or (short_kw != "短片" and short_kw in tag)
        for tag in normalized_tags
        for short_kw in _SHORT_DRAMA_TAG_MARKERS
    ):
        return True

    if any(marker in combined for marker in _GENERIC_SHORT_DRAMA_TEXT_MARKERS):
        return True

    if _SHORT_FILM_TOKEN_RE.search(subtitle_text):
        return True

    if _SHORT_DRAMA_PLOT_RE.search(f"{title or ''} {subtitle_text}"):
        return True

    return False


def is_short_drama_douban_media(media: MediaInfo) -> bool:
    """
    判断豆瓣电影/剧集 MediaInfo 条目是否为竖屏微短剧、AI 漫剧或真人短片。

    1. 纪录片、真人秀、脱口秀、音乐歌舞晚会等非虚构节目一律豁免；
    2. 非动画显式“短片/短剧”及全品类“动态漫/漫剧”直接拦截；
    3. 豆瓣剧集推荐流中的中国大陆条目，若同时无正规播放源、无平台、无有效评分人数、
       无集数更新、无榜单荣誉且剧照不超过 1 张（含挂靠“动画”标签的 AI 修仙漫剧及带单条水军短评的微短剧），精准拦截。

    :param media: 豆瓣转换后的统一媒体信息对象
    :return: 命中短剧/AI漫剧特征时返回 True，否则返回 False
    """
    if not media:
        return False
    raw_info = media.douban_info or {}
    collected_tags: List[str] = []
    for genre in media.genres or []:
        if isinstance(genre, dict) and genre.get("name"):
            collected_tags.append(str(genre["name"]))
        elif genre:
            collected_tags.append(str(genre))
    for field in ("genres", "tags"):
        for val in raw_info.get(field) or []:
            if isinstance(val, dict) and val.get("name"):
                collected_tags.append(str(val["name"]))
            elif val:
                collected_tags.append(str(val))

    subtitle_text = str(raw_info.get("card_subtitle") or media.overview or "")
    tag_and_sub_text = " ".join([*collected_tags, subtitle_text])
    if any(exempt_kw in tag_and_sub_text for exempt_kw in _NON_FICTION_EXEMPT_MARKERS):
        return False

    if is_short_drama_text(
        title=media.title or "",
        subtitle=subtitle_text,
        tags=collected_tags,
    ):
        return True

    if media.type == MediaType.TV and isinstance(raw_info, dict) and "has_linewatch" in raw_info:
        head_segments = [seg.strip() for seg in subtitle_text.split("/")[:3]]
        is_mainland = any("中国大陆" in seg for seg in head_segments)
        if not is_mainland:
            return False

        has_linewatch = bool(raw_info.get("has_linewatch"))
        vendor_count = int(raw_info.get("vendor_count") or 0)
        rating_info = raw_info.get("rating")
        rating_count = int(rating_info.get("count") or 0) if isinstance(rating_info, dict) else 0
        episodes_info = str(raw_info.get("episodes_info") or "").strip()
        honor_infos = raw_info.get("honor_infos") or []
        photo_count = len(raw_info.get("photos") or [])

        # 中国大陆无长视频源 + 无豆瓣开分人数 + 无集数 + 无荣誉 + 剧照<=1（覆盖未标“短片”的竖屏短剧及挂“动画”标签的 AI 漫剧）
        if (
            not has_linewatch
            and vendor_count == 0
            and rating_count == 0
            and not episodes_info
            and not honor_infos
            and photo_count <= 1
        ):
            return True

        # 中国大陆无长视频源 + 无开分人数 + 标题强命中微短剧/AI漫剧爽文套路词
        if not has_linewatch and vendor_count == 0 and rating_count == 0 and _SHORT_DRAMA_PLOT_RE.search(media.title or ""):
            return True

    return False


def _parse_season_token(raw_token: str) -> Optional[int]:
    """将中文数字或阿拉伯数字季号转换为正整数。"""
    token = (raw_token or "").strip()
    if not token:
        return None
    if token.isdigit():
        val = int(token)
        return val if val >= 1 else None
    try:
        val = int(cn2an.cn2an(token, "smart"))
        return val if val >= 1 else None
    except (ValueError, TypeError):
        return None


def _extract_main_series_title(clean_title: str) -> str:
    """从清洗后标题中提取篇章前的主剧名，供一级搜索未命中时二次回退。"""
    if not clean_title:
        return ""

    # 1. 冒号或间隔号分隔副标题：如 "狐妖小红娘·月红篇"、"鬼灭之刃：柱训练篇"
    for sep in ("：", ":", "·"):
        if sep in clean_title:
            head = clean_title.split(sep, 1)[0].strip()
            if len(head) >= 2:
                return head

    # 2. 中文主名 + 空格 + 篇章名：如 "凡人修仙传 星海飞驰篇"
    space_match = re.match(r"^([\u4e00-\u9fff0-9]{2,}(?:之[\u4e00-\u9fff0-9]{2,})?)\s+(.+)$", clean_title)
    if space_match:
        head = space_match.group(1).strip()
        if len(head) >= 2:
            return head

    # 3. “之”字连接篇章副标题：如 "伍六七之暗影宿命"、"唐朝诡事录之西行"
    zhi_match = re.match(r"^([\u4e00-\u9fffA-Za-z0-9]{2,})之([\u4e00-\u9fffA-Za-z0-9]{2,})$", clean_title)
    if zhi_match:
        head = zhi_match.group(1).strip()
        if len(head) >= 2:
            return head

    return clean_title


def parse_discover_title_and_season(
    raw_title: str,
) -> Tuple[str, Optional[int], str]:
    """
    清洗流媒体探索源标题中的平台修饰词，并提取季号与主系列名称。

    :param raw_title: 平台原始标题（如“间谍过家家 第二季（中配）”、“庆余年2”、“凡人修仙传 星海飞驰篇”）
    :return: (clean_title, season, main_series_title) 三元组
    """
    if not raw_title or not str(raw_title).strip():
        return "", None, ""

    working = str(raw_title).strip()
    working = _LEADING_BRACKET_TAG_RE.sub("", working).strip()
    # 循环剥离多重修饰括号与尾缀，例如“第二季 [中配]（粤语版）”
    while True:
        updated = _PLATFORM_MODIFIER_BRACKET_RE.sub(" ", working)
        updated = _PLATFORM_TRAILING_MODIFIER_RE.sub("", updated)
        updated = re.sub(r"\s{2,}", " ", updated).strip()
        if updated == working:
            break
        working = updated

    season: Optional[int] = None

    # 优先匹配中文“第X季”
    cn_match = _EXPLICIT_CN_SEASON_RE.search(working)
    if cn_match:
        parsed = _parse_season_token(cn_match.group(1))
        prefix = working[:cn_match.start()].strip(" ·-—_:：")
        suffix = working[cn_match.end():].strip(" ·-—_:：")
        if parsed is not None and prefix:
            season = parsed
            working = f"{prefix} {suffix}".strip() if suffix else prefix

    # 再匹配英文 Season X / S02
    if season is None:
        en_match = _EXPLICIT_EN_SEASON_RE.search(working)
        if en_match:
            raw_num = en_match.group(1) or en_match.group(2)
            parsed = _parse_season_token(raw_num)
            prefix = working[:en_match.start()].strip(" ·-—_:：")
            suffix = working[en_match.end():].strip(" ·-—_:：")
            if parsed is not None and prefix:
                season = parsed
                working = f"{prefix} {suffix}".strip() if suffix else prefix

    # 兼容末尾单数字季号：如“庆余年2”、“画江湖之不良人6”（不误伤 1988 / 2049 等年份）
    if season is None and working not in _NON_SEASON_NUMERIC_TITLES:
        num_match = _TRAILING_NUM_SEASON_RE.match(working)
        if num_match:
            base = num_match.group("base").strip()
            # 确保 base 末尾不是数字，避免拆分四位年份或连续编号
            if base and not base[-1].isdigit():
                season = int(num_match.group("num"))
                working = base

    clean_title = re.sub(r"\s{2,}", " ", working).strip()
    if not clean_title:
        clean_title = str(raw_title).strip()

    main_series_title = _extract_main_series_title(clean_title)
    return clean_title, season, main_series_title


def _normalize_recognized_authority(mediainfo: Optional[MediaInfo]) -> Optional[MediaInfo]:
    """
    若第三方插件篡改了 mediainfo.source（如设为 bilibili），但对象实际拥有权威源 ID，
    将其修正回真实权威源与对应 media_id，恢复分季与剧集组能力。
    """
    if not mediainfo:
        return None
    normalized_source = normalize_media_source(mediainfo.source)
    if normalized_source in MEDIA_SOURCE_ID_FIELDS:
        return mediainfo

    if mediainfo.tmdb_id:
        mediainfo.source = "themoviedb"
        mediainfo.media_id = str(mediainfo.tmdb_id)
    elif mediainfo.douban_id:
        mediainfo.source = "douban"
        mediainfo.media_id = str(mediainfo.douban_id)
    elif mediainfo.bangumi_id:
        mediainfo.source = "bangumi"
        mediainfo.media_id = str(mediainfo.bangumi_id)
    elif mediainfo.anilist_id:
        mediainfo.source = "anilist"
        mediainfo.media_id = str(mediainfo.anilist_id)
    return mediainfo


def _candidate_recognize_sources() -> List[str]:
    """按系统主识别源优先、TMDB/豆瓣互为备用的顺序构造识别源列表。"""
    primary = normalize_media_source(settings.RECOGNIZE_SOURCE) or "themoviedb"
    sources = [primary]
    for fallback in ("themoviedb", "douban"):
        if fallback not in sources:
            sources.append(fallback)
    return sources


def _build_search_meta(
    name: str,
    year: Optional[str],
    mtype: Optional[MediaType],
    season: Optional[int],
) -> MetaInfo:
    """构造用于标题关联识别的 MetaInfo。"""
    meta = MetaInfo(name)
    meta.name = name
    if not meta.cn_name:
        meta.cn_name = name
    if year and str(year).strip() and str(year).strip() != "None":
        meta.year = str(year).strip()[:4]
    if mtype:
        meta.type = mtype
    if season is not None and mtype != MediaType.MOVIE:
        meta.begin_season = season
        if not meta.type or meta.type == MediaType.UNKNOWN:
            meta.type = MediaType.TV
    return meta


def resolve_external_media(
    mediaid: str,
    mtype: Optional[MediaType] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[int] = None,
    *,
    mediachain: Optional[Any] = None,
    recognize_media_fn: Optional[Any] = None,
    event_mgr: Optional[Any] = None,
) -> Tuple[Optional[MediaInfo], Optional[int]]:
    """
    同步将非原生外部探索源条目关联解析为权威媒体信息（TMDB/豆瓣等）与目标季号。

    :param mediaid: 带前缀的媒体键（如 bilibili:28229420）
    :param mtype: 可选媒体类型
    :param title: 可选原始标题；缺失时自动从探索缓存或平台详情接口回查补齐
    :param year: 可选年份
    :param season: 调用方显式指定的季号
    :param mediachain: 可选注入的 MediaChain 实例
    :param recognize_media_fn: 可选注入的 ID 识别函数
    :param event_mgr: 可选注入的事件管理器
    :return: (权威 MediaInfo 或 None, 解析出的有效季号或 None)
    """
    media_source, source_media_id = parse_media_key(mediaid)
    if media_source in MEDIA_SOURCE_ID_FIELDS:
        return None, season

    from app.helper import discover_sources

    cached_season: Optional[int] = None
    cached_item = discover_sources.get_cached_discover_item(mediaid)
    if cached_item:
        title = title or cached_item.get("title")
        year = year or cached_item.get("year")
        mtype = mtype or cached_item.get("mtype")
        cached_season = cached_item.get("season")

    if not title and media_source and source_media_id:
        detail = discover_sources.fetch_discover_item_detail(media_source, source_media_id)
        if detail:
            title = title or detail.get("title")
            year = year or detail.get("year")
            mtype = mtype or detail.get("mtype")
            cached_season = cached_season if cached_season is not None else detail.get("season")

    clean_title, parsed_season, main_series_title = (
        parse_discover_title_and_season(title) if title else ("", None, "")
    )
    effective_season = (
        season
        if season is not None
        else (parsed_season if parsed_season is not None else cached_season)
    )
    effective_mtype = mtype
    if effective_mtype == MediaType.MOVIE:
        effective_season = None
    elif effective_mtype is None and effective_season is not None:
        effective_mtype = MediaType.TV

    chain = mediachain or MediaChain()
    rec_media = recognize_media_fn or chain.recognize_media
    ev_mgr = event_mgr or eventmanager
    mediainfo: Optional[MediaInfo] = None

    # 1. 优先尝试已注册模块按外部 source + media_id 直接识别，并修正被篡改的权威源身份
    if media_source and source_media_id:
        mediainfo = rec_media(
            source=media_source,
            mediaid=source_media_id,
            mtype=effective_mtype,
        )
        mediainfo = _normalize_recognized_authority(mediainfo)

    # 2. 兼容 MediaRecognizeConvert 插件转换事件
    if not mediainfo and mediaid:
        event_data = MediaRecognizeConvertEventData(
            mediaid=mediaid,
            convert_type=settings.RECOGNIZE_SOURCE,
        )
        event = ev_mgr.send_event(ChainEventType.MediaRecognizeConvert, event_data)
        if event and event.event_data and event.event_data.media_dict:
            conv_data: MediaRecognizeConvertEventData = event.event_data
            new_id = conv_data.media_dict.get("id")
            if new_id is not None and conv_data.convert_type:
                mediainfo = rec_media(
                    source=conv_data.convert_type,
                    mediaid=str(new_id),
                    mtype=effective_mtype,
                )
                mediainfo = _normalize_recognized_authority(mediainfo)

    # 3. 按清洗后标题在主识别源与备用权威源中识别，必要时放宽平台差异年份并用主剧名二次回退
    candidate_titles = [t for t in dict.fromkeys([clean_title, main_series_title]) if t]
    for candidate_title in candidate_titles:
        if mediainfo:
            break
        for rec_source in _candidate_recognize_sources():
            search_meta = _build_search_meta(
                candidate_title, year, effective_mtype, effective_season
            )
            mediainfo = chain.recognize_by_meta(
                search_meta,
                source=rec_source,
                obtain_images=False,
            )
            if not mediainfo and year:
                relaxed_meta = _build_search_meta(
                    candidate_title, None, effective_mtype, effective_season
                )
                mediainfo = chain.recognize_by_meta(
                    relaxed_meta,
                    source=rec_source,
                    obtain_images=False,
                )
            if mediainfo:
                mediainfo = _normalize_recognized_authority(mediainfo)
                break

    if mediainfo:
        if mediainfo.type == MediaType.MOVIE:
            effective_season = None
        elif effective_season is not None and mediainfo.season is None:
            mediainfo.season = effective_season
        elif effective_season is None and mediainfo.season is not None:
            effective_season = mediainfo.season
        return mediainfo, effective_season

    return None, effective_season


async def async_resolve_external_media(
    mediaid: str,
    mtype: Optional[MediaType] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[int] = None,
    *,
    mediachain: Optional[Any] = None,
    recognize_media_fn: Optional[Any] = None,
    event_mgr: Optional[Any] = None,
) -> Tuple[Optional[MediaInfo], Optional[int]]:
    """
    异步将非原生外部探索源条目关联解析为权威媒体信息（TMDB/豆瓣等）与目标季号。

    :param mediaid: 带前缀的媒体键（如 bilibili:28229420）
    :param mtype: 可选媒体类型
    :param title: 可选原始标题；缺失时自动从探索缓存或平台详情接口回查补齐
    :param year: 可选年份
    :param season: 调用方显式指定的季号
    :param mediachain: 可选注入的 MediaChain 实例
    :param recognize_media_fn: 可选注入的异步 ID 识别函数
    :param event_mgr: 可选注入的事件管理器
    :return: (权威 MediaInfo 或 None, 解析出的有效季号或 None)
    """
    media_source, source_media_id = parse_media_key(mediaid)
    if media_source in MEDIA_SOURCE_ID_FIELDS:
        return None, season

    from app.helper import discover_sources

    cached_season: Optional[int] = None
    cached_item = discover_sources.get_cached_discover_item(mediaid)
    if cached_item:
        title = title or cached_item.get("title")
        year = year or cached_item.get("year")
        mtype = mtype or cached_item.get("mtype")
        cached_season = cached_item.get("season")

    if not title and media_source and source_media_id:
        detail = await discover_sources.async_fetch_discover_item_detail(
            media_source, source_media_id
        )
        if detail:
            title = title or detail.get("title")
            year = year or detail.get("year")
            mtype = mtype or detail.get("mtype")
            cached_season = cached_season if cached_season is not None else detail.get("season")

    clean_title, parsed_season, main_series_title = (
        parse_discover_title_and_season(title) if title else ("", None, "")
    )
    effective_season = (
        season
        if season is not None
        else (parsed_season if parsed_season is not None else cached_season)
    )
    effective_mtype = mtype
    if effective_mtype == MediaType.MOVIE:
        effective_season = None
    elif effective_mtype is None and effective_season is not None:
        effective_mtype = MediaType.TV

    chain = mediachain or MediaChain()
    rec_media = recognize_media_fn or chain.async_recognize_media
    ev_mgr = event_mgr or eventmanager
    mediainfo: Optional[MediaInfo] = None

    # 1. 优先尝试已注册模块按外部 source + media_id 直接识别，并修正被篡改的权威源身份
    if media_source and source_media_id:
        mediainfo = await rec_media(
            source=media_source,
            mediaid=source_media_id,
            mtype=effective_mtype,
        )
        mediainfo = _normalize_recognized_authority(mediainfo)

    # 2. 兼容 MediaRecognizeConvert 插件转换事件
    if not mediainfo and mediaid:
        event_data = MediaRecognizeConvertEventData(
            mediaid=mediaid,
            convert_type=settings.RECOGNIZE_SOURCE,
        )
        event = await ev_mgr.async_send_event(
            ChainEventType.MediaRecognizeConvert, event_data
        )
        if event and event.event_data and event.event_data.media_dict:
            conv_data: MediaRecognizeConvertEventData = event.event_data
            new_id = conv_data.media_dict.get("id")
            if new_id is not None and conv_data.convert_type:
                mediainfo = await rec_media(
                    source=conv_data.convert_type,
                    mediaid=str(new_id),
                    mtype=effective_mtype,
                )
                mediainfo = _normalize_recognized_authority(mediainfo)

    # 3. 按清洗后标题在主识别源与备用权威源中识别，未命中时用篇章主剧名二次回退
    candidate_titles = [t for t in dict.fromkeys([clean_title, main_series_title]) if t]
    for candidate_title in candidate_titles:
        if mediainfo:
            break
        for rec_source in _candidate_recognize_sources():
            search_meta = _build_search_meta(
                candidate_title, year, effective_mtype, effective_season
            )
            mediainfo = await chain.async_recognize_by_meta(
                search_meta,
                source=rec_source,
                obtain_images=False,
            )
            if not mediainfo and year:
                relaxed_meta = _build_search_meta(
                    candidate_title, None, effective_mtype, effective_season
                )
                mediainfo = await chain.async_recognize_by_meta(
                    relaxed_meta,
                    source=rec_source,
                    obtain_images=False,
                )
            if mediainfo:
                mediainfo = _normalize_recognized_authority(mediainfo)
                break

    if mediainfo:
        if mediainfo.type == MediaType.MOVIE:
            effective_season = None
        elif effective_season is not None and mediainfo.season is None:
            mediainfo.season = effective_season
        elif effective_season is None and mediainfo.season is not None:
            effective_season = mediainfo.season
        return mediainfo, effective_season

    return None, effective_season
