import base64
import re
from pathlib import Path
from typing import Set, Tuple, Optional, Union, List, Dict
from urllib.parse import parse_qs, urlparse

from torrentool.torrent import Torrent

from app import schemas
from app.core.cache import FileCache
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.helper.torrent import EpisodeSelection, select_torrent_files
from app.log import logger
from app.modules import _ModuleBase, _DownloaderBase
from app.modules.rtorrent.rtorrent import Rtorrent
from app.schemas import DownloaderTorrent
from app.schemas.types import (
    DownloadTaskState,
    DownloaderType,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)
from app.utils.string import StringUtils

_MAGNET_BTIH_HEX_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40})$", re.IGNORECASE)
_MAGNET_BTIH_B32_RE = re.compile(r"^urn:btih:([A-Za-z2-7]{32})$", re.IGNORECASE)
_MAGNET_BTMH_SHA256_RE = re.compile(r"^urn:btmh:1220([0-9a-fA-F]{64})$", re.IGNORECASE)


def _extract_expected_torrent_hash(
    torrent_from_file: Optional[Torrent],
    content: Optional[Union[str, bytes]],
) -> Optional[str]:
    """
    从已解析的种子对象或磁力链接中提前提取 40 位小写十六进制 InfoHash。
    """
    info_hash = getattr(torrent_from_file, "info_hash", None)
    if isinstance(info_hash, str):
        normalized = info_hash.strip().lower()
        if normalized:
            return normalized

    if isinstance(content, bytes):
        magnet_text = content.decode("utf-8", errors="ignore")
    elif isinstance(content, str):
        magnet_text = content
    else:
        return None

    if not magnet_text.lower().startswith("magnet:"):
        return None

    xt_values: List[str] = []
    for key, values in parse_qs(urlparse(magnet_text).query).items():
        if key.lower() == "xt":
            xt_values.extend(value.strip() for value in values if value and value.strip())

    for value in xt_values:
        match = _MAGNET_BTIH_HEX_RE.match(value)
        if match:
            return match.group(1).lower()

    for value in xt_values:
        match = _MAGNET_BTIH_B32_RE.match(value)
        if match:
            try:
                return base64.b32decode(match.group(1), casefold=True).hex()
            except Exception:
                continue

    for value in xt_values:
        match = _MAGNET_BTMH_SHA256_RE.match(value)
        if match:
            return match.group(1)[:40].lower()

    return None


