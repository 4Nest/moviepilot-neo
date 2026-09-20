from typing import Optional, List, Dict, Any, ClassVar

from pydantic import BaseModel, Field, ConfigDict, model_validator

from app.schemas.types import MediaType


def compute_subscribe_completed_episode(subscribe: "Subscribe") -> Optional[int]:
    """
    计算订阅"已完成"集数派生值，仅用于响应填充，不入库。

    普通电视剧按 ``total_episode - lack_episode`` 计算；分集洗版按订阅目标范围内
    priority==100 的分集数量计算；全集洗版按整包准入基线是否达到 100 计算。
    """
    total_episode = subscribe.total_episode or 0
    if subscribe.type != MediaType.TV.value or not total_episode:
        return None

    start_episode = subscribe.start_episode or 1
    version_rules = getattr(subscribe, "version_rules", None) or []
    if version_rules:
        # 多版本订阅:父行 lack_episode 不承载事实(停留在初始值),下载事实在各版本
        # version_progress.note。已完成集数取启用版本 note 与目标范围的并集大小。
        target = set(range(start_episode, total_episode + 1))
        downloaded: set = set()
        progress = subscribe.version_progress or {}
        for rule in version_rules:
            if isinstance(rule, dict) and not rule.get("enabled", True):
                continue
            rule_id = rule.get("id") if isinstance(rule, dict) else getattr(rule, "id", None)
            entry = progress.get(str(rule_id))
            note = entry.get("note") if isinstance(entry, dict) else getattr(entry, "note", None)
            for episode in note or []:
                try:
                    episode_number = int(episode)
                except (TypeError, ValueError):
                    continue
                if episode_number in target:
                    downloaded.add(episode_number)
        return len(downloaded)
    if not subscribe.best_version:
        lack = subscribe.lack_episode or 0
        return max(total_episode - lack, 0)

    if subscribe.best_version_full:
        completed_targets = max(total_episode - start_episode + 1, 0) \
            if subscribe.current_priority == 100 else 0
        return min(min(max(start_episode - 1, 0), total_episode) + completed_targets, total_episode)

    episode_priority = subscribe.episode_priority or {}
    if not episode_priority and subscribe.current_priority is not None:
        # 兼容只有整体优先级的洗版快照，响应派生值需与链路侧按集口径保持一致。
        episode_priority = {
            str(episode): int(subscribe.current_priority)
            for episode in range(start_episode, total_episode + 1)
        }
    priority_completed = sum(
        1
        for ep_key, priority in episode_priority.items()
        if str(ep_key).isdigit()
        and start_episode <= int(ep_key) <= total_episode
        and priority == 100
    )
    return min(max(start_episode - 1, 0), total_episode) + priority_completed


class SubscribeVersionSettings(BaseModel):
    """版本子订阅的完整、独立设置快照。"""

    keyword: Optional[str] = None
    filter: Optional[str] = None
    include: Optional[str] = None
    exclude: Optional[str] = None
    quality: Optional[str] = None
    resolution: Optional[str] = None
    effect: Optional[str] = None
    total_episode: Optional[int] = 0
    start_episode: Optional[int] = 0
    sites: List[int] = Field(default_factory=list)
    downloader: Optional[str] = None
    best_version: Optional[int] = None
    best_version_full: Optional[int] = None
    save_path: Optional[str] = None
    search_imdbid: Optional[int] = 0
    manual_total_episode: Optional[int] = 0
    custom_words: Optional[str] = None
    media_category: Optional[str] = None
    filter_groups: List[str] = Field(default_factory=list)
    episode_group: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class SubscribeVersionRule(BaseModel):
    """一个具名版本及其独立匹配设置。"""

    id: str
    name: str
    enabled: bool = True
    release_group: Optional[str] = None
    settings: SubscribeVersionSettings

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_release_group(self) -> "SubscribeVersionRule":
        if self.release_group:
            import re
            re.compile(self.release_group)
        return self



class SubscribeDecisionSummary(BaseModel):
    """最近一次订阅搜索的结构化判定结果。"""

    target: Optional[str] = None
    searched: int = 0
    matched: int = 0
    downloaded: int = 0
    result: str
    reason: Optional[str] = None
    updated_at: str

class SubscribeVersionProgress(BaseModel):
    """版本运行事实；只读输入，不允许公共写接口覆盖。"""

    state: Optional[str] = None
    last_update: Optional[str] = None
    lack_episode: Optional[int] = None
    note: Any = None
    current_priority: Optional[int] = None
    episode_priority: Dict[str, int] = Field(default_factory=dict)
    completed: bool = False
    decision_summary: Optional[SubscribeDecisionSummary] = None


