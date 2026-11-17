from datetime import datetime, timedelta
from typing import Optional, List

from pydantic import Field, field_validator

from app.workflow.actions import BaseAction, ActionChain
from app.workflow.incremental import parse_torrent_pubdate
from app.core.config import global_vars
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import ActionParams, ActionContext


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    rule_groups: Optional[List[str]] = Field(default=[], description="规则组")
    quality: Optional[str] = Field(default=None, description="资源质量")
    resolution: Optional[str] = Field(default=None, description="资源分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    include: Optional[str] = Field(default=None, description="包含规则")
    exclude: Optional[str] = Field(default=None, description="排除规则")
    size: Optional[str] = Field(default=None, description="资源大小范围（MB）")
    max_age_hours: Optional[float] = Field(
        default=None,
        description="仅保留最近 N 小时内发布的资源（留空或 <=0 表示不限制）",
    )

    @field_validator("max_age_hours", mode="before")
    @classmethod
    def normalize_max_age_hours(cls, value):
        """将空字符串或非正数规范化为 None（不限制发布时间）。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None


class FilterTorrentsAction(BaseAction):
    """
    过滤资源数据
    """

    contract = {
        "inputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list", "merge": "replace"}],
    }

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._torrents = []

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "过滤资源"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "对资源列表数据进行过滤"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return FilterTorrentsParams().model_dump()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        过滤torrents中的资源
        """
        params = FilterTorrentsParams(**params)
        min_pub_dt = (
            datetime.now() - timedelta(hours=params.max_age_hours)
            if params.max_age_hours
            else None
        )
        for torrent in context.torrents:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if min_pub_dt is not None:
                pub_dt = parse_torrent_pubdate(getattr(torrent.torrent_info, "pubdate", None))
                if pub_dt is None:
                    logger.info(
                        f"{torrent.torrent_info.title} 缺少有效发布时间，不满足最近 {params.max_age_hours} 小时限制，跳过"
                    )
                    continue
                if pub_dt < min_pub_dt:
                    logger.info(
                        f"{torrent.torrent_info.title} 发布于 {torrent.torrent_info.pubdate}，早于最近 {params.max_age_hours} 小时窗口，跳过"
                    )
                    continue
            if TorrentHelper().filter_torrent(
                    torrent_info=torrent.torrent_info,
                    filter_params={
                        "quality": params.quality,
                        "resolution": params.resolution,
                        "effect": params.effect,
                        "include": params.include,
                        "exclude": params.exclude,
                        "size": params.size
                    }
            ):
                if ActionChain().filter_torrents(
                        rule_groups=params.rule_groups,
                        torrent_list=[torrent.torrent_info],
                        mediainfo=torrent.media_info
                ):
                    self._torrents.append(torrent)

        logger.info(f"过滤后剩余 {len(self._torrents)} 个资源")

        context.torrents = self._torrents

        self.job_done(f"过滤后剩余 {len(self._torrents)} 个资源")
        return context
