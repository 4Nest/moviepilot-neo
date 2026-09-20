from typing import List, Any, Annotated, Optional

import cn2an
from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_token, verify_apitoken
from app.db import get_async_db, get_db
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_admin, get_current_admin_async
from app.helper.server import MoviePilotServerHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.event import SubscribeModifiedEventData
from app.schemas.types import MediaType, EventType, SystemConfigKey
from app.utils.media import normalize_media_source, parse_media_key

router = APIRouter()


def start_subscribe_add(
    title: str, year: str, mtype: MediaType, tmdbid: int, season: int, username: str
):
    """
    启动订阅任务
    """
    SubscribeChain().add(
        title=title,
        year=year,
        mtype=mtype,
        tmdbid=tmdbid,
        season=season,
        username=username,
    )


def build_subscribe_event_payload(subscribe: Subscribe) -> dict:
    """
    从 ORM 已加载字段构造订阅事件快照，避免异步接口里属性懒加载触发隐式 IO。
    """
    values = subscribe.__dict__
    return {column.name: values.get(column.name) for column in subscribe.__table__.columns}


async def list_subscribes_by_media_key(
        db: AsyncSession, media_key: str, season: Optional[int] = None,
) -> List[Subscribe]:
    """按统一媒体键查询订阅，并兼容迁移前的专用 ID 字段。"""
    source, media_id = parse_media_key(media_key)
    if not source or not media_id:
        return await Subscribe.async_list_by_mediaid(db, media_key)

    subscribes = list(await Subscribe.async_list_by_media_identity(
        db, media_source=source, media_id=media_id
    ))
    if source == "themoviedb" and media_id.isdigit():
        subscribes.extend(await Subscribe.async_get_by_tmdbid(db, int(media_id), season))
    elif source == "douban":
        subscribes.extend(await Subscribe.async_list_by_doubanid(db, media_id))
    elif source == "bangumi" and media_id.isdigit():
        subscribes.extend(await Subscribe.async_list_by_bangumiid(db, int(media_id)))
    elif source == "anilist" and media_id.isdigit():
        subscribes.extend(await Subscribe.async_list_by_anilistid(db, int(media_id)))

    unique_subscribes = {subscribe.id: subscribe for subscribe in subscribes}
    if season is not None:
        return [
            subscribe for subscribe in unique_subscribes.values()
            if subscribe.season == season
        ]
    return list(unique_subscribes.values())