class RtorrentModule(_ModuleBase, _DownloaderBase[Rtorrent]):
    """
    rTorrent 下载器模块，负责下载任务添加、文件选集和任务管理。
    """

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=Rtorrent.__name__.lower(), service_type=Rtorrent
        )

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        return "Rtorrent"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型
        """
        return DownloaderType.Rtorrent

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    def stop(self):
        """
        停止模块
        """
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            if server.is_inactive():
                server.reconnect()
            if not server.transfer_info():
                return False, f"无法连接rTorrent下载器：{name}"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        返回控制模块启用状态的配置项
        """
        pass

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"rTorrent下载器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    @staticmethod
    def _append_episode_files(
        server: Rtorrent,
        torrent_hash: str,
        episodes: Set[int],
        custom_words: Optional[str] = None,
        episode_selection: Optional[EpisodeSelection] = None,
        torrent_files: Optional[List[Dict]] = None,
    ) -> Tuple[bool, str]:
        """
        为已有任务追加启用目标集数对应的文件，不取消已选中的其他文件，也不暂停或删除任务。
        """
        if torrent_files is None:
            torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return False, f"追加选集失败，已有任务 {torrent_hash} 获取种子文件失败"
        media_extensions = {
            str(extension).lower()
            for extension in getattr(settings, "RMT_MEDIAEXT", [])
        }
        file_entries = []
        file_priorities = {}
        try:
            for idx, item in enumerate(torrent_files):
                file_id = item.get("id")
                if file_id is None:
                    file_id = idx
                file_entries.append((file_id, item.get("name")))
                file_priorities[file_id] = item.get("priority")
            wanted, _unwanted, selected_episodes = select_torrent_files(
                file_entries,
                set(episodes or set()),
                media_extensions,
                custom_words,
                episode_selection,
            )
        finally:
            torrent_files.clear()
        if not wanted or not episodes or not set(episodes).issubset(selected_episodes):
            return False, (
                f"追加选集失败，已有任务 {torrent_hash} 未找到目标集数 "
                f"{sorted(set(episodes or set()) - selected_episodes)}，请检查季集映射"
            )
        to_enable = [
            file_id for file_id in wanted
            if file_priorities.get(file_id) in (None, 0)
        ]
        if not to_enable:
            return True, "下载任务已存在"
        if not server.set_files(
            torrent_hash=torrent_hash, file_ids=to_enable, priority=1
        ):
            return False, f"追加选集失败，已有任务 {torrent_hash} 设置选集优先级失败"
        if not server.start_torrents(torrent_hash):
            return False, f"追加选集失败，已有任务 {torrent_hash} 启动任务未确认，未记录成功"
        return True, f"下载任务已存在，已追加选择集数：{sorted(selected_episodes)}"

    def _handle_existing_torrent(
        self,
        server: Rtorrent,
        torrent_hash: str,
        torrent_tags: Optional[List[str]],
        torrent_layout: str,
        downloader: Optional[str] = None,
        episodes: Optional[Set[int]] = None,
        custom_words: Optional[str] = None,
        episode_selection: Optional[EpisodeSelection] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        处理 rTorrent 中已存在的任务：按需追加选集，成功后同步标签并返回任务信息。
        """
        if episodes:
            appended, message = self._append_episode_files(
                server, torrent_hash, episodes, custom_words, episode_selection,
            )
            if not appended:
                return None, None, None, message
        else:
            message = "下载任务已存在"
        if torrent_tags is not None:
            if "已整理" in torrent_tags:
                server.remove_torrents_tag(ids=torrent_hash, tag=["已整理"])
            if settings.TORRENT_TAG and settings.TORRENT_TAG not in torrent_tags:
                logger.info(f"给种子 {torrent_hash} 打上标签：{settings.TORRENT_TAG}")
                server.set_torrents_tag(ids=torrent_hash, tags=[settings.TORRENT_TAG])
        return (
            downloader or self.get_default_config_name(),
            torrent_hash,
            torrent_layout,
            message,
        )

    @staticmethod
    def _select_episode_files(
        server: Rtorrent,
        torrent_hash: str,
        episodes: Set[int],
        custom_words: Optional[str] = None,
        episode_selection: Optional[EpisodeSelection] = None,
    ) -> Tuple[bool, str, bool]:
        """
        对暂停添加的任务完成选集，所有 RPC 均成功才允许下载链确认任务。
        若文件列表已存在优先级为 0 的跳过文件，判定为已有选集任务并自动切换为追加选集模式。
        返回：(是否成功, 提示信息, 是否为已有任务)。
        """
        torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return False, "获取种子文件失败，下载任务保留暂停状态，请检查下载器", False
        if any(item.get("priority") == 0 for item in torrent_files):
            appended, message = RtorrentModule._append_episode_files(
                server,
                torrent_hash,
                episodes,
                custom_words,
                episode_selection,
                torrent_files=torrent_files,
            )
            return appended, message, True
        media_extensions = {
            str(extension).lower()
            for extension in getattr(settings, "RMT_MEDIAEXT", [])
        }
        try:
            wanted, unwanted, selected_episodes = select_torrent_files(
                ((item.get("id"), item.get("name")) for item in torrent_files),
                set(episodes or set()),
                media_extensions,
                custom_words,
                episode_selection,
            )
        finally:
            torrent_files.clear()
        if not wanted or not episodes or not set(episodes).issubset(selected_episodes):
            return False, "未匹配到目标集数，请检查季集映射，下载任务保留暂停状态", False
        if unwanted and not server.set_files(
            torrent_hash=torrent_hash, file_ids=unwanted, priority=0
        ):
            return False, "设置选集优先级失败，下载任务保留暂停状态", False
        if not server.set_files(
            torrent_hash=torrent_hash, file_ids=wanted, priority=1
        ):
            return False, "设置选集优先级失败，下载任务保留暂停状态", False
        if not server.start_torrents(torrent_hash):
            server.stop_torrents(torrent_hash)
            return False, "启动任务未确认，未记录成功", False
        return True, f"添加下载成功，已选择集数：{sorted(selected_episodes)}", False

    def download(
        self,
        content: Union[Path, str, bytes],
        download_dir: Path,
        cookie: str,
        episodes: Set[int] = None,
        category: Optional[str] = None,
        label: Optional[str] = None,
        downloader: Optional[str] = None,
        custom_words: Optional[str] = None,
        episode_selection: Optional[EpisodeSelection] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务。已有任务在传入 episodes 时按目标追加启用未下载集数文件，不取消已有文件。

        :param content: 种子文件地址或者磁力链接或种子内容
        :param download_dir: 下载目录
        :param cookie: 站点 Cookie
        :param episodes: 需要下载的集数
        :param category: 分类，rTorrent 中未使用
        :param label: 标签
        :param downloader: 下载器名称
        :param custom_words: 来源识别词，按行参与文件名识别
        :param episode_selection: 目标季与已确认的季集对应关系；新增任务选集失败清理本次任务并保留文件
        :return: 下载器名称、种子 Hash、种子文件布局、错误原因
        """

        def __get_torrent_info() -> Tuple[Optional[Torrent], Optional[bytes]]:
            """
            获取种子名称
            """
            torrent_info, torrent_content = None, None
            try:
                if isinstance(content, Path):
                    if content.exists():
                        torrent_content = content.read_bytes()
                    else:
                        torrent_content = FileCache().get(
                            content.as_posix(), region="torrents"
                        )
                else:
                    torrent_content = content

                if torrent_content:
                    if StringUtils.is_magnet_link(torrent_content):
                        return None, torrent_content
                    else:
                        torrent_info = Torrent.from_string(torrent_content)

                return torrent_info, torrent_content
            except Exception as e:
                logger.error(f"获取种子名称失败：{e}")
                return None, None

        if not content:
            return None, None, None, "下载内容为空"

        # 读取种子的名称
        torrent_from_file, content = __get_torrent_info()
        # 检查是否为磁力链接
        is_magnet = (
            isinstance(content, str)
            and content.startswith("magnet:")
            or isinstance(content, bytes)
            and content.startswith(b"magnet:")
        )
        if not torrent_from_file and not is_magnet:
            return None, None, None, f"添加种子任务失败：无法读取种子文件"

        # 获取下载器
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None

        expected_hash = _extract_expected_torrent_hash(torrent_from_file, content)
        # 仅当无法预知种子 Hash 时才生成随机临时标签，避免 rTorrent 残留无意义标签
        temp_tag = None if expected_hash else StringUtils.generate_random_str(10)
        if label:
            tags = [t.strip() for t in label.split(",") if t.strip()]
            if temp_tag:
                tags.append(temp_tag)
        elif settings.TORRENT_TAG:
            tags = [temp_tag, settings.TORRENT_TAG] if temp_tag else [settings.TORRENT_TAG]
        else:
            tags = [temp_tag] if temp_tag else []
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 添加任务
        state = server.add_torrent(
            content=content,
            download_dir=self.normalize_path(download_dir, downloader),
            is_paused=is_paused,
            tags=tags or None,
            cookie=cookie,
        )

        # rTorrent 始终使用原始种子布局
        torrent_layout = "Original"

        if not state:
            # 查询所有下载器的种子
            torrents, error = server.get_torrents()
            if error:
                return None, None, None, "无法连接rTorrent下载器"
            if torrents:
                try:
                    for torrent in torrents:
                        # 优先按 InfoHash 匹配，其次按名称与大小相等判定为同一个种子
                        torrent_hash = torrent.get("hash")
                        is_same_torrent = (
                            expected_hash
                            and str(torrent_hash or "").strip().lower() == expected_hash
                        ) or (
                            torrent_from_file is not None
                            and torrent.get("name") == getattr(torrent_from_file, "name", "")
                            and torrent.get("total_size") == getattr(torrent_from_file, "total_size", 0)
                        )
                        if is_same_torrent:
                            torrent_tags = [
                                str(t).strip()
                                for t in (torrent.get("tags") or "").split(",")
                                if t.strip()
                            ]
                            logger.warn(
                                f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}"
                            )
                            return self._handle_existing_torrent(
                                server=server,
                                torrent_hash=torrent_hash,
                                torrent_tags=torrent_tags,
                                torrent_layout=torrent_layout,
                                downloader=downloader,
                                episodes=episodes,
                                custom_words=custom_words,
                                episode_selection=episode_selection,
                            )
                finally:
                    torrents.clear()
                    del torrents
            return None, None, None, f"添加种子任务失败：{content}"
        else:
            # 获取种子Hash
            if expected_hash:
                torrent_hash = expected_hash
            else:
                torrent_hash = server.get_torrent_id_by_tag(tags=temp_tag)
            if not torrent_hash:
                if torrent_from_file:
                    torrents, error = server.get_torrents()
                    if not error and torrents:
                        try:
                            for torrent in torrents:
                                if torrent.get("name") == getattr(
                                    torrent_from_file, "name", ""
                                ) and torrent.get("total_size") == getattr(
                                    torrent_from_file, "total_size", 0
                                ):
                                    existing_hash = torrent.get("hash")
                                    logger.warn(
                                        f"下载器中已存在该种子任务：{existing_hash} - {torrent.get('name')}"
                                    )
                                    return self._handle_existing_torrent(
                                        server=server,
                                        torrent_hash=existing_hash,
                                        torrent_tags=None,
                                        torrent_layout=torrent_layout,
                                        downloader=downloader,
                                        episodes=episodes,
                                        custom_words=custom_words,
                                        episode_selection=episode_selection,
                                    )
                        finally:
                            torrents.clear()
                            del torrents
                return (
                    None,
                    None,
                    None,
                    f"下载任务添加成功，但获取rTorrent任务信息失败：{content}",
                )
            else:
                if is_paused:
                    selected, message, is_existing = self._select_episode_files(
                        server,
                        torrent_hash,
                        episodes,
                        custom_words,
                        episode_selection,
                    )
                    if not selected:
                        if is_existing:
                            return None, None, None, message
                        return (
                            None,
                            None,
                            None,
                            self._discard_failed_selection(server, torrent_hash, message),
                        )
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        message,
                    )
                else:
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        "添加下载成功",
                    )

    def list_torrents(
        self,
        status: TorrentStatus = None,
        hashs: Union[list, str] = None,
        downloader: Optional[str] = None,
        include_all_tags: bool = False,
    ) -> Optional[List[DownloaderTorrent]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :param include_all_tags:  是否包含未打内置标签的下载任务
        :return: 下载器中符合状态的种子列表
        """
        # 获取下载器
        if downloader:
            server: Rtorrent = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers: Dict[str, Rtorrent] = self.get_instances()
        ret_torrents = []
        query_status = self.__normalize_query_status(status)
        query_tags = None if include_all_tags else settings.TORRENT_TAG

        def __get_torrent_path(torrent_data: dict) -> Path:
            """
            获取种子内容路径。
            """
            content_path = torrent_data.get("content_path")
            if content_path:
                return Path(content_path)
            return Path(torrent_data.get("save_path")) / torrent_data.get("name")

        def __build_torrent(downloader_name: str, torrent_data: dict) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data.get("name"))
            dlspeed = torrent_data.get("dlspeed") or 0
            upspeed = torrent_data.get("upspeed") or 0
            total_size = torrent_data.get("total_size") or 0
            completed_size = torrent_data.get("completed") or 0
            torrent_path = __get_torrent_path(torrent_data)
            return DownloaderTorrent(
                downloader=downloader_name,
                hash=torrent_data.get("hash"),
                title=torrent_data.get("name"),
                name=meta.name,
                year=meta.year,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.get("save_path") or ""), downloader_name
                ) if torrent_data.get("save_path") else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                progress=torrent_data.get("progress", 0),
                size=total_size,
                state=self.__normalize_torrent_state(
                    torrent_data.get("state"), torrent_data.get("complete")
                ),
                dlspeed=StringUtils.str_filesize(dlspeed),
                upspeed=StringUtils.str_filesize(upspeed),
                tags=torrent_data.get("tags"),
                left_time=StringUtils.str_secends((total_size - completed_size) / dlspeed)
                if dlspeed > 0
                else "",
            )

        if hashs:
            # 按Hash获取
            for name, server in servers.items():
                torrents, _ = (
                    server.get_torrents(ids=hashs, tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.TRANSFER:
            # 获取已完成且未整理的
            for name, server in servers.items():
                torrents = (
                    server.get_completed_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        tags = torrent_info.get("tags") or ""
                        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                        if "已整理" in tag_list:
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.DOWNLOADING:
            # 获取正在下载的任务
            for name, server in servers.items():
                torrents = (
                    server.get_downloading_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status in (
                TorrentQueryStatus.ALL,
                TorrentQueryStatus.COMPLETED,
                TorrentQueryStatus.PAUSED,
        ):
            # 获取完整任务列表，由 MoviePilot 统一归一实际下载器状态。
            for name, server in servers.items():
                torrents, _ = server.get_torrents(tags=query_tags) or []
                try:
                    for torrent_info in torrents:
                        torrent_state = self.__normalize_torrent_state(
                            torrent_info.get("state"), torrent_info.get("complete")
                        )
                        if (
                                query_status != TorrentQueryStatus.ALL
                                and torrent_state != query_status.value
                        ):
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        else:
            return None
        return ret_torrents  # noqa

    @staticmethod
    def __normalize_query_status(
            status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]]
    ) -> TorrentQueryStatus:
        """
        归一任务查询状态。
        """
        status_value = getattr(status, "value", status)
        status_text = str(status_value or "").strip().lower()
        if not status_text or status_text in {"all", "全部"}:
            return TorrentQueryStatus.ALL
        if status_text in {
            TorrentStatus.TRANSFER.value,
            TorrentQueryStatus.TRANSFER.value,
            "transfer",
        }:
            return TorrentQueryStatus.TRANSFER
        if status_text in {
            TorrentStatus.DOWNLOADING.value,
            TorrentQueryStatus.DOWNLOADING.value,
            "downloading",
        }:
            return TorrentQueryStatus.DOWNLOADING
        if status_text in {
            TorrentQueryStatus.COMPLETED.value,
            "complete",
            "seeding",
            "完成",
            "已完成",
        }:
            return TorrentQueryStatus.COMPLETED
        if status_text in {TorrentQueryStatus.PAUSED.value, "pause", "暂停", "已暂停"}:
            return TorrentQueryStatus.PAUSED
        return TorrentQueryStatus.ALL

    @staticmethod
    def __normalize_torrent_state(
            state: Optional[Union[int, str]],
            complete: Optional[Union[int, str]],
    ) -> str:
        """
        归一 rTorrent 原始任务状态。
        """
        if str(state) == "0":
            return DownloadTaskState.PAUSED.value
        if str(complete) == "0":
            return DownloadTaskState.DOWNLOADING.value
        return DownloadTaskState.COMPLETED.value

    def transfer_completed(
        self, hashs: Union[str, list], downloader: Optional[str] = None
    ) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        # 获取原标签
        org_tags = server.get_torrent_tags(ids=hashs)
        # 种子打上已整理标签
        if org_tags:
            tags = org_tags + ["已整理"]
        else:
            tags = ["已整理"]
        # 直接设置完整标签（覆盖）
        server.set_torrents_tag(ids=hashs, tags=tags, overwrite=True)
        return None

    def remove_torrents(
        self,
        hashs: Union[str, list],
        delete_file: Optional[bool] = True,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def set_torrents_tag(
        self, hashs: Union[str, list], tags: list,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.set_torrents_tag(ids=hashs, tags=tags)

    def update_torrent(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
            download_limit: Optional[float] = None,
            upload_limit: Optional[float] = None,
            tracker_list: Optional[list] = None,
            save_path: Optional[str] = None,
            category: Optional[str] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> Optional[Dict[str, bool]]:
        """
        修改下载任务属性。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :param download_limit: 下载限速，单位 KB/s
        :param upload_limit: 上传限速，单位 KB/s
        :param tracker_list: Tracker URL列表，rTorrent 当前封装不支持
        :param save_path: 保存目录
        :param category: 分类，rTorrent 当前封装不支持
        :param ratio_limit: 分享率限制，rTorrent 当前封装不支持
        :param seeding_time_limit: 做种时间限制，rTorrent 当前封装不支持
        :return: 各项修改结果
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        results = {}
        if download_limit is not None or upload_limit is not None:
            results["limits"] = server.change_torrent(
                hash_string=hash_string,
                download_limit=download_limit,
                upload_limit=upload_limit,
            )
        if ratio_limit is not None or seeding_time_limit is not None:
            results["seeding_limits"] = False
        if tracker_list is not None:
            results["trackers"] = False
        if save_path is not None:
            results["save_path"] = server.set_torrent_location(
                hash_string=hash_string,
                location=self.normalize_path(Path(save_path), downloader),
            )
        if category is not None:
            results["category"] = False
        return results

    def start_torrents(
        self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(
        self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        获取种子文件列表
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.get_files(tid=tid)

    def downloader_info(
        self, downloader: Optional[str] = None
    ) -> Optional[List[schemas.DownloaderInfo]]:
        """
        下载器信息
        """
        if downloader:
            server: Rtorrent = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = self.get_instances().values()
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            ret_info.append(
                schemas.DownloaderInfo(
                    download_speed=info.get("dl_info_speed"),
                    upload_speed=info.get("up_info_speed"),
                    download_size=info.get("dl_info_data"),
                    upload_size=info.get("up_info_data"),
                )
            )
        return ret_info
