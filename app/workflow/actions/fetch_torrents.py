import random
import time
from typing import Optional, List

from pydantic import Field, field_validator

from app.workflow.actions import BaseAction
from app.workflow.incremental import build_scope_signature, filter_incremental_torrents
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import global_vars
from app.log import logger
from app.schemas import ActionParams, ActionContext, MediaType


class FetchTorrentsParams(ActionParams):
    """
    获取站点资源参数
    """
    search_type: Optional[str] = Field(default="keyword", description="搜索类型")
    name: Optional[str] = Field(default=None, description="资源名称")
    year: Optional[str] = Field(default=None, description="年份")
    type: Optional[str] = Field(default=None, description="资源类型 (电影/电视剧)")
    season: Optional[int] = Field(default=None, description="季度")
    sites: Optional[List[int]] = Field(default=[], description="站点列表")
    match_media: Optional[bool] = Field(default=False, description="匹配媒体信息")
    only_new: Optional[bool] = Field(default=False, description="增量追新模式（首次运行仅建立存量基线，后续仅处理新发布资源）")

    @field_validator("season", mode="before")
    @classmethod
    def normalize_season(cls, value):
        """允许前端数字框清空时传入空字符串。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class FetchTorrentsAction(BaseAction):
    """
    搜索站点资源
    """

    contract = {
        "inputs": [{"name": "medias", "label": "媒体", "kind": "list"}],
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
    }

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._torrents = []

    @classmethod
    @property
    def name(cls) -> str:  # noqa
        return "搜索站点资源"

    @classmethod
    @property
    def description(cls) -> str:  # noqa
        return "搜索站点种子资源列表"

    @classmethod
    @property
    def data(cls) -> dict:  # noqa
        return FetchTorrentsParams().model_dump()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        搜索站点，获取资源列表
        """
        params = FetchTorrentsParams(**params)
        searchchain = SearchChain()
        baseline_initialized = False
        baseline_count = 0
        skipped_old_count = 0

        if params.search_type == "keyword":
            # 按关键字搜索
            torrents = searchchain.search_by_title(title=params.name, sites=params.sites)
            candidates = []
            for torrent in torrents:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                if params.year and torrent.meta_info.year != params.year:
                    continue
                if params.season is not None and torrent.meta_info.begin_season != params.season:
                    continue
                candidates.append(torrent)

            if params.only_new and not global_vars.is_workflow_stopped(workflow_id):
                scope_sig = build_scope_signature(
                    "keyword",
                    params.name,
                    params.year,
                    params.type,
                    params.season,
                    sorted(params.sites or []),
                )
                candidates, baseline_initialized, count_meta = filter_incremental_torrents(
                    workflow_id=workflow_id,
                    action_id=self._action_id,
                    scope_signature=scope_sig,
                    torrents=candidates,
                    systemconfig=self.systemconfigoper,
                )
                if baseline_initialized:
                    baseline_count = count_meta
                    logger.info(
                        f"已建立追新基线（记录 {baseline_count} 条存量资源），后续仅处理新发布资源"
                    )
                else:
                    skipped_old_count = count_meta

            for torrent in candidates:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                # 识别媒体信息（在增量过滤之后执行，避免每次重复刮削存量旧种）
                if params.match_media:
                    torrent.media_info = MediaChain().recognize_by_meta(
                        torrent.meta_info,
                        obtain_images=False,
                    )
                    if not torrent.media_info:
                        logger.warning(f"{torrent.torrent_info.title} 未识别到媒体信息")
                        continue
                if params.type and torrent.media_info and torrent.media_info.type != MediaType(params.type):
                    continue
                self._torrents.append(torrent)
        else:
            # 搜索媒体列表
            raw_media_torrents = []
            for media in context.medias:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                torrents = searchchain.search_by_id(tmdbid=media.tmdb_id,
                                                    doubanid=media.douban_id,
                                                    bangumiid=media.bangumi_id,
                                                    anilistid=media.anilist_id,
                                                    source=media.source,
                                                    mediaid=media.media_id,
                                                    mtype=MediaType(media.type),
                                                    sites=params.sites)
                for torrent in torrents:
                    raw_media_torrents.append(torrent)

                # 随机休眠 5-30秒
                sleep_time = random.randint(5, 30)
                logger.info(f"随机休眠 {sleep_time} 秒 ...")
                time.sleep(sleep_time)

            if params.only_new and not global_vars.is_workflow_stopped(workflow_id):
                scope_sig = build_scope_signature("media", sorted(params.sites or []))
                raw_media_torrents, baseline_initialized, count_meta = filter_incremental_torrents(
                    workflow_id=workflow_id,
                    action_id=self._action_id,
                    scope_signature=scope_sig,
                    torrents=raw_media_torrents,
                    systemconfig=self.systemconfigoper,
                )
                if baseline_initialized:
                    baseline_count = count_meta
                else:
                    skipped_old_count = count_meta

            self._torrents.extend(raw_media_torrents)

        if self._torrents:
            context.torrents.extend(self._torrents)
            logger.info(f"共搜索到 {len(self._torrents)} 条资源")

        if baseline_initialized:
            self.job_done(f"已建立追新基线（记录 {baseline_count} 条存量资源），后续仅处理新资源")
        elif params.only_new and skipped_old_count > 0:
            self.job_done(f"搜索到 {len(self._torrents)} 个新资源（跳过 {skipped_old_count} 个已见/存量资源）")
        else:
            self.job_done(f"搜索到 {len(self._torrents)} 个资源")
        return context
