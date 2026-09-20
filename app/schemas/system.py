import re
from dataclasses import dataclass
from typing import Optional, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


@dataclass
class ServiceInfo:
    """
    封装服务相关信息的数据类
    """

    # 名称
    name: Optional[str] = None
    # 实例
    instance: Optional[Any] = None
    # 模块
    module: Optional[Any] = None
    # 类型
    type: Optional[str] = None
    # 配置
    config: Optional[Any] = None


class MediaServerConf(BaseModel):
    """
    媒体服务器配置
    """

    # 名称
    name: Optional[str] = None
    # 类型 emby/zspace/jellyfin/plex/trimemedia/ugreen/mediavault
    type: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False
    # 同步媒体体库列表
    sync_libraries: Optional[list] = Field(default_factory=list)
    # 自动同步间隔（小时），未设置时使用旧全局配置
    sync_interval: Optional[int] = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("sync_interval", mode="before")
    @classmethod
    def validate_sync_interval(cls, value: Any) -> Optional[int]:
        """
        兼容前端清空输入框后残留的空字符串等非法值，避免历史配置导致模块初始化失败

        :param value: 原始配置值
        :return: 合法的间隔小时数，无法解析时返回 None
        """
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class DownloaderConf(BaseModel):
    """
    下载器配置
    """

    # 名称
    name: Optional[str] = None
    # 类型 qbittorrent/transmission/rtorrent
    type: Optional[str] = None
    # 是否默认
    default: Optional[bool] = False
    # 是否 BT(公开)站点默认
    bt_default: Optional[bool] = False
    # BT(公开)站点默认下载路径,仅经 BT 默认选中该下载器时生效
    bt_save_path: Optional[str] = None
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False
    # 路径映射
    path_mapping: Optional[list[tuple[str, str]]] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class TelegramNotificationConfig(BaseModel):
    """Telegram 通知渠道参数。"""

    TELEGRAM_TOKEN: str = Field(min_length=1)
    TELEGRAM_CHAT_ID: str = Field(min_length=1)
    TELEGRAM_USERS: Optional[str] = None
    TELEGRAM_ADMINS: Optional[str] = None
    API_URL: Optional[str] = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WechatNotificationConfig(BaseModel):
    """企业微信应用或智能机器人参数。"""

    WECHAT_MODE: Literal["app", "bot"] = "app"
    WECHAT_CORPID: Optional[str] = None
    WECHAT_APP_ID: Optional[str] = None
    WECHAT_APP_SECRET: Optional[str] = None
    WECHAT_PROXY: Optional[str] = None
    WECHAT_TOKEN: Optional[str] = None
    WECHAT_ENCODING_AESKEY: Optional[str] = None
    WECHAT_BOT_ID: Optional[str] = None
    WECHAT_BOT_SECRET: Optional[str] = None
    WECHAT_BOT_CHAT_ID: Optional[str] = None
    WECHAT_BOT_WS_URL: Optional[str] = None
    WECHAT_ADMINS: Optional[str] = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_mode_credentials(self) -> "WechatNotificationConfig":
        required = (
            ("WECHAT_BOT_ID", "WECHAT_BOT_SECRET")
            if self.WECHAT_MODE == "bot"
            else ("WECHAT_CORPID", "WECHAT_APP_ID", "WECHAT_APP_SECRET")
        )
        missing = [field for field in required if not getattr(self, field)]
        if missing:
            raise ValueError(f"企业微信 {self.WECHAT_MODE} 模式缺少配置：{', '.join(missing)}")
        return self


class NotificationConf(BaseModel):
    """可直接运行的通知渠道配置。"""

    id: Optional[str] = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    type: Literal["telegram", "wechat"]
    config: dict = Field(default_factory=dict)
    switchs: list[Literal[
        "资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它"
    ]] = Field(default_factory=list)
    enabled: bool = False

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_channel_config(self) -> "NotificationConf":
        config_type = TelegramNotificationConfig if self.type == "telegram" else WechatNotificationConfig
        self.config = config_type.model_validate(self.config).model_dump(exclude_none=True)
        return self


class NotificationSwitchConf(BaseModel):
    """通知场景的接收范围。"""

    type: Literal["资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它"]
    action: Literal["all", "user", "admin", "user,admin"] = "all"

    model_config = ConfigDict(extra="forbid")


class NotificationTimePeriod(BaseModel):
    """允许发送通知的每日时间段。"""

    start: str
    end: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("start", "end")
    @classmethod
    def validate_time(cls, value: str) -> str:
        """校验 24 小时时间并将历史秒级值规范化为 HH:MM。"""
        match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", value)
        if not match:
            raise ValueError("时间必须使用 HH:MM 格式")
        hour, minute, second = (int(part) if part is not None else 0 for part in match.groups())
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("时间必须在 00:00 到 23:59 之间")
        return f"{hour:02d}:{minute:02d}"


class PluginMarketSyncRequest(BaseModel):
    """
    插件市场仓库同步请求
    """

    # Wiki 插件文档 Markdown 原始文件地址
    wiki_url: Optional[str] = Field(
        default="https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md",
    )


class StorageConf(BaseModel):
    """
    存储配置
    """

    # 类型 local/alipan/u115/rclone/alist
    type: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class TransferDirectoryConf(BaseModel):
    """
    文件整理目录配置
    """

    # 名称
    name: Optional[str] = None
    # 优先级
    priority: Optional[int] = 0
    # 存储
    storage: Optional[str] = None
    # 下载目录
    download_path: Optional[str] = None
    # 适用媒体类型
    media_type: Optional[str] = None
    # 适用媒体类别
    media_category: Optional[str] = None
    # 下载类型子目录
    download_type_folder: Optional[bool] = False
    # 下载类别子目录
    download_category_folder: Optional[bool] = False
    # 监控方式 downloader/monitor，None为不监控
    monitor_type: Optional[str] = None
    # 监控模式 fast / compatibility
    monitor_mode: Optional[str] = "fast"
    # 整理方式 move/copy/link/softlink
    transfer_type: Optional[str] = None
    # 文件覆盖模式 always/size/never/latest
    overwrite_mode: Optional[str] = None
    # 整理到媒体库目录
    library_path: Optional[str] = None
    # 媒体库目录存储
    library_storage: Optional[str] = None
    # 智能重命名
    renaming: Optional[bool] = False
    # 刮削
    scraping: Optional[bool] = False
    # 是否发送通知
    notify: Optional[bool] = True
    # 媒体库类型子目录
    library_type_folder: Optional[bool] = False
    # 媒体库类别子目录
    library_category_folder: Optional[bool] = False
    model_config = ConfigDict(extra="forbid")