@router.get("/", summary="查询所有订阅", response_model=List[schemas.Subscribe])
async def read_subscribes(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    查询所有订阅
    """
    return await Subscribe.async_list(db)


@router.get(
    "/list", summary="查询所有订阅（API_TOKEN）", response_model=List[schemas.Subscribe]
)
async def list_subscribes(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    查询所有订阅 API_TOKEN认证（?token=xxx）
    """
    return await Subscribe.async_list()


@router.post("/", summary="新增订阅", response_model=schemas.Response)
async def create_subscribe(
    *,
    subscribe_in: schemas.Subscribe,
    current_user: User = Depends(get_current_admin_async),
) -> schemas.Response:
    """
    新增订阅
    """
    # 类型转换
    if subscribe_in.type:
        mtype = MediaType(subscribe_in.type)
    else:
        mtype = None
    # 非 TMDB 来源的标题可能自带季标记，入库前统一拆分。
    if (
            subscribe_in.doubanid
            or subscribe_in.bangumiid
            or subscribe_in.anilistid
            or normalize_media_source(subscribe_in.media_source) not in (None, "themoviedb")
    ):
        meta = MetaInfo(subscribe_in.name)
        subscribe_in.name = meta.name
        if subscribe_in.season is None:
            subscribe_in.season = meta.begin_season
    # 标题转换
    if subscribe_in.name:
        title = subscribe_in.name
    else:
        title = None
    subscribe_dict = subscribe_in.to_public_write_payload()
    subscribe_dict["username"] = settings.SUPERUSER
    sid, message = await SubscribeChain().async_add(
        mtype=mtype,
        title=title,
        exist_ok=True,
        **subscribe_dict,
    )
    return schemas.Response(success=bool(sid), message=message, data={"id": sid})

@router.put("/", summary="更新订阅", response_model=schemas.Response)
async def update_subscribe(
    *,
    subscribe_in: schemas.Subscribe,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    更新订阅信息
    """
    subscribe = await Subscribe.async_get(db, subscribe_in.id)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    old_subscribe_dict = subscribe.to_dict()
    subscribe_dict = subscribe_in.to_public_write_payload()
    # 旧客户端遗漏 version_rules 时保留已存在规则；显式数组才替换规则集合。
    if "version_rules" not in subscribe_in.model_fields_set:
        subscribe_dict.pop("version_rules", None)
        subscribe_dict.pop("version_mode", None)
    else:
        subscribe_dict["version_mode"] = "all" if subscribe_in.version_rules else "any"
    # 旧客户端未携带新开关字段时保留已存值,避免 schema 默认值 0 覆盖已开启的开关
    if "skip_library_check" not in subscribe_in.model_fields_set:
        subscribe_dict.pop("skip_library_check", None)
    subscribe_dict["username"] = subscribe.username
    if subscribe_in.total_episode and subscribe_in.total_episode > (subscribe.total_episode or 0):
        # 扩大目标范围时，新增加的集数尚无下载事实，应同步计入缺失集数。
        subscribe_dict["lack_episode"] = (subscribe.lack_episode or 0) + (
            subscribe_in.total_episode - (subscribe.total_episode or 0)
        )
    # 是否手动修改过总集数
    if subscribe_in.total_episode != subscribe.total_episode:
        subscribe_dict["manual_total_episode"] = 1
    # 更新到数据库
    await subscribe.async_update(db, subscribe_dict)
    # 重新获取更新后的订阅数据
    updated_subscribe = await Subscribe.async_get(db, subscribe_in.id)
    # 发送订阅调整事件
    await eventmanager.async_send_event(
        EventType.SubscribeModified,
        SubscribeModifiedEventData(
            subscribe_id=subscribe_in.id,
            old_subscribe_info=old_subscribe_dict,
            subscribe_info=updated_subscribe.to_dict() if updated_subscribe else {},
            scene="update",
        ).to_dict(),
    )
    return schemas.Response(success=True)

@router.put("/status/{subid}", summary="更新订阅状态", response_model=schemas.Response)
async def update_subscribe_status(
    subid: int,
    state: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    更新订阅状态
    """
    subscribe = await Subscribe.async_get(db, subid)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    valid_states = ["R", "P", "S"]
    if state not in valid_states:
        return schemas.Response(success=False, message="无效的订阅状态")
    old_subscribe_dict = subscribe.to_dict()
    await subscribe.async_update(db, {"state": state})
    # 重新获取更新后的订阅数据
    updated_subscribe = await Subscribe.async_get(db, subid)
    # 发送订阅调整事件
    await eventmanager.async_send_event(
        EventType.SubscribeModified,
        SubscribeModifiedEventData(
            subscribe_id=subid,
            old_subscribe_info=old_subscribe_dict,
            subscribe_info=updated_subscribe.to_dict() if updated_subscribe else {},
            scene="status",
        ).to_dict(),
    )
    return schemas.Response(success=True)


@router.get("/media/{mediaid}", summary="查询订阅", response_model=schemas.Subscribe)
async def subscribe_mediaid(
    mediaid: str,
    season: Optional[int] = None,
    title: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    根据 TMDB、豆瓣、Bangumi、AniList 或插件媒体键查询订阅。
    """
    subscribes = await list_subscribes_by_media_key(db, mediaid, season)
    result = subscribes[0] if subscribes else None
    source, _ = parse_media_key(mediaid)
    title_check = not result and bool(title) and source != "themoviedb"
    # 使用名称检查订阅
    if title_check and title:
        meta = MetaInfo(title)
        if season is not None:
            meta.begin_season = season
        subscribes = await Subscribe.async_list_by_title(
            db, title=meta.name, season=meta.begin_season
        )
        result = subscribes[0] if subscribes else None
    return result if result else Subscribe()
@router.get("/refresh", summary="刷新订阅", response_model=schemas.Response)
def refresh_subscribes(
    current_user: User = Depends(get_current_admin),
) -> Any:
    """
    刷新所有订阅
    """
    Scheduler().start("subscribe_refresh")
    return schemas.Response(success=True)

async def _clear_subscribe_progress(subid: int, db: AsyncSession) -> schemas.Response:
    """清空订阅运行事实并恢复搜索状态。"""
    subscribe = await Subscribe.async_get(db, subid)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    old_subscribe_dict = subscribe.to_dict()
    await subscribe.async_update(
        db,
        {
            "note": [],
            "lack_episode": subscribe.total_episode,
            "current_priority": None,
            "episode_priority": {},
            "manual_total_episode": 0,
            "version_progress": {},
            "decision_summary": None,
            "state": "R",
        },
    )
    updated_subscribe = await Subscribe.async_get(db, subid)
    await eventmanager.async_send_event(
        EventType.SubscribeModified,
        SubscribeModifiedEventData(
            subscribe_id=subid,
            old_subscribe_info=old_subscribe_dict,
            subscribe_info=updated_subscribe.to_dict() if updated_subscribe else {},
            scene="reset",
        ).to_dict(),
    )
    return schemas.Response(success=True)


@router.post("/{subid}/clear-progress", summary="清空订阅进度", response_model=schemas.Response)
async def clear_subscribe_progress(
    subid: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> schemas.Response:
    """清空下载事实、版本进度和完成状态。"""
    return await _clear_subscribe_progress(subid, db)


@router.post("/{subid}/recompute-progress", summary="重新计算订阅进度", response_model=schemas.Response)
async def recompute_subscribe_progress(
    subid: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> schemas.Response:
    """根据媒体库与下载事实重新计算进度，不清空任何事实。"""
    subscribe = await Subscribe.async_get(db, subid)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    summary = SubscribeChain().refresh_subscribe_progress(subscribe, scene="manual_recompute")
    return schemas.Response(success=summary.get("reason") != "resolve_missing_failed", data=summary)


@router.post("/{subid}/force-search", summary="强制搜索一次", response_model=schemas.Response)
async def force_search_subscribe(
    subid: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> schemas.Response:
    """本次忽略媒体库已有判断执行搜索，不修改订阅持久设置。"""
    if not await Subscribe.async_get(db, subid):
        return schemas.Response(success=False, message="订阅不存在")
    background_tasks.add_task(
        Scheduler().start,
        job_id="subscribe_search",
        **{"sid": subid, "state": None, "manual": True, "force_search": True},
    )
    return schemas.Response(success=True)



@router.get("/check", summary="刷新订阅 TMDB 信息", response_model=schemas.Response)
def check_subscribes(
    current_user: User = Depends(get_current_admin),
) -> Any:
    """
    刷新订阅 TMDB 信息
    """
    Scheduler().start("subscribe_tmdb")
    return schemas.Response(success=True)


@router.get("/search", summary="搜索所有订阅", response_model=schemas.Response)
async def search_subscribes(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    搜索所有订阅
    """
    background_tasks.add_task(
        Scheduler().start,
        job_id="subscribe_search",
        **{"sid": None, "state": "R", "manual": True},
    )
    return schemas.Response(success=True)


@router.get(
    "/search/{subscribe_id}", summary="搜索订阅", response_model=schemas.Response
)
async def search_subscribe(
    subscribe_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    根据订阅编号搜索订阅
    """
    subscribe = await Subscribe.async_get(db, subscribe_id)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    background_tasks.add_task(
        Scheduler().start,
        job_id="subscribe_search",
        **{"sid": subscribe_id, "state": None, "manual": True},
    )
    return schemas.Response(success=True)


@router.delete("/media/{mediaid}", summary="删除订阅", response_model=schemas.Response)
async def delete_subscribe_by_mediaid(
    mediaid: str,
    season: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    根据任意媒体数据源 ID 删除订阅。
    """
    delete_subscribes = await list_subscribes_by_media_key(db, mediaid, season)
    delete_events = []
    for subscribe in delete_subscribes:
        subscribe_info = build_subscribe_event_payload(subscribe)
        subscribe_id = subscribe_info.get("id")
        if not subscribe_id:
            continue
        delete_events.append((subscribe_id, subscribe_info))
        await db.delete(subscribe)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    for subscribe_id, subscribe_info in delete_events:
        try:
            await eventmanager.async_send_event(
                EventType.SubscribeDeleted,
                {"subscribe_id": subscribe_id, "subscribe_info": subscribe_info},
            )
        except Exception as err:
            logger.error(f"发送订阅删除事件失败：{subscribe_id} - {err}", exc_info=True)
    return schemas.Response(success=True)


@router.post(
    "/seerr", summary="OverSeerr/JellySeerr通知订阅", response_model=schemas.Response
)
async def seerr_subscribe(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Annotated[str | None, Header()] = None,
) -> Any:
    """
    Jellyseerr/Overseerr网络勾子通知订阅
    """
    if not authorization or authorization != settings.API_TOKEN:
        raise HTTPException(
            status_code=400,
            detail="授权失败",
        )
    req_json = await request.json()
    if not req_json:
        raise HTTPException(
            status_code=500,
            detail="报文内容为空",
        )
    notification_type = req_json.get("notification_type")
    if notification_type not in ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"]:
        return schemas.Response(success=False, message="不支持的通知类型")
    subject = req_json.get("subject")
    media_type = (
        MediaType.MOVIE
        if req_json.get("media", {}).get("media_type") == "movie"
        else MediaType.TV
    )
    tmdbId = req_json.get("media", {}).get("tmdbId")
    if not media_type or not tmdbId or not subject:
        return schemas.Response(success=False, message="请求参数不正确")
    # 唯一账号体系下，通知来源的订阅统一归属 canonical admin
    user_name = settings.SUPERUSER
    # 添加订阅
    if media_type == MediaType.MOVIE:
        background_tasks.add_task(
            start_subscribe_add,
            mtype=media_type,
            tmdbid=tmdbId,
            title=subject,
            year="",
            # 电影不传季号，避免被误判为剧集（S00）并污染通知标题
            season=None,
            username=user_name,
        )
    else:
        seasons = []
        for extra in req_json.get("extra", []):
            if extra.get("name") == "Requested Seasons":
                seasons = [
                    int(str(sea).strip())
                    for sea in extra.get("value").split(", ")
                    if str(sea).isdigit()
                ]
                break
        for season in seasons:
            background_tasks.add_task(
                start_subscribe_add,
                mtype=media_type,
                tmdbid=tmdbId,
                title=subject,
                year="",
                season=season,
                username=user_name,
            )

    return schemas.Response(success=True)


@router.get(
    "/history/{mtype}", summary="查询订阅历史", response_model=List[schemas.Subscribe]
)
async def subscribe_history(
    mtype: str,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    查询电影/电视剧订阅历史
    """
    histories = await SubscribeHistory.async_list_by_type(
        db, mtype=mtype, page=page, count=count
    )
    result = []
    for history in histories:
        history_item = schemas.Subscribe.model_validate(history, from_attributes=True)
        if history_item.type == MediaType.TV.value:
            history_item.total_episode = 0
            history_item.lack_episode = 0
        result.append(history_item)
    return result


@router.delete(
    "/history/{history_id}", summary="删除订阅历史", response_model=schemas.Response
)
async def delete_subscribe_history(
    history_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    删除订阅历史
    """
    history = await SubscribeHistory.async_get(db, history_id)
    if history:
        await SubscribeHistory.async_delete(db, history_id)
    return schemas.Response(success=True)


@router.get(
    "/popular",
    summary="热门订阅（基于用户共享数据）",
    response_model=List[schemas.MediaInfo],
)
async def popular_subscribes(
    stype: str,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    min_sub: Optional[int] = None,
    genre_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    sort_type: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询热门订阅
    """
    subscribes = await MoviePilotServerHelper.async_get_subscribe_statistic(
        stype=stype,
        page=page,
        count=count,
        genre_id=genre_id,
        min_rating=min_rating,
        max_rating=max_rating,
        sort_type=sort_type,
    )
    if subscribes:
        ret_medias = []
        for sub in subscribes:
            # 订阅人数
            count = sub.get("count")
            if min_sub and count < min_sub:
                continue
            media = MediaInfo()
            media.type = MediaType(sub.get("type"))
            media.tmdb_id = sub.get("tmdbid")
            # 处理标题
            title = sub.get("name")
            season = sub.get("season")
            if season not in (None, "") and int(season) != 1 and media.tmdb_id:
                # 小写数据转大写
                season_str = cn2an.an2cn(season, "low")
                title = f"{title} 第{season_str}季"
            media.title = title
            media.year = sub.get("year")
            media.douban_id = sub.get("doubanid")
            media.bangumi_id = sub.get("bangumiid")
            media.anilist_id = sub.get("anilistid")
            media.source = sub.get("media_source")
            media.media_id = sub.get("media_id")
            media.tvdb_id = sub.get("tvdbid")
            media.imdb_id = sub.get("imdbid")
            media.season = sub.get("season")
            media.overview = sub.get("description")
            media.vote_average = sub.get("vote")
            media.poster_path = sub.get("poster")
            media.backdrop_path = sub.get("backdrop")
            media.popularity = count
            ret_medias.append(media)
        return [media.to_dict() for media in ret_medias]
    return []


@router.get(
    "/files/{subscribe_id}",
    summary="订阅相关文件信息",
    response_model=schemas.SubscrbieInfo,
)
def subscribe_files(
    subscribe_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
) -> Any:
    """
    订阅相关文件信息
    """
    subscribe = Subscribe.get(db, subscribe_id)
    if subscribe:
        return SubscribeChain().subscribe_files_info(subscribe)
    return schemas.SubscrbieInfo()


@router.post("/share", summary="分享订阅", response_model=schemas.Response)
async def subscribe_share(
    sub: schemas.SubscribeShare,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    分享订阅
    """
    subscribe = await Subscribe.async_get(db, sub.subscribe_id)
    if not subscribe:
        return schemas.Response(success=False, message="订阅不存在")
    state, errmsg = await MoviePilotServerHelper.async_sub_share(
        subscribe_id=sub.subscribe_id,
        share_title=sub.share_title,
        share_comment=sub.share_comment,
        share_user=sub.share_user,
    )
    return schemas.Response(success=state, message=errmsg)


@router.delete("/share/{share_id}", summary="删除分享", response_model=schemas.Response)
async def subscribe_share_delete(
    share_id: int, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    删除分享
    """
    state, errmsg = await MoviePilotServerHelper.async_share_delete(share_id=share_id)
    return schemas.Response(success=state, message=errmsg)


@router.post("/fork", summary="复用订阅", response_model=schemas.Response)
async def subscribe_fork(
    sub: schemas.SubscribeShare,
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    复用订阅
    """
    sub_dict = sub.model_dump()
    sub_dict.pop("id")
    for key in list(sub_dict.keys()):
        if not hasattr(schemas.Subscribe(), key):
            sub_dict.pop(key)
    result = await create_subscribe(
        subscribe_in=schemas.Subscribe(**sub_dict), current_user=current_user
    )
    if result.success:
        await MoviePilotServerHelper.async_sub_fork(share_id=sub.id)
    return result


@router.get("/follow", summary="查询已Follow的订阅分享人", response_model=List[str])
async def followed_subscribers(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询已Follow的订阅分享人
    """
    return SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []


@router.post("/follow", summary="Follow订阅分享人", response_model=schemas.Response)
async def follow_subscriber(
    share_uid: Optional[str] = None, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    Follow订阅分享人
    """
    subscribers = SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid not in subscribers:
        subscribers.append(share_uid)
        await SystemConfigOper().async_set(
            SystemConfigKey.FollowSubscribers, subscribers
        )
    return schemas.Response(success=True)


@router.delete(
    "/follow", summary="取消Follow订阅分享人", response_model=schemas.Response
)
async def unfollow_subscriber(
    share_uid: Optional[str] = None, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    取消Follow订阅分享人
    """
    subscribers = SystemConfigOper().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid in subscribers:
        subscribers.remove(share_uid)
        await SystemConfigOper().async_set(
            SystemConfigKey.FollowSubscribers, subscribers
        )
    return schemas.Response(success=True)


@router.get(
    "/shares", summary="查询分享的订阅", response_model=List[schemas.SubscribeShare]
)
async def subscribe_shares(
    name: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    genre_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    sort_type: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询分享的订阅
    """
    return await MoviePilotServerHelper.async_get_subscribe_shares(
        name=name,
        page=page,
        count=count,
        genre_id=genre_id,
        min_rating=min_rating,
        max_rating=max_rating,
        sort_type=sort_type,
    )


@router.get(
    "/share/statistics",
    summary="查询订阅分享统计",
    response_model=List[schemas.SubscribeShareStatistics],
)
async def subscribe_share_statistics(
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询订阅分享统计
    返回每个分享人分享的媒体数量以及总的复用人次
    """
    return await MoviePilotServerHelper.async_get_subscribe_share_statistics()


@router.get("/{subscribe_id}", summary="订阅详情", response_model=schemas.Subscribe)
async def read_subscribe(
    subscribe_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    根据订阅编号查询订阅信息
    """
    if not subscribe_id:
        return Subscribe()
    subscribe = await Subscribe.async_get(db, subscribe_id)
    return subscribe if subscribe else Subscribe()


@router.delete("/{subscribe_id}", summary="删除订阅", response_model=schemas.Response)
async def delete_subscribe(
    subscribe_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    删除订阅信息
    """
    subscribe = await Subscribe.async_get(db, subscribe_id)
    if subscribe:
        # 在删除之前获取订阅信息
        subscribe_info = build_subscribe_event_payload(subscribe)
        await db.delete(subscribe)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        # 发送事件
        await eventmanager.async_send_event(
            EventType.SubscribeDeleted,
            {"subscribe_id": subscribe_id, "subscribe_info": subscribe_info},
        )
        # 统计订阅
        MoviePilotServerHelper.sub_done_async(
            {
                "tmdbid": subscribe_info.get("tmdbid"),
                "doubanid": subscribe_info.get("doubanid"),
                "bangumiid": subscribe_info.get("bangumiid"),
                "anilistid": subscribe_info.get("anilistid"),
                "media_source": subscribe_info.get("media_source"),
                "media_id": subscribe_info.get("media_id"),
                "season": subscribe_info.get("season"),
            }
        )
    return schemas.Response(success=True)
