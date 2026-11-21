from typing import Optional

from pydantic import Field

from app.workflow.actions import BaseAction
from app.workflow.incremental import build_scope_signature, filter_incremental_torrents
from app.chain.media import MediaChain
from app.core.config import settings, global_vars
from app.core.context import Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.schemas import ActionParams, ActionContext


class FetchRssParams(ActionParams):
    """
    获取RSS资源列表参数
    """
    url: str = Field(default=None, description="RSS地址")
    proxy: Optional[bool] = Field(default=False, description="是否使用代理")
    timeout: Optional[int] = Field(default=15, description="超时时间")
    content_type: Optional[str] = Field(default=None, description="Content-Type")
    referer: Optional[str] = Field(default=None, description="Referer")
    ua: Optional[str] = Field(default=None, description="User-Agent")
    match_media: Optional[bool] = Field(default=False, description="匹配媒体信息")
    only_new: Optional[bool] = Field(default=False, description="增量追新模式（首次运行仅建立存量基线，后续仅处理新发布资源）")


class FetchRssAction(BaseAction):
    """
    获取RSS资源列表
    """

    contract = {
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
    }

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._rss_torrents = []
        self._has_error = False

    @classmethod
    @property
    def name(cls) -> str:  # noqa
        return "获取RSS资源"

    @classmethod
    @property
    def description(cls) -> str:  # noqa
        return "订阅RSS地址获取资源"

    @classmethod
    @property
    def data(cls) -> dict:  # noqa
        return FetchRssParams().model_dump()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        请求RSS地址获取数据，并解析为资源列表
        """
        params = FetchRssParams(**params)
        if not params.url:
            return context

        headers = {}
        if params.content_type:
            headers["Content-Type"] = params.content_type
        if params.referer:
            headers["Referer"] = params.referer
        if params.ua:
            headers["User-Agent"] = params.ua

        rss_items = RssHelper().parse(url=params.url,
                                      proxy=settings.PROXY if params.proxy else None,
                                      timeout=params.timeout,
                                      headers=headers)
        if rss_items is None or rss_items is False:
            logger.error(f'RSS地址 {params.url} 请求失败！')
            self._has_error = True
            return context

        if not rss_items:
            logger.error(f'RSS地址 {params.url} 未获取到RSS数据！')
            return context

        # 先组装候选种子，开启增量追新时在媒体识别前过滤已见存量资源
        candidates = []
        for item in rss_items:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if not item.get("title"):
                continue
            torrentinfo = TorrentInfo(
                title=item.get("title"),
                description=item.get("description"),
                site_proxy=bool(params.proxy),
                enclosure=item.get("enclosure"),
                page_url=item.get("link"),
                size=item.get("size"),
                pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
            )
            meta = MetaInfo(title=torrentinfo.title, subtitle=torrentinfo.description)
            candidates.append(Context(meta_info=meta, media_info=None, torrent_info=torrentinfo))

        baseline_initialized = False
        baseline_count = 0
        skipped_old_count = 0
        if params.only_new and not global_vars.is_workflow_stopped(workflow_id):
            scope_sig = build_scope_signature("rss", params.url)
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
                    f"已建立RSS追新基线（记录 {baseline_count} 条存量资源），后续仅处理新发布资源"
                )
            else:
                skipped_old_count = count_meta

        for candidate in candidates:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if params.match_media:
                mediainfo = MediaChain().recognize_by_meta(
                    candidate.meta_info,
                    obtain_images=False,
                )
                if not mediainfo:
                    logger.warning(f"{candidate.torrent_info.title} 未识别到媒体信息")
                    continue
                candidate.media_info = mediainfo
            self._rss_torrents.append(candidate)

        if self._rss_torrents:
            logger.info(f"获取到 {len(self._rss_torrents)} 个RSS资源")
            context.torrents.extend(self._rss_torrents)

        if baseline_initialized:
            self.job_done(f"已建立追新基线（记录 {baseline_count} 条存量资源），后续仅处理新资源")
        elif params.only_new and skipped_old_count > 0:
            self.job_done(f"获取到 {len(self._rss_torrents)} 个新资源（跳过 {skipped_old_count} 个已见/存量资源）")
        else:
            self.job_done(f"获取到 {len(self._rss_torrents)} 个资源")
        return context