class Subscribe(BaseModel):
    # 公共创建和更新接口不得接收系统字段和运行事实；其余字段默认作为订阅输入透传。
    PUBLIC_WRITE_EXCLUDED_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "version_progress", "version_mode", "legacy_version_id", "decision_summary",
        "id", "poster", "backdrop", "vote", "description", "lack_episode", "completed_episode",
        "note", "state", "last_update", "username", "current_priority", "episode_priority", "date",
    })

    id: Optional[int] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    tmdbid: Optional[int] = None
    doubanid: Optional[str] = None
    bangumiid: Optional[int] = None
    anilistid: Optional[int] = None
    mediaid: Optional[str] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 过滤规则
    filter: Optional[str] = None
    # 多版本规则由客户端完整提交；进度仅由服务端运行链路写入
    version_rules: Optional[List[SubscribeVersionRule]] = None
    version_mode: Optional[str] = None
    version_progress: Optional[Dict[str, SubscribeVersionProgress]] = None
    decision_summary: Optional[SubscribeDecisionSummary] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集数
    start_episode: Optional[int] = 0
    # 缺失集数
    lack_episode: Optional[int] = 0
    # 已完成集数
    completed_episode: Optional[int] = None
    # 附加信息
    note: Optional[Any] = None
    # 状态：N-新建， R-订阅中
    state: Optional[str] = None
    # 最后更新时间
    last_update: Optional[str] = None
    # 订阅用户
    username: Optional[str] = None
    # 订阅站点
    sites: Optional[List[int]] = Field(default_factory=list)
    # 下载器
    downloader: Optional[str] = None
    # 是否洗版
    best_version: Optional[int] = None
    # 是否只洗全集整包
    best_version_full: Optional[int] = None
    # 当前优先级
    current_priority: Optional[int] = None
    # 洗版时已下载剧集的优先级状态
    episode_priority: Optional[Dict[str, int]] = None
    # 保存路径
    save_path: Optional[str] = None
    # 是否使用 imdbid 搜索
    search_imdbid: Optional[int] = 0
    # 是否跳过媒体库存在检测 0否 1是
    skip_library_check: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 过滤规则组
    filter_groups: Optional[List[str]] = Field(default_factory=list)
    # 剧集组
    legacy_version_id: Optional[str] = None
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _fill_completed_episode(self) -> "Subscribe":
        """填充电视剧订阅的派生完成集数。"""
        if self.completed_episode is None:
            self.completed_episode = compute_subscribe_completed_episode(self)
        return self

    @model_validator(mode="after")
    def _validate_version_rules(self) -> "Subscribe":
        if self.version_mode not in (None, "any", "all"):
            raise ValueError("version_mode must be any or all")
        if self.version_rules is not None:
            ids = [rule.id for rule in self.version_rules]
            if len(ids) != len(set(ids)):
                raise ValueError("version rule IDs must be unique")
            if self.version_rules and self.version_mode not in (None, "all"):
                raise ValueError("structured version rules require all mode")
        return self

    def to_public_write_payload(self) -> Dict[str, Any]:
        """裁剪公共订阅写入字段，避免请求体覆盖下载事实和运行状态。"""
        return self.model_dump(exclude=self.PUBLIC_WRITE_EXCLUDED_FIELDS)


class SubscribeShare(BaseModel):
    # 分享ID
    id: Optional[int] = None
    # 订阅ID
    subscribe_id: Optional[int] = None
    # 分享标题
    share_title: Optional[str] = None
    # 分享说明
    share_comment: Optional[str] = None
    # 分享人
    share_user: Optional[str] = None
    # 分享人唯一ID
    share_uid: Optional[str] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    tmdbid: Optional[int] = None
    doubanid: Optional[str] = None
    bangumiid: Optional[int] = None
    anilistid: Optional[int] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None
    # 复用人次
    count: Optional[int] = 0


class SubscribeShareStatistics(BaseModel):
    # 分享人
    share_user: Optional[str] = None
    # 分享数量
    share_count: Optional[int] = 0
    # 总复用人次
    total_reuse_count: Optional[int] = 0


class SubscribeDownloadFileInfo(BaseModel):
    # 种子名称
    torrent_title: Optional[str] = None
    # 站点名称
    site_name: Optional[str] = None
    # 下载器
    downloader: Optional[str] = None
    # hash
    hash: Optional[str] = None
    # 文件路径
    file_path: Optional[str] = None


class SubscribeLibraryFileInfo(BaseModel):
    # 存储
    storage: Optional[str] = "local"
    # 文件路径
    file_path: Optional[str] = None
    # 媒体服务器名称
    server: Optional[str] = None
    # 媒体服务器类型：emby、jellyfin、plex 等
    server_type: Optional[str] = None
    # 媒体服务器条目 ID
    itemid: Optional[str] = None


class SubscribeEpisodeInfo(BaseModel):
    # 标题
    title: Optional[str] = None
    # 描述
    description: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 下载文件信息
    download: Optional[List[SubscribeDownloadFileInfo]] = Field(default_factory=list)
    # 媒体库文件信息
    library: Optional[List[SubscribeLibraryFileInfo]] = Field(default_factory=list)


class SubscrbieInfo(BaseModel):
    # 订阅信息
    subscribe: Optional[Subscribe] = None
    # 集信息 {集号: {download: 文件路径，library: 文件路径, backdrop: url, title: 标题, description: 描述}}
    episodes: Optional[Dict[int, SubscribeEpisodeInfo]] = Field(default_factory=dict)
