from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app import schemas
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.security import verify_token
from app.helper.discover_helper import is_short_drama_douban_media
from app.helper.discover_sources import (
    async_discover_bilibili,
    async_discover_mangguo,
    async_discover_tencentvideo,
    get_builtin_discover_sources,
)
from app.schemas import DiscoverSourceEventData
from app.schemas.types import ChainEventType, MediaType

router = APIRouter()


def _extract_bangumi_weekday(media: MediaInfo) -> Optional[int]:
    """从 Bangumi MediaInfo 提取放送星期（1=周一 ... 7=周日）。"""
    raw_info = media.bangumi_info or {}
    weekday = raw_info.get("air_weekday") or getattr(media, "air_weekday", None)
    if weekday is not None:
        try:
            day = int(weekday)
            if 1 <= day <= 7:
                return day
        except (TypeError, ValueError):
            pass
    if media.release_date and len(str(media.release_date)) >= 10:
        try:
            return datetime.strptime(str(media.release_date)[:10], "%Y-%m-%d").isoweekday()
        except ValueError:
            pass
    return None


@router.get(
    "/source",
    summary="获取探索数据源",
    response_model=List[schemas.DiscoverMediaSource],
)
def source(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取内置与插件扩展探索数据源列表，并按 mediaid_prefix 去重。
    """
    sources_by_prefix: Dict[str, schemas.DiscoverMediaSource] = {}
    for builtin in get_builtin_discover_sources():
        if builtin and builtin.mediaid_prefix:
            sources_by_prefix[builtin.mediaid_prefix] = builtin

    event_data = DiscoverSourceEventData()
    event = eventmanager.send_event(ChainEventType.DiscoverSource, event_data)
    if event and event.event_data:
        event_data = event.event_data
        for extra in event_data.extra_sources or []:
            if not extra or not extra.mediaid_prefix:
                continue
            sources_by_prefix[extra.mediaid_prefix] = extra
    return list(sources_by_prefix.values())


@router.get("/bangumi", summary="探索Bangumi", response_model=List[schemas.MediaInfo])
async def bangumi(
    type: Optional[int] = 2,
    cat: Optional[int] = None,
    sort: Optional[str] = "rank",
    year: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    探索Bangumi
    """
    medias = await BangumiChain().async_discover(
        type=type, cat=cat, sort=sort, year=year, limit=count, offset=(page - 1) * count
    )
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/bangumi_calendar",
    summary="探索Bangumi每日放送",
    response_model=List[schemas.MediaInfo],
)
async def bangumi_calendar(
    weekday: Optional[int] = 0,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    按星期浏览 Bangumi 每日放送番剧，保留原生 bangumi 媒体身份。
    """
    medias = await BangumiChain().async_calendar()
    if not medias:
        return []
    if weekday and 1 <= int(weekday) <= 7:
        target_day = int(weekday)
        filtered = [m for m in medias if _extract_bangumi_weekday(m) == target_day]
        return [media.to_dict() for media in filtered]

    today_weekday = datetime.now().isoweekday()
    sorted_medias = sorted(
        medias,
        key=lambda m: (
            (_extract_bangumi_weekday(m) - today_weekday) % 7
            if _extract_bangumi_weekday(m) is not None
            else 99
        ),
    )
    return [media.to_dict() for media in sorted_medias]


@router.get("/bilibili", summary="探索哔哩哔哩", response_model=List[schemas.MediaInfo])
async def bilibili(
    mtype: Optional[str] = "bangumi",
    order: Optional[str] = "2",
    area: Optional[str] = None,
    style_id: Optional[str] = None,
    season_version: Optional[str] = None,
    spoken_language_type: Optional[str] = None,
    season_status: Optional[str] = None,
    is_finish: Optional[str] = None,
    copyright: Optional[str] = None,
    _copyright: Optional[str] = Query(default=None, alias="_copyright"),
    year: Optional[str] = None,
    exclude_short_drama: Optional[bool] = False,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览哔哩哔哩番剧、国创、电视剧、电影、纪录片与综艺。
    """
    effective_copyright = copyright if copyright not in (None, "") else _copyright
    medias = await async_discover_bilibili(
        mtype=mtype,
        order=order,
        area=area,
        style_id=style_id,
        season_version=season_version,
        spoken_language_type=spoken_language_type,
        season_status=season_status,
        is_finish=is_finish,
        copyright_val=effective_copyright,
        year=year,
        exclude_short_drama=bool(exclude_short_drama),
        page=page or 1,
        count=count or 30,
    )
    return [media.to_dict() for media in medias] if medias else []


@router.get(
    "/tencentvideo", summary="探索腾讯视频", response_model=List[schemas.MediaInfo]
)
async def tencentvideo(
    mtype: Optional[str] = "tv",
    sort: Optional[str] = "75",
    itype: Optional[str] = None,
    iarea: Optional[str] = None,
    iyear: Optional[str] = None,
    ipay: Optional[str] = None,
    characteristic: Optional[str] = None,
    exclude_short_drama: Optional[bool] = False,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览腾讯视频电视剧、电影、动漫、综艺、纪录片与少儿内容。
    """
    medias = await async_discover_tencentvideo(
        mtype=mtype,
        sort=sort,
        itype=itype,
        iarea=iarea,
        iyear=iyear,
        ipay=ipay,
        characteristic=characteristic,
        exclude_short_drama=bool(exclude_short_drama),
        page=page or 1,
        count=count or 30,
    )
    return [media.to_dict() for media in medias] if medias else []


@router.get("/mangguo", summary="探索芒果TV", response_model=List[schemas.MediaInfo])
async def mangguo(
    mtype: Optional[str] = "tv",
    sort: Optional[str] = "c2",
    kind: Optional[str] = None,
    area: Optional[str] = None,
    year: Optional[str] = None,
    chargeInfo: Optional[str] = None,
    exclude_short_drama: Optional[bool] = False,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览芒果TV综艺、电视剧、电影、动漫、纪录片与少儿内容。
    """
    medias = await async_discover_mangguo(
        mtype=mtype,
        sort=sort,
        kind=kind,
        area=area,
        year=year,
        charge_info=chargeInfo,
        exclude_short_drama=bool(exclude_short_drama),
        page=page or 1,
        count=count or 30,
    )
    return [media.to_dict() for media in medias] if medias else []


@router.get(
    "/douban_movies", summary="探索豆瓣电影", response_model=List[schemas.MediaInfo]
)
async def douban_movies(
    sort: Optional[str] = "R",
    tags: Optional[str] = "",
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    exclude_short_drama: Optional[bool] = False,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = await DoubanChain().async_douban_discover(
        mtype=MediaType.MOVIE,
        sort=sort,
        tags=tags,
        page=page,
        count=count,
        exclude_short_drama=bool(exclude_short_drama),
    )
    if not movies:
        return []
    if exclude_short_drama and "短片" not in (tags or ""):
        movies = [movie for movie in movies if not is_short_drama_douban_media(movie)]
    return [media.to_dict() for media in movies]


@router.get(
    "/douban_tvs", summary="探索豆瓣剧集", response_model=List[schemas.MediaInfo]
)
async def douban_tvs(
    sort: Optional[str] = "R",
    tags: Optional[str] = "",
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    exclude_short_drama: Optional[bool] = False,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = await DoubanChain().async_douban_discover(
        mtype=MediaType.TV,
        sort=sort,
        tags=tags,
        page=page,
        count=count,
        exclude_short_drama=bool(exclude_short_drama),
    )
    if not tvs:
        return []
    if exclude_short_drama and "短片" not in (tags or ""):
        tvs = [tv for tv in tvs if not is_short_drama_douban_media(tv)]
    return [media.to_dict() for media in tvs]


@router.get(
    "/tmdb_movies", summary="探索TMDB电影", response_model=List[schemas.MediaInfo]
)
async def tmdb_movies(
    sort_by: Optional[str] = "popularity.desc",
    with_genres: Optional[str] = "",
    with_original_language: Optional[str] = "",
    with_keywords: Optional[str] = "",
    with_watch_providers: Optional[str] = "",
    vote_average: Optional[float] = 0.0,
    vote_count: Optional[int] = 0,
    release_date: Optional[str] = "",
    release_date_lte: Optional[str] = "",
    with_networks: Optional[str] = "",
    with_runtime_gte: Optional[int] = 0,
    exclude_short_drama: Optional[bool] = False,
    page: Optional[int] = 1,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览TMDB电影信息
    """
    movies = await TmdbChain().async_tmdb_discover(
        mtype=MediaType.MOVIE,
        sort_by=sort_by,
        with_genres=with_genres,
        with_original_language=with_original_language,
        with_keywords=with_keywords,
        with_watch_providers=with_watch_providers,
        vote_average=vote_average,
        vote_count=vote_count,
        release_date=release_date,
        release_date_lte=release_date_lte,
        with_networks=with_networks,
        with_runtime_gte=with_runtime_gte,
        exclude_short_drama=exclude_short_drama,
        page=page,
    )
    return [movie.to_dict() for movie in movies] if movies else []


@router.get("/tmdb_tvs", summary="探索TMDB剧集", response_model=List[schemas.MediaInfo])
async def tmdb_tvs(
    sort_by: Optional[str] = "popularity.desc",
    with_genres: Optional[str] = "",
    with_original_language: Optional[str] = "",
    with_keywords: Optional[str] = "",
    with_watch_providers: Optional[str] = "",
    vote_average: Optional[float] = 0.0,
    vote_count: Optional[int] = 0,
    release_date: Optional[str] = "",
    release_date_lte: Optional[str] = "",
    with_networks: Optional[str] = "",
    with_runtime_gte: Optional[int] = 0,
    exclude_short_drama: Optional[bool] = False,
    page: Optional[int] = 1,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览TMDB剧集信息
    """
    tvs = await TmdbChain().async_tmdb_discover(
        mtype=MediaType.TV,
        sort_by=sort_by,
        with_genres=with_genres,
        with_original_language=with_original_language,
        with_keywords=with_keywords,
        with_watch_providers=with_watch_providers,
        vote_average=vote_average,
        vote_count=vote_count,
        release_date=release_date,
        release_date_lte=release_date_lte,
        with_networks=with_networks,
        with_runtime_gte=with_runtime_gte,
        exclude_short_drama=exclude_short_drama,
        page=page,
    )
    return [tv.to_dict() for tv in tvs] if tvs else []
