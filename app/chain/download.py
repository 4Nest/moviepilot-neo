import base64
import copy
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, Set, Dict, Union
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from app import schemas
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.core.cache import FileCache
from app.core.config import settings, global_vars
from app.core.context import MediaInfo, SubtitleInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.downloadfailure_oper import DownloadFailureOper
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.mediaserver_oper import MediaServerOper
from app.db.site_oper import SiteOper
from app.helper.directory import DirectoryHelper, validate_download_save_path
from app.helper.interaction import (
    PendingSlashInteraction,
    SlashInteractionManager,
    build_navigation_buttons,
    page_items,
    supports_interaction_buttons,
    supports_markdown,
    update_or_post_message,
)
from app.helper.service import ServiceConfigHelper
from app.helper.thread import ThreadHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import ExistMediaInfo, FileURI, NotExistMediaInfo, DownloaderTorrent, Notification, ResourceSelectionEventData, \
    ResourceDownloadEventData
from app.schemas.types import MediaType, TorrentStatus, EventType, MessageChannel, NotificationType, ContentType, \
    ChainEventType, DownloadTaskState, TorrentQueryStatus
from app.utils.http import RequestUtils
from app.utils.media import build_media_key, resolve_media_identity
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS = 60 * 60
DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS = (
    "无法读取种子文件",
    "下载种子内容为空",
    "无法获取下载地址",
    "种子下载失败",
    "torrent not found",
    "not found",
    "404",
    "deleted",
    "invalid torrent",
)

download_interaction_manager = SlashInteractionManager()


class DownloadChain(ChainBase):
    """
    下载处理链
    """

    _SUBTITLE_ARCHIVE_FORMATS = {
        ".zip": "zip",
        ".rar": "rar",
    }

    @staticmethod
    def _normalize_indirect_download_url(url: str, base_url: Optional[str] = None) -> str:
        """
        将两段式下载结果约束到索引器配置的可信 API 地址。

        :param url: 换票接口返回的临时下载地址
        :param base_url: 索引器配置的可信 API Base URL
        :return: 使用可信 API 来源的临时下载地址
        """
        if not url or not base_url:
            return url
        base_parts = urlparse(base_url)
        if not base_parts.scheme or not base_parts.netloc:
            return url
        url_parts = urlparse(url)
        if not url_parts.netloc:
            return urljoin(f"{base_url.rstrip('/')}/", url)
        return url_parts._replace(
            scheme=base_parts.scheme,
            netloc=base_parts.netloc,
        ).geturl()

    @staticmethod
    def _media_identity_keys(media: Optional[MediaInfo]) -> Set[str]:
        """返回媒体的统一身份键及全部兼容 ID，用于临时缺失集映射匹配。"""
        if not media:
            return set()
        source, media_id = resolve_media_identity(media=media)
        values = {
            media.tmdb_id, media.douban_id, media.bangumi_id, media.anilist_id,
            build_media_key(source, media_id),
        }
        return {str(value) for value in values if value is not None and str(value)}

    @classmethod
    def _matches_media_identity(cls, media: Optional[MediaInfo], media_key: object) -> bool:
        """判断媒体是否命中统一身份键或任一兼容 ID。"""
        return media_key is not None and str(media_key) in cls._media_identity_keys(media)

    @staticmethod
    def _safe_subtitle_file_name(file_name: str, fallback_name: str) -> str:
        """
        生成安全的字幕文件名。
        """
        file_name = Path(file_name or fallback_name).name
        if not Path(file_name).suffix and Path(fallback_name).suffix:
            file_name = f"{file_name}{Path(fallback_name).suffix}"
        return file_name

    @staticmethod
    def _is_subtitle_archive(file_name: str) -> bool:
        """
        判断是否为字幕压缩包。
        """
        return Path(file_name).suffix.lower() in DownloadChain._SUBTITLE_ARCHIVE_FORMATS

    @classmethod
    def _subtitle_archive_format(cls, file_name: str) -> Optional[str]:
        """
        获取字幕压缩包格式。
        """
        return cls._SUBTITLE_ARCHIVE_FORMATS.get(Path(file_name).suffix.lower())

    @staticmethod
    def _is_subtitle_file(file_name: str) -> bool:
        """
        判断是否为支持的字幕文件。
        """
        return Path(file_name).suffix.lower() in settings.RMT_SUBEXT

    @classmethod
    def _get_subtitle_working_dir(
            cls,
            storage_chain: StorageChain,
            storage: str,
            target_path: Path,
    ) -> Tuple[Optional[schemas.FileItem], str]:
        """
        获取字幕保存目录，返回失败原因供前端展示。
        """
        try:
            working_dir_item = storage_chain.get_folder(storage, target_path)
        except Exception as err:
            message = f"下载目录获取失败，无法保存字幕：{target_path} - {str(err)}"
            logger.error(message)
            return None, message

        if not working_dir_item:
            message = f"下载目录不存在，无法保存字幕：{target_path}"
            logger.error(message)
            return None, message
        return working_dir_item, ""

    @staticmethod
    def _detect_subtitle_fallback_name(subtitle: SubtitleInfo, content: bytes) -> str:
        """
        根据响应内容生成兜底字幕文件名。
        """
        suffix = ".zip" if content.startswith(b"PK") else ".srt"
        return f"{subtitle.title or subtitle.subtitle_id or 'subtitle'}{suffix}"

    @staticmethod
    def _resolve_media_download_dir(
            media_info: MediaInfo,
            save_path: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[Path], str]:
        """
        根据媒体信息解析下载目录。
        """
        storage = 'local'
        if save_path is not None:
            try:
                validated_save_path = validate_download_save_path(save_path)
            except ValueError as err:
                logger.warn(str(err))
                return None, None, str(err)
            if re.match(r"^[A-Za-z]:/", validated_save_path):
                target_dir = Path(validated_save_path)
            else:
                file_uri = FileURI.from_uri(validated_save_path)
                storage = file_uri.storage or storage
                target_dir = Path(file_uri.path)

            dir_info = DirectoryHelper().get_download_dir_by_save_path(
                media=media_info,
                save_path=validated_save_path,
            )
            if dir_info:
                target_dir = DownloadChain._append_download_classification(
                    root_path=target_dir,
                    dir_info=dir_info,
                    media_info=media_info,
                )
            return storage, target_dir, ""

        dir_info = DirectoryHelper().get_download_dir(media_info)
        storage = dir_info.storage if dir_info else storage
        if not dir_info:
            logger.error(f"未找到下载目录：{media_info.type.value} {media_info.title_year}")
            return None, None, "未找到下载目录"

        download_dir = DownloadChain._append_download_classification(
            root_path=Path(dir_info.download_path),
            dir_info=dir_info,
            media_info=media_info,
        )
        return storage, download_dir, ""

    @staticmethod
    def _append_download_classification(
            root_path: Path,
            dir_info: schemas.TransferDirectoryConf,
            media_info: MediaInfo,
    ) -> Path:
        """
        按下载目录配置拼装媒体类型和类别子目录。

        :param root_path: 下载根目录
        :param dir_info: 下载目录配置
        :param media_info: 媒体信息
        :return: 应传给存储或下载器的媒体下载目录
        """
        download_dir = root_path
        if not dir_info.media_type and dir_info.download_type_folder:
            download_dir = download_dir / media_info.type.value
        if not dir_info.media_category and dir_info.download_category_folder and media_info.category:
            download_dir = download_dir / media_info.category
        return download_dir

    @staticmethod
    def _upload_subtitle_file(
            storage_chain: StorageChain,
            storage: str,
            working_dir_item: schemas.FileItem,
            subtitle_file: Path,
    ) -> Tuple[Optional[str], str]:
        """
        上传单个字幕文件到目标目录。
        """
        target_sub_file = Path(working_dir_item.path) / subtitle_file.name
        if storage_chain.get_file_item(storage, target_sub_file):
            logger.info(f"字幕文件已存在：{target_sub_file}")
            return target_sub_file.as_posix(), ""
        logger.info(f"转移字幕 {subtitle_file} 到 {target_sub_file} ...")
        uploaded = storage_chain.upload_file(working_dir_item, subtitle_file)
        if uploaded:
            return uploaded.path, ""
        message = f"保存字幕文件失败：{target_sub_file}"
        logger.error(message)
        return None, message

    @staticmethod
    def _build_subtitle_download_error(response) -> str:
        """
        从字幕下载响应中提取前端可展示的失败原因。
        """
        status_code = getattr(response, "status_code", None)
        reason = getattr(response, "reason", "") or ""
        message = "下载字幕文件失败"
        if status_code:
            message = f"{message}，状态码：{status_code}"
            if reason:
                message = f"{message} {reason}"
        try:
            response_text = (getattr(response, "text", "") or "").strip()
            response_text = re.sub(r"\s+", " ", response_text)
            if response_text:
                message = f"{message}：{response_text[:200]}"
        except Exception as err:
            logger.debug(f"读取字幕下载失败响应内容失败：{str(err)}")
        return message

    def _save_subtitle_response(
            self,
            subtitle: SubtitleInfo,
            response,
            storage: str,
            target_dir: Path,
    ) -> Tuple[bool, str, List[str]]:
        """
        保存字幕下载响应到目标目录。
        """
        fallback_name = self._detect_subtitle_fallback_name(subtitle, response.content)
        file_name = subtitle.file_name or TorrentHelper.get_url_filename(response, subtitle.enclosure)
        if not Path(file_name).suffix:
            file_name = fallback_name
        file_name = self._safe_subtitle_file_name(
            file_name=file_name,
            fallback_name=fallback_name,
        )
        if not self._is_subtitle_archive(file_name) and not self._is_subtitle_file(file_name):
            message = f"下载链接不是支持的字幕文件：{file_name}"
            logger.warn(f"{message}，链接：{subtitle.enclosure}")
            return False, message, []

        storage_chain = StorageChain()
        working_dir_item, message = self._get_subtitle_working_dir(
            storage_chain=storage_chain,
            storage=storage,
            target_path=target_dir,
        )
        if not working_dir_item:
            return False, message, []

        saved_files = []
        temp_file = settings.TEMP_PATH / file_name
        temp_extract_dir = temp_file.with_name(temp_file.stem)
        try:
            settings.TEMP_PATH.mkdir(parents=True, exist_ok=True)
            temp_file.write_bytes(response.content)
            if self._is_subtitle_archive(file_name):
                try:
                    SystemUtils.unpack_archive(
                        temp_file,
                        temp_extract_dir,
                        archive_format=self._subtitle_archive_format(file_name),
                    )
                except Exception as err:
                    message = f"字幕压缩包解压失败：{str(err)}"
                    logger.error(f"{message}，文件：{temp_file}")
                    return False, message, []
                for sub_file in SystemUtils.list_files(temp_extract_dir, settings.RMT_SUBEXT):
                    uploaded_path, message = self._upload_subtitle_file(
                        storage_chain=storage_chain,
                        storage=storage,
                        working_dir_item=working_dir_item,
                        subtitle_file=sub_file,
                    )
                    if uploaded_path:
                        saved_files.append(uploaded_path)
                    elif message:
                        logger.error(message)
            else:
                uploaded_path, message = self._upload_subtitle_file(
                    storage_chain=storage_chain,
                    storage=storage,
                    working_dir_item=working_dir_item,
                    subtitle_file=temp_file,
                )
                if uploaded_path:
                    saved_files.append(uploaded_path)
                elif message:
                    return False, message, []
            if not saved_files:
                message = "未保存任何字幕文件"
                logger.error(message)
                return False, message, []
            return True, "字幕文件保存成功", saved_files
        except Exception as err:
            message = f"保存字幕文件失败：{str(err)}"
            logger.error(message)
            return False, message, []
        finally:
            try:
                if temp_extract_dir.exists():
                    shutil.rmtree(temp_extract_dir)
                if temp_file.exists():
                    temp_file.unlink()
            except Exception as err:
                logger.error(f"删除临时字幕文件失败：{str(err)}")

    def download_subtitle(
            self,
            subtitle: SubtitleInfo,
            media_source: Optional[str] = None,
            media_id: Optional[str] = None,
            tmdbid: Optional[int] = None,
            doubanid: Optional[str] = None,
            save_path: Optional[str] = None,
            username: Optional[str] = None,
            bangumiid: Optional[int] = None,
            anilistid: Optional[int] = None,
    ) -> Tuple[bool, str, List[str]]:
        """
        下载字幕文件并保存到媒体对应的下载目录。

        :param subtitle: 字幕搜索结果
        :param media_source: 媒体数据源
        :param media_id: 数据源原生ID
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param bangumiid: Bangumi ID
        :param anilistid: AniList ID
        :param save_path: 保存路径
        :param username: 调用下载的用户名
        :return: 成功状态、提示消息、保存文件列表
        """
        if not subtitle or not subtitle.enclosure:
            return False, "字幕下载链接为空", []

        metainfo = MetaInfo(title=subtitle.title, subtitle=subtitle.description)
        mediainfo = self.recognize_media(
            meta=metainfo,
            source=media_source,
            mediaid=media_id,
            tmdbid=tmdbid,
            doubanid=doubanid,
            bangumiid=bangumiid,
            anilistid=anilistid,
        )
        if not mediainfo:
            return False, "无法识别媒体信息", []
        mediainfo = MediaChain().supplement_tmdb_info(mediainfo, metainfo)

        storage, target_dir, error_msg = self._resolve_media_download_dir(
            media_info=mediainfo,
            save_path=save_path,
        )
        if not target_dir:
            return False, error_msg or "未找到下载目录", []

        request = RequestUtils(
            cookies=subtitle.site_cookie,
            ua=subtitle.site_ua or settings.USER_AGENT,
            proxies=settings.PROXY if subtitle.site_proxy else None,
        )
        try:
            response = request.get_res(subtitle.enclosure, raise_exception=True)
        except Exception as err:
            message = f"下载字幕文件失败：{str(err)}"
            logger.error(message)
            return False, message, []
        if response is None:
            return False, "下载字幕文件失败：未收到站点响应", []
        if response.status_code != 200:
            message = self._build_subtitle_download_error(response)
            logger.error(message)
            return False, message, []

        success, message, saved_files = self._save_subtitle_response(
            subtitle=subtitle,
            response=response,
            storage=storage,
            target_dir=target_dir,
        )
        if not success:
            return False, message, []

        logger.info(
            f"{mediainfo.title_year} 字幕下载完成：{subtitle.site_name} - {subtitle.title}，用户：{username}"
        )
        return True, "字幕下载成功", saved_files

    def _submit_download_added_task(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes],
    ) -> None:
        """
        后台执行下载成功后的附加处理，避免站点字幕下载阻塞添加下载响应。
        """

        def _run_download_added() -> None:
            try:
                self.download_added(
                    context=context,
                    download_dir=download_dir,
                    torrent_content=torrent_content,
                )
            except Exception as e:
                logger.error(f"执行下载成功后处理失败：{str(e)}")

        try:
            ThreadHelper().submit(_run_download_added)
        except Exception as err:
            logger.error(f"提交下载成功后处理后台任务失败：{str(err)}")

    @staticmethod
    def _is_subscribe_source(source: Optional[str]) -> bool:
        """
        判断下载来源是否为订阅任务。
        """
        return bool(source and str(source).startswith("Subscribe|"))

    @staticmethod
    def _format_failure_episodes(meta: Optional[MetaBase]) -> Optional[str]:
        """
        从识别元数据中格式化用于失败记录的集数。
        """
        if not meta:
            return None
        if getattr(meta, "episode", None):
            return meta.episode
        episode_list = getattr(meta, "episode_list", None)
        if episode_list:
            return StringUtils.format_ep(list(episode_list))
        return None

    @staticmethod
    def _torrent_resource_key(torrent: Optional[TorrentInfo]) -> str:
        """
        生成不保存敏感下载链接的种子资源键。
        """
        if not torrent:
            return ""
        for attr_name in ("torrent_id", "info_hash"):
            value = getattr(torrent, attr_name, None)
            if value:
                return str(value)

        for attr_name in ("page_url", "enclosure"):
            url = getattr(torrent, attr_name, None)
            if not url:
                continue
            match = re.search(r"\[(.*?)](.*)", str(url))
            if match:
                url = match.group(2)
            parsed = urlparse(str(url))
            params = parse_qs(parsed.query)
            for param_name in ("id", "torrentid", "torrent_id", "tid", "hash"):
                values = params.get(param_name)
                if values:
                    return f"{parsed.netloc}:{param_name}={values[0]}"
            if parsed.netloc and parsed.path:
                return f"{parsed.netloc}{parsed.path}"

        title = getattr(torrent, "title", "") or ""
        size = getattr(torrent, "size", "") or ""
        return f"title={title}|size={size}"

    @classmethod
    def _build_download_failure_fingerprint(cls, context: Context) -> Optional[str]:
        """
        根据媒体和种子资源信息生成失败冷却指纹。
        """
        media = getattr(context, "media_info", None)
        torrent = getattr(context, "torrent_info", None)
        if not media or not torrent:
            return None

        media_type = getattr(getattr(media, "type", None), "value", getattr(media, "type", None))
        media_source, media_id = resolve_media_identity(media=media)
        media_key = (
            f"{media_source}:{media_id}"
            if media_source and media_id
            else getattr(media, "imdb_id", None)
            or getattr(media, "tvdb_id", None)
            or f"{getattr(media, 'title', '')}:{getattr(media, 'year', '')}"
        )
        meta = getattr(context, "meta_info", None)
        site = getattr(torrent, "site", None) or getattr(torrent, "site_name", None)
        meta_season = getattr(meta, "season", None)
        media_season = getattr(media, "season", None)
        season = meta_season if meta_season is not None else media_season
        payload = {
            "media_type": str(media_type or ""),
            "media_key": str(media_key or ""),
            "season": str(season) if season is not None else "",
            "episodes": cls._format_failure_episodes(meta) or "",
            "site": str(site or ""),
            "resource": cls._torrent_resource_key(torrent),
        }
        if not payload["media_type"] or not payload["media_key"] or not payload["resource"]:
            return None
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _download_failure_ttl(error_msg: Optional[str]) -> int:
        """
        按失败原因确定资源冷却时间。
        """
        error_text = str(error_msg or "").lower()
        if error_text.startswith(("换票失败：", "临时下载地址失效：")):
            return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS
        if any(keyword in error_text for keyword in DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS):
            return DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS
        return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS

    def _record_download_failure(
            self,
            context: Context,
            error_msg: Optional[str],
            downloader: Optional[str] = None,
            source: Optional[str] = None,
            episodes: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """
        记录资源级下载失败，并返回本次失败指纹。
        """
        fingerprint = self._build_download_failure_fingerprint(context)
        if not fingerprint:
            return None

        now_timestamp = time.time()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_timestamp))
        next_retry_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now_timestamp + self._download_failure_ttl(error_msg)),
        )
        media = context.media_info
        media_source, media_id = resolve_media_identity(media=media)
        meta = context.meta_info
        torrent = context.torrent_info
        site = getattr(torrent, "site", None)
        try:
            DownloadFailureOper().record_failure(
                fingerprint=fingerprint,
                now_time=now_time,
                next_retry_at=next_retry_at,
                type=getattr(getattr(media, "type", None), "value", getattr(media, "type", None)),
                title=getattr(media, "title", None),
                year=getattr(media, "year", None),
                tmdbid=getattr(media, "tmdb_id", None),
                doubanid=getattr(media, "douban_id", None),
                bangumiid=media.bangumi_id,
                anilistid=media.anilist_id,
                media_source=media_source,
                media_id=media_id,
                seasons=getattr(meta, "season", None),
                episodes=StringUtils.format_ep(list(episodes)) if episodes else self._format_failure_episodes(meta),
                site=site if isinstance(site, int) else None,
                site_name=getattr(torrent, "site_name", None),
                torrent_id=self._torrent_resource_key(torrent),
                torrent_name=getattr(torrent, "title", None),
                torrent_size=getattr(torrent, "size", None),
                downloader=downloader,
                source=str(source)[:1000] if source else None,
                error_message=str(error_msg or "")[:1000],
            )
        except Exception as err:
            logger.error(f"记录下载失败冷却失败：{str(err)}")
        return fingerprint

    def _active_download_failure_fingerprints(
            self,
            contexts: List[Context],
            source: Optional[str],
    ) -> Set[str]:
        """
        查询当前订阅候选中仍处于冷却期的失败指纹。
        """
        if not self._is_subscribe_source(source):
            return set()
        fingerprints = [
            fingerprint
            for fingerprint in [
                self._build_download_failure_fingerprint(context)
                for context in contexts or []
            ]
            if fingerprint
        ]
        if not fingerprints:
            return set()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            return set(
                DownloadFailureOper()
                .get_active_by_fingerprints(fingerprints=fingerprints, now_time=now_time)
                .keys()
            )
        except Exception as err:
            logger.error(f"查询下载失败冷却失败：{str(err)}")
            return set()

    @staticmethod
    def _redact_indirect_download_error(
            text: str, request_params: Dict[str, Any], cookie: Optional[str],
    ) -> str:
        """
        移除换票错误中的凭据和完整签名地址，并限制诊断长度。
        """
        headers = request_params.get("header")
        secrets = list(headers.values()) if isinstance(headers, dict) else []
        if cookie:
            secrets.extend([cookie, *(part.partition("=")[2].strip() for part in cookie.split(";"))])
        for secret in secrets:
            if isinstance(secret, str) and secret:
                text = text.replace(secret, "[已隐藏]")
                text = text.replace(secret.removeprefix("Bearer "), "[已隐藏]")

        def url_origin(match: re.Match) -> str:
            """仅保留诊断 URL 的协议和主机。"""
            parsed = urlparse(match.group())
            return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "[已隐藏]"

        text = re.sub(r"https?://[^\s<>\"'，；（）]+", url_origin, text)
        text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [已隐藏]", text)
        text = re.sub(
            r"(?i)\b(token|passkey|api[-_]?key|cookie|password|secret|sign)\s*[:=]\s*[^\s,;]+",
            r"\1=[已隐藏]", text,
        )
        return " ".join(text.split())[:500]

    @classmethod
    def _indirect_download_failure_reason(
            cls, response: Any, payload: Any, request_params: Dict[str, Any],
            cookie: Optional[str], reason: str,
    ) -> str:
        """
        组合换票阶段、HTTP 状态、有限响应摘要和 Cookie 携带状态。
        """
        headers = request_params.get("header")
        header_cookie = isinstance(headers, dict) and any(
            str(key).lower() == "cookie" and value for key, value in headers.items()
        )
        cookie_sent = bool((cookie and request_params.get("cookie")) or header_cookie)
        details = ["未收到响应（连接失败或超时）"] if response is None else [f"HTTP {response.status_code}"]
        if response is not None:
            fields = []
            if isinstance(payload, dict):
                for key in ("code", "message", "msg", "error"):
                    value = payload.get(key)
                    if isinstance(value, dict):
                        value = value.get("message")
                    if isinstance(value, (str, int)):
                        fields.append(f"{key}={value}")
            summary = "；".join(fields) or (
                "空响应" if not response.content else "响应未包含有效下载地址"
            )
            details.append(f"响应摘要：{summary}")
        details.append(f"携带 Cookie：{'是' if cookie_sent else '否'}")
        error = cls._redact_indirect_download_error(
            f"{reason}（{'；'.join(details)}）", request_params, cookie,
        )
        return f"换票失败：{error}"[:500]

    def _resolve_indirect_download_url(
            self, url: str, ua: Optional[str] = None, cookie: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """
        解析 [base64]url 换票配方，失败时返回已脱敏的换票诊断。
        """
        match = re.search(r"\[(.*)](.*)", url)
        if not match:
            return None, "换票失败：请求编码无效"
        encoded_params, request_url = match.groups()
        if not encoded_params:
            return request_url, ""
        try:
            request_text = base64.b64decode(encoded_params.encode("utf-8")).decode("utf-8")
            request_params = json.loads(request_text)
        except (ValueError, UnicodeError):
            return None, "换票失败：无法解析请求编码"
        if not isinstance(request_params, dict):
            return None, "换票失败：请求参数格式无效"

        original_cookie = cookie
        request_cookie = cookie if request_params.get("cookie") else None
        headers = request_params.get("header")
        proxy = request_params.get("proxy")
        request = RequestUtils(
            ua=ua,
            cookies=request_cookie,
            headers=headers,
            proxies=settings.PROXY if proxy else None,
        )
        if request_params.get("method") == "get":
            response = request.get_res(request_url, params=request_params.get("params"))
        else:
            response = request.post_res(request_url, params=request_params.get("params"))

        payload = None
        if response is not None:
            try:
                payload = response.json()
            except (TypeError, ValueError, AttributeError):
                payload = None
        if response is None:
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "无法连接站点下载凭证接口",
            )
        if not response:
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "站点拒绝换票请求",
            )
        if not request_params.get("result"):
            data = response.text.strip()
            if data.startswith("magnet:"):
                return data, ""
            data = self._normalize_indirect_download_url(
                url=data, base_url=request_params.get("result_base_url"),
            )
            parsed = urlparse(data)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return None, self._indirect_download_failure_reason(
                    response, payload, request_params, original_cookie, "接口未返回有效下载地址",
                )
            return data, ""
        if not isinstance(payload, dict):
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "站点下载凭证响应结构无效",
            )
        success_key = request_params.get("success")
        if success_key and not payload.get(success_key):
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "站点返回换票失败",
            )
        data = payload
        for key in str(request_params.get("result")).split("."):
            if not isinstance(data, dict):
                data = None
                break
            data = data.get(key)
            if not data:
                break
        if not data:
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "站点未返回有效下载地址",
            )
        result_path = request_params.get("result_path")
        result_query_param = request_params.get("result_query_param")
        if result_path and result_query_param:
            result_url = urljoin(
                f"{str(request_params.get('result_base_url')).rstrip('/')}/",
                str(result_path).lstrip('/'),
            )
            return f"{result_url}?{urlencode({result_query_param: data})}", ""
        if not isinstance(data, str):
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "站点未返回有效下载地址",
            )
        if data.startswith("magnet:"):
            return data, ""
        data = self._normalize_indirect_download_url(
            url=data,
            base_url=request_params.get("result_base_url"),
        )
        parsed = urlparse(data)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None, self._indirect_download_failure_reason(
                response, payload, request_params, original_cookie, "接口未返回有效下载地址",
            )
        logger.info("已获取到站点临时下载地址")
        return data, ""

    def download_torrent(self, torrent: TorrentInfo,
                         channel: MessageChannel = None,
                         source: Optional[str] = None,
                         userid: Union[str, int] = None,
                         on_failure: Optional[Callable[[str], None]] = None,
                         ) -> Tuple[Optional[Union[str, bytes]], str, list]:
        """
        下载种子文件或返回磁力链接，并向调用方透传具体失败原因。

        :param on_failure: 准备或取种失败回调，用于保留瞬态冷却分类
        :return: 种子内容，种子目录名，种子文件清单
        """
        if not torrent.enclosure:
            return None, "", []
        if torrent.enclosure.startswith("magnet:"):
            return torrent.enclosure, "", []
        site_cookie = torrent.site_cookie
        indirect_download = torrent.enclosure.startswith("[")
        redirect_error = ""
        if indirect_download:
            torrent_url, redirect_error = self._resolve_indirect_download_url(
                url=torrent.enclosure,
                ua=torrent.site_ua,
                cookie=site_cookie,
            )
            # 涉及解析地址的不使用 Cookie 下载种子，否则 MT 会出错。
            site_cookie = None
        else:
            torrent_url = torrent.enclosure
        if not torrent_url:
            reason = redirect_error or (
                "换票失败：未获取到下载地址" if indirect_download else "无法获取下载地址"
            )
            if on_failure:
                on_failure(reason)
            logger.error(f"{torrent.title} 无法获取下载地址：{reason}")
            self.post_message(Notification(
                channel=channel,
                source=source if channel else None,
                mtype=NotificationType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{reason}\n站点：{torrent.site_name}",
                userid=userid,
            ))
            return None, "", []
        _, content, download_folder, files, error_msg = TorrentHelper().download_torrent(
            url=torrent_url,
            cookie=site_cookie,
            ua=torrent.site_ua or settings.USER_AGENT,
            proxy=torrent.site_proxy,
            cache_invalid=not indirect_download,
        )

        if isinstance(content, str):
            return content, "", []
        if not content:
            if indirect_download:
                reason = "返回内容不是有效种子，请重新换票" \
                    if "种子数据有误" in (error_msg or "") else (error_msg or "")
                error_msg = f"临时下载地址失效：{reason or '未下载到种子数据'}"
            reason = error_msg or "下载种子内容为空"
            if on_failure:
                on_failure(reason)
            logger.error(f"下载种子文件失败：{torrent.title}，{reason}")
            self.post_message(Notification(
                channel=channel,
                source=source if channel else None,
                mtype=NotificationType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{reason}\n站点：{torrent.site_name}",
                userid=userid,
            ))
            return None, "", []
        return content, download_folder, files

    @staticmethod
    def _resolve_bt_default_config(torrent: TorrentInfo) -> Optional[schemas.DownloaderConf]:
        """
        公开(BT)站点资源在未显式或站点级指定下载器时,回退到配置的 BT 默认下载器。

        :param torrent: 种子信息
        :return: BT 默认下载器配置;非 BT 站点或未配置时返回 None
        """
        is_public = torrent.site_public
        if is_public is None and torrent.site:
            site = SiteOper().get(torrent.site)
            is_public = bool(site and site.public)
        if not is_public:
            return None
        for conf in ServiceConfigHelper.get_downloader_configs():
            if conf.enabled and conf.bt_default:
                return conf
        return None

    @staticmethod
    def _resolve_bt_save_path(save_path: str) -> Tuple[str, Path, str]:
        """
        解析 BT 默认下载路径。该路径来自管理员级下载器配置,
        与下载目录配置同一信任级别,不经过保存路径白名单校验,
        也不追加媒体类型/类别子目录。
        """
        value = save_path.strip()
        if re.match(r"^[A-Za-z]:/", value):
            return 'local', Path(value), ""
        file_uri = FileURI.from_uri(value)
        return file_uri.storage or 'local', Path(file_uri.path), ""

    def download_single(self, context: Context,
                        torrent_file: Path = None,
                        torrent_content: Optional[Union[str, bytes]] = None,
                        episodes: Set[int] = None,
                        channel: MessageChannel = None,
                        source: Optional[str] = None,
                        downloader: Optional[str] = None,
                        save_path: Optional[str] = None,
                        userid: Union[str, int] = None,
                        username: Optional[str] = None,
                        label: Optional[str] = None,
                        return_detail: bool = False,
                        custom_words: Optional[str] = None,
                        subscribe_id: Optional[int] = None,
                        version_rule_id: Optional[str] = None,
                        version_settings: Optional[dict] = None) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        下载及发送通知
        :param context: 资源上下文
        :param torrent_file: 种子文件路径
        :param torrent_content: 种子内容（磁力链或种子文件内容）
        :param episodes: 需要下载的集数
        :param channel: 通知渠道
        :param source: 来源（消息通知、Subscribe、Manual等）
        :param downloader: 下载器
        :param save_path: 保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param userid: 用户ID
        :param username: 调用下载的用户名/插件名
        :param label: 自定义标签
        :param return_detail: 是否返回详细结果；False 时返回下载任务 hash 或 None，True 时返回 (hash, error_msg)
        :param custom_words: 下载来源（如订阅）的完整自定义识别词文本，随下载记录存档，供整理时原样复现识别
        :return: return_detail=False 时返回下载任务 hash 或 None；return_detail=True 时返回 (hash, error_msg)
        """
        _torrent = context.torrent_info
        _media = context.media_info
        _meta = context.meta_info
        _site_downloader = _torrent.site_downloader
        # 显式指定 > 站点指定 > BT(公开)站点默认
        _bt_conf = None
        if not downloader and not _site_downloader:
            _bt_conf = self._resolve_bt_default_config(_torrent)
        _effective_downloader = downloader or _site_downloader or (_bt_conf.name if _bt_conf else None)

        # 下载目录和下载器分类依赖 TMDB 辅助分类，但媒体主身份保持不变。
        _media = MediaChain().supplement_tmdb_info(_media, _meta)
        context.media_info = _media

        # 发送资源下载事件，允许外部拦截下载
        event_data = ResourceDownloadEventData(
            context=context,
            episodes=episodes or context.meta_info.episode_list,
            channel=channel,
            origin=source,
            downloader=downloader,
            options={
                "save_path": save_path,
                "userid": userid,
                "username": username,
                "media_category": _media.category
            }
        )
        # 触发资源下载事件
        event = eventmanager.send_event(ChainEventType.ResourceDownload, event_data)
        if event and event.event_data:
            event_data: ResourceDownloadEventData = event.event_data
            # 如果事件被取消，跳过资源下载
            if event_data.cancel:
                logger.debug(
                    f"Resource download canceled by event: {event_data.source},"
                    f"Reason: {event_data.reason}")
                return (None, "下载被事件取消") if return_detail else None
            # 如果事件修改了下载路径，使用新路径
            if event_data.options and "save_path" in event_data.options:
                save_path = event_data.options.get("save_path")

        if save_path is not None:
            try:
                save_path = validate_download_save_path(save_path)
            except ValueError as err:
                logger.warn(str(err))
                return (None, str(err)) if return_detail else None

        # 实际下载的集数
        download_episodes = StringUtils.format_ep(list(episodes)) if episodes else None
        if episodes is not None:
            context.selected_episodes = sorted(set(episodes))
        elif _meta and _meta.episode_list:
            context.selected_episodes = sorted(set(_meta.episode_list))
        else:
            context.selected_episodes = []
        _folder_name = ""
        download_errors: List[str] = []
        if not torrent_file and not torrent_content:
            # 下载种子文件，得到的可能是文件也可能是磁力链
            torrent_content, _folder_name, _file_list = self.download_torrent(
                _torrent,
                channel=channel,
                source=source,
                userid=userid,
                on_failure=download_errors.append,
            )
        elif torrent_file:
            if torrent_file.exists():
                torrent_content = torrent_file.read_bytes()
            else:
                # 缓存处理器
                cache_backend = FileCache()
                # 读取缓存的种子文件
                torrent_content = cache_backend.get(torrent_file.as_posix(), region="torrents")

        if not torrent_content:
            error_msg = download_errors[-1] if download_errors else "下载种子内容为空"
            self._record_download_failure(
                context=context,
                error_msg=error_msg,
                downloader=_effective_downloader,
                source=source,
                episodes=episodes,
            )
            return (None, error_msg) if return_detail else None

        # 获取种子文件的文件夹名和文件清单
        _folder_name, _file_list = TorrentHelper().get_fileinfo_from_torrent_content(torrent_content)

        # 显式保存路径优先;BT 默认下载器配置的路径次之;否则按下载目录设置
        if not save_path and _bt_conf and _bt_conf.bt_save_path:
            storage, download_dir, error_msg = self._resolve_bt_save_path(_bt_conf.bt_save_path)
        else:
            storage, download_dir, error_msg = self._resolve_media_download_dir(
                media_info=_media,
                save_path=save_path,
            )
        if not download_dir:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(f"{_media.type.value} {_media.title_year} 未找到下载目录！",
                                       title="下载失败", role="system")
            return (None, error_msg or "未找到下载目录") if return_detail else None
        file_uri = FileURI(storage=storage, path=download_dir.as_posix())
        download_dir = Path(file_uri.uri)

        # 添加下载
        result: Optional[tuple] = self.download(content=torrent_content,
                                                cookie=_torrent.site_cookie,
                                                episodes=episodes,
                                                download_dir=download_dir,
                                                category=_media.category,
                                                label=label,
                                                downloader=_effective_downloader)
        if result:
            _downloader, _hash, _layout, error_msg = result
        else:
            _downloader, _hash, _layout, error_msg = None, None, None, "未找到下载器"

        if _hash:
            # `不创建子文件夹` 或 `不存在子文件夹`
            if _layout == "NoSubfolder" or not _folder_name:
                # 下载路径记录至文件
                download_path = download_dir / _file_list[0] if _file_list else download_dir
            # 原始布局
            elif _folder_name:
                download_path = download_dir / _folder_name
            # 创建子文件夹
            else:
                download_path = download_dir / Path(_file_list[0]).stem if _file_list else download_dir
            # 文件保存路径
            _save_path = download_dir if _layout == "NoSubfolder" or not _folder_name else download_path

            # 登记下载记录
            downloadhis = DownloadHistoryOper()
            media_source, media_id = resolve_media_identity(media=_media)
            downloadhis.add(
                path=download_path.as_posix(),
                type=_media.type.value,
                title=_media.title,
                year=_media.year,
                tmdbid=_media.tmdb_id,
                imdbid=_media.imdb_id,
                tvdbid=_media.tvdb_id,
                doubanid=_media.douban_id,
                bangumiid=_media.bangumi_id,
                anilistid=_media.anilist_id,
                media_source=media_source,
                media_id=media_id,
                seasons=_meta.season,
                episodes=download_episodes or _meta.episode,
                image=_media.get_backdrop_image(),
                poster=_media.get_poster_image(),
                downloader=_downloader,
                download_hash=_hash,
                torrent_name=_torrent.title,
                torrent_description=_torrent.description,
                torrent_site=_torrent.site_name,
                userid=userid,
                username=username,
                channel=channel.value if channel else None,
                date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                media_category=_media.category,
                episode_group=_media.episode_group,
                note={"source": source},
                custom_words=custom_words,
                subscribe_id=subscribe_id,
                version_rule_id=version_rule_id,
                version_settings=version_settings,
            )

            # 登记下载文件
            files_to_add = []
            unmatched_single_files = []
            media_file_count = 0
            media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT
            for file in _file_list:
                suffix = Path(file).suffix.lower()
                if not suffix or suffix not in media_exts:
                    continue
                if suffix in settings.RMT_MEDIAEXT:
                    media_file_count += 1
                file_item = {
                    "download_hash": _hash,
                    "downloader": _downloader,
                    "fullpath": (_save_path / file).as_posix(),
                    "savepath": _save_path.as_posix(),
                    "filepath": file,
                    "torrentname": _meta.org_string,
                }
                if episodes:
                    file_meta = MetaInfo(
                        Path(file).stem,
                        custom_words=custom_words.splitlines() if custom_words else [],
                    )
                    file_episodes = set(file_meta.episode_list)
                    if file_meta.begin_episode and file_meta.begin_episode in episodes:
                        files_to_add.append(file_item)
                    elif suffix in settings.RMT_MEDIAEXT and len(file_episodes) <= 1:
                        unmatched_single_files.append(file_item)
                else:
                    files_to_add.append(file_item)
            if (not files_to_add and episodes and len(episodes) == 1
                    and media_file_count == 1 and len(unmatched_single_files) == 1):
                # 下载器已按唯一视频文件兜底选集，历史明细使用同一映射。
                files_to_add = unmatched_single_files
            if files_to_add:
                downloadhis.add_files(files_to_add)

            # 下载成功发送消息
            self.post_message(
                Notification(
                    channel=channel,
                    source=source if channel else None,
                    mtype=NotificationType.Download,
                    ctype=ContentType.DownloadAdded,
                    image=_media.get_message_image(),
                    link=settings.MP_DOMAIN('/#/downloading'),
                    userid=userid,
                    username=username
                ),
                meta=_meta,
                mediainfo=_media,
                torrentinfo=_torrent,
                download_episodes=download_episodes,
                username=username,
            )
            # 下载成功后处理
            self._submit_download_added_task(
                context=context,
                download_dir=download_dir,
                torrent_content=torrent_content,
            )
            # 广播事件
            self.eventmanager.send_event(EventType.DownloadAdded, {
                "hash": _hash,
                "context": context,
                "username": username,
                "downloader": _downloader,
                "episodes": episodes or _meta.episode_list,
                "source": source
            })
        else:
            # 下载失败
            logger.error(f"{_media.title_year} 添加下载任务失败："
                         f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
            self._record_download_failure(
                context=context,
                error_msg=error_msg,
                downloader=_downloader or _effective_downloader,
                source=source,
                episodes=episodes,
            )
            # 只发送给对应渠道和用户
            self.post_message(Notification(
                channel=channel,
                source=source if channel else None,
                mtype=NotificationType.Manual,
                title="添加下载任务失败：%s %s"
                      % (_media.title_year, _meta.season_episode),
                text=f"站点：{_torrent.site_name}\n"
                     f"种子名称：{_meta.org_string}\n"
                     f"错误信息：{error_msg}",
                image=_media.get_message_image(),
                userid=userid))
        if return_detail:
            return _hash, error_msg
        return _hash
    def batch_download(self,
                       contexts: List[Context],
                       no_exists: Dict[Union[int, str], Dict[int, NotExistMediaInfo]] = None,
                       save_path: Optional[str] = None,
                       downloader: Optional[str] = None,
                       source: Optional[str] = None,
                       userid: Union[str, int] = None,
                       username: Optional[str] = None,
                       channel: MessageChannel = None,
                       custom_words: Optional[str] = None,
                       subscribe_id: Optional[int] = None,
                       version_rule_id: Optional[str] = None,
                       version_settings: Optional[dict] = None
                       ) -> Tuple[List[Context], Dict[Union[int, str], Dict[int, NotExistMediaInfo]]]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param channel:  通知渠道
        :param source:  来源（消息通知、订阅、手工下载等）
        :param userid:  用户ID
        :param username: 调用下载的用户名/插件名
        :param downloader: 下载器
        :param custom_words: 下载来源（如订阅）的完整自定义识别词文本，随下载记录存档，供整理时原样复现识别
        :return: 已经下载的资源列表、剩余未下载到的剧集 no_exists[tmdb_id/douban_id] = {season: NotExistMediaInfo}
        """
        # 已下载的项目
        downloaded_list: List[Context] = []

        def __update_seasons(_mid: Union[int, str], _need: list, _current: list) -> list:
            """
            更新need_tvs季数，返回剩余季数
            :param _mid: TMDBID
            :param _need: 需要下载的季数
            :param _current: 已经下载的季数
            """
            # 剩余季数
            need = list(set(_need).difference(set(_current)))
            # 清除已下载的季信息
            seas = copy.deepcopy(no_exists.get(_mid))
            if seas:
                for _sea in list(seas):
                    if _sea not in need:
                        no_exists[_mid].pop(_sea)
                    if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                        no_exists.pop(_mid)
                        break
            return need

        def __update_episodes(_mid: Union[int, str], _sea: int, _need: list, _current: set) -> list:
            """
            更新need_tvs集数，返回剩余集数
            :param _mid: TMDBID
            :param _sea: 季数
            :param _need: 需要下载的集数
            :param _current: 已经下载的集数
            """
            # 剩余集数
            need = list(set(_need).difference(set(_current)))
            if need:
                not_exist = no_exists[_mid][_sea]
                no_exists[_mid][_sea] = NotExistMediaInfo(
                    season=not_exist.season,
                    episodes=need,
                    total_episode=not_exist.total_episode,
                    start_episode=not_exist.start_episode,
                    require_complete_coverage=not_exist.require_complete_coverage
                )
            else:
                no_exists[_mid].pop(_sea)
                if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                    no_exists.pop(_mid)
            return need

        def __get_season_episodes(_mid: Union[int, str], season: int) -> int:
            """
            获取需要的季的集数
            """
            if not no_exists.get(_mid):
                return 9999
            no_exist = no_exists.get(_mid)
            if not no_exist.get(season):
                return 9999
            return no_exist[season].total_episode

        def __get_no_exist_media(_mid: Union[int, str], season: int) -> Optional[NotExistMediaInfo]:
            """
            获取指定媒体和季的缺失信息。
            """
            if not no_exists or not no_exists.get(_mid):
                return None
            return no_exists.get(_mid).get(season)

        def __get_required_episodes(_mid: Union[int, str], season: int) -> Set[int]:
            """
            获取整季候选必须覆盖的目标集范围。
            """
            tv = __get_no_exist_media(_mid, season)
            if not tv:
                return set()
            if not tv.total_episode:
                return set()
            start = tv.start_episode or 1
            return set(range(start, tv.total_episode + 1))

        def __requires_complete_coverage(_tv: Optional[NotExistMediaInfo]) -> bool:
            """
            判断当前缺失范围是否要求候选资源完整覆盖目标范围。
            """
            if not _tv:
                return False
            return bool(_tv.require_complete_coverage)

        def __apply_allowed_episodes(_need_episodes, _context: Context) -> Set[int]:
            """
            根据候选携带的允许集裁剪 need_episodes，返回真正可下载的剧集集合。

            语义：allowed_episodes 为 None 表示调用方未约束，沿用 need_episodes；
            非空集合则与 need_episodes 取交集；空集合（显式拒绝）会被交集自然消解为空。
            调用方根据返回集合是否为空决定是否跳过当前候选。
            """
            effective = set(_need_episodes)
            allowed = _context.allowed_episodes
            if allowed is not None:
                effective &= set(allowed)
            return effective

        def __get_movie_download_key(_context: Context) -> str:
            """
            获取电影下载去重键，确保失败候选不会阻断后续同名资源尝试。
            """
            return _context.media_info.title_year

        # 发送资源选择事件，允许外部修改上下文数据
        logger.debug(f"Initial contexts: {len(contexts)} items, Downloader: {downloader}")
        event_data = ResourceSelectionEventData(
            contexts=contexts,
            downloader=downloader,
            origin=source
        )
        event = eventmanager.send_event(ChainEventType.ResourceSelection, event_data)
        # 如果事件修改了上下文数据，使用更新后的数据
        if event and event.event_data:
            event_data: ResourceSelectionEventData = event.event_data
            if event_data.updated and event_data.updated_contexts is not None:
                logger.debug(f"Contexts updated by event: "
                             f"{len(event_data.updated_contexts)} items (source: {event_data.source})")
                contexts = event_data.updated_contexts

        # 仅排序，不提前按媒体控重；下载失败时需要继续尝试同组后续候选。
        contexts = TorrentHelper().sort_torrents(contexts)
        active_failure_fingerprints = self._active_download_failure_fingerprints(
            contexts=contexts,
            source=source,
        )

        def __is_context_in_failure_cooldown(_context: Context) -> bool:
            """
            判断候选资源是否仍处于失败冷却期。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint and fingerprint in active_failure_fingerprints:
                logger.info(f"{_context.torrent_info.title} 近期添加下载失败，暂时跳过该资源")
                return True
            return False

        def __remember_context_failure(_context: Context) -> None:
            """
            将本轮失败候选加入内存冷却集合，避免同一批次重复尝试。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint:
                active_failure_fingerprints.add(fingerprint)

        # 如果是电影，直接下载
        downloaded_movies = set()
        for context in contexts:
            if global_vars.is_system_stopped:
                break
            if context.media_info.type == MediaType.MOVIE:
                if __is_context_in_failure_cooldown(context):
                    continue
                movie_key = __get_movie_download_key(context)
                if movie_key in downloaded_movies:
                    continue
                logger.info(f"开始下载电影 {context.torrent_info.title} ...")
                if self.download_single(context, save_path=save_path, channel=channel,
                                        source=source, userid=userid, username=username,
                                        downloader=downloader, custom_words=custom_words,
                                        subscribe_id=subscribe_id, version_rule_id=version_rule_id,
                                        version_settings=version_settings):
                    # 下载成功
                    logger.info(f"{context.torrent_info.title} 添加下载成功")
                    downloaded_list.append(context)
                    downloaded_movies.add(movie_key)
                else:
                    __remember_context_failure(context)

        # 电视剧整季匹配
        if no_exists:
            logger.info(f"开始匹配电视剧整季：{no_exists}")
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子 {tmdbid: [seasons]}
            need_seasons: Dict[int, list] = {}
            for need_mid, need_tv in no_exists.items():
                for tv in need_tv.values():
                    if not tv:
                        continue
                    # 季列表为空的，代表全季缺失
                    if not tv.episodes:
                        if not need_seasons.get(need_mid):
                            need_seasons[need_mid] = []
                        need_seasons[need_mid].append(tv.season if tv.season is not None else 1)
            logger.info(f"缺失整季：{need_seasons}")
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_mid, need_season in need_seasons.items():
                # 循环种子
                for context in contexts:
                    if global_vars.is_system_stopped:
                        break
                    # 媒体信息
                    media = context.media_info
                    # 识别元数据
                    meta = context.meta_info
                    # 种子信息
                    torrent = context.torrent_info
                    # 排除电视剧
                    if media.type != MediaType.TV:
                        continue
                    # 种子的季清单
                    torrent_season = meta.season_list
                    # 没有季的默认为第1季
                    if not torrent_season:
                        torrent_season = [1]
                    # 种子有集的不要
                    if meta.episode_list:
                        continue
                    # 匹配TMDBID
                    if self._matches_media_identity(media, need_mid):
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 种子季是需要季或者子集
                        if set(torrent_season).issubset(set(need_season)):
                            complete_coverage_matched = False
                            if len(torrent_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                logger.info(f"开始下载种子 {torrent.title} ...")
                                download_errors: List[str] = []
                                content, _, torrent_files = self.download_torrent(
                                    torrent, on_failure=download_errors.append,
                                )
                                if not content:
                                    logger.warn(f"{torrent.title} 种子下载失败！")
                                    self._record_download_failure(
                                        context=context,
                                        error_msg=download_errors[-1] if download_errors else "下载种子内容为空",
                                        downloader=downloader,
                                        source=source,
                                    )
                                    __remember_context_failure(context)
                                    continue
                                if isinstance(content, str):
                                    logger.warn(f"{meta.org_string} 下载地址是磁力链，无法确定种子文件集数")
                                    continue
                                torrent_episodes = TorrentHelper().get_torrent_episodes(torrent_files)
                                logger.info(f"{meta.org_string} 解析种子文件集数为 {torrent_episodes}")
                                if not torrent_episodes:
                                    continue
                                torrent_episodes_set = set(torrent_episodes)
                                # 更新集数范围
                                begin_ep = min(torrent_episodes)
                                end_ep = max(torrent_episodes)
                                meta.set_episodes(begin=begin_ep, end=end_ep)
                                meta.set_episode_list(torrent_episodes)
                                # 需要目标集范围；完整覆盖场景必须覆盖范围内每一集，不能只按数量判断。
                                need_tv_info = __get_no_exist_media(need_mid, torrent_season[0])
                                required_episodes = __get_required_episodes(need_mid, torrent_season[0]) \
                                    if __requires_complete_coverage(need_tv_info) else set()
                                need_total = __get_season_episodes(need_mid, torrent_season[0])
                                complete_coverage_matched = bool(required_episodes) \
                                    and required_episodes.issubset(torrent_episodes_set)
                                if complete_coverage_matched:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数已完整覆盖目标范围："
                                        f"{StringUtils.format_ep(sorted(required_episodes))}")
                                if required_episodes and not complete_coverage_matched:
                                    missing_episodes = sorted(required_episodes.difference(torrent_episodes_set))
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数未覆盖目标范围，"
                                        f"缺少 {StringUtils.format_ep(missing_episodes)}，先放弃这个种子")
                                    continue
                                if not required_episodes and need_total and len(torrent_episodes) < need_total:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
                                    continue
                                else:
                                    # 整包超出完整覆盖目标时只选订阅范围内剧集。
                                    selected_episodes = required_episodes \
                                        if complete_coverage_matched and required_episodes != torrent_episodes_set else None
                                    if selected_episodes:
                                        logger.info(
                                            f"{meta.org_string} 种子包含目标范围外剧集，仅选择："
                                            f"{StringUtils.format_ep(sorted(selected_episodes))}"
                                        )
                                    logger.info(f"开始下载 {torrent.title} ...")
                                    download_id = self.download_single(
                                        context=context,
                                        torrent_content=content,
                                        episodes=selected_episodes,
                                        save_path=save_path,
                                        channel=channel,
                                        source=source,
                                        userid=userid,
                                        username=username,
                                        downloader=downloader,
                                        custom_words=custom_words,
                                        subscribe_id=subscribe_id,
                                        version_rule_id=version_rule_id,
                                        version_settings=version_settings
                                    )
                            else:
                                # 下载
                                logger.info(f"开始下载 {torrent.title} ...")
                                download_id = self.download_single(context, save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words,
                                                                   subscribe_id=subscribe_id,
                                                                   version_rule_id=version_rule_id,
                                                                   version_settings=version_settings)

                            if download_id:
                                # 下载成功
                                if complete_coverage_matched:
                                    context.confirmed_full_coverage = True
                                logger.info(f"{torrent.title} 添加下载成功")
                                downloaded_list.append(context)
                                # 更新仍需季集
                                need_season = __update_seasons(_mid=need_mid,
                                                               _need=need_season,
                                                               _current=torrent_season)
                                logger.info(f"{need_mid} 剩余需要季：{need_season}")
                                if not need_season:
                                    # 全部下载完成
                                    break
                            else:
                                __remember_context_failure(context)
        # 电视剧季内的集匹配
        if no_exists:
            logger.info(f"开始电视剧完整集匹配：{no_exists}")
            # TMDBID列表
            need_tv_list = list(no_exists)
            for need_mid in need_tv_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
                if not need_tv:
                    continue
                need_tv_copy = copy.deepcopy(no_exists.get(need_mid))
                # 循环每一季
                for sea, tv in need_tv_copy.items():
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    # TMDB总集数
                    total_episode = tv.total_episode
                    # 需要开始集
                    start_episode = tv.start_episode or 1
                    # 缺失整季的转化为缺失集进行比较
                    if not need_episodes:
                        need_episodes = list(range(start_episode, total_episode + 1))
                    # 循环种子
                    for context in contexts:
                        if global_vars.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 匹配TMDB
                        if self._matches_media_identity(media, need_mid):
                            # 不重复添加
                            if context in downloaded_list:
                                continue
                            if __is_context_in_failure_cooldown(context):
                                continue
                            # 种子季
                            torrent_season = meta.season_list
                            # 只处理单季含集的种子
                            if len(torrent_season) != 1 or torrent_season[0] != need_season:
                                continue
                            # 种子集列表
                            torrent_episodes = set(meta.episode_list)
                            # 整季的不处理
                            if not torrent_episodes:
                                continue
                            # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                            effective_need = __apply_allowed_episodes(need_episodes, context)
                            if not effective_need:
                                continue
                            if __requires_complete_coverage(tv):
                                # 完整覆盖任务要求候选集数覆盖目标范围，允许资源包含范围外的额外集。
                                required_episodes = __get_required_episodes(need_mid, need_season)
                                match_episodes = required_episodes.issubset(torrent_episodes) \
                                    if required_episodes else False
                            else:
                                # 普通缺集下载保持原语义：候选自身必须是所需集的子集。
                                match_episodes = torrent_episodes.issubset(effective_need)
                            if match_episodes:
                                selected_episodes = required_episodes if __requires_complete_coverage(tv) else None
                                logger.info(f"开始下载 {meta.title} ...")
                                download_id = self.download_single(context, episodes=selected_episodes,
                                                                   save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words,
                                                                   subscribe_id=subscribe_id,
                                                                   version_rule_id=version_rule_id,
                                                                   version_settings=version_settings)
                                if download_id:
                                    # 下载成功
                                    if __requires_complete_coverage(tv):
                                        context.confirmed_full_coverage = True
                                    logger.info(f"{meta.title} 添加下载成功")
                                    downloaded_list.append(context)
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(
                                        _mid=need_mid,
                                        _need=need_episodes,
                                        _sea=need_season,
                                        _current=selected_episodes or torrent_episodes,
                                    )
                                    logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")
                                else:
                                    __remember_context_failure(context)

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        if no_exists:
            logger.info(f"开始电视剧多集拆包匹配：{no_exists}")
            # TMDBID列表
            no_exists_list = list(no_exists)
            for need_mid in no_exists_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
                if not need_tv:
                    continue
                # 需要季列表
                need_tv_list = list(need_tv)
                # 循环需要季
                for sea in need_tv_list:
                    # NotExistMediaInfo
                    tv = need_tv.get(sea)
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    if __requires_complete_coverage(tv):
                        continue
                    # 没有集的不处理
                    if not need_episodes:
                        continue
                    # 循环种子
                    for context in contexts:
                        if global_vars.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 种子信息
                        torrent = context.torrent_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 没有需要集后退出
                        if not need_episodes:
                            break
                        # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                        effective_need = __apply_allowed_episodes(need_episodes, context)
                        if not effective_need:
                            continue
                        # 选中一个单季整季的或单季包括需要的所有集的
                        if self._matches_media_identity(media, need_mid) \
                                and (not meta.episode_list
                                     or set(meta.episode_list).intersection(effective_need)) \
                                and len(meta.season_list) == 1 \
                                and meta.season_list[0] == need_season:
                            # 检查种子看是否有需要的集
                            logger.info(f"开始下载种子 {torrent.title} ...")
                            download_errors: List[str] = []
                            content, _, torrent_files = self.download_torrent(
                                torrent, on_failure=download_errors.append,
                            )
                            if not content:
                                logger.info(f"{torrent.title} 种子下载失败！")
                                self._record_download_failure(
                                    context=context,
                                    error_msg=download_errors[-1] if download_errors else "下载种子内容为空",
                                    downloader=downloader,
                                    source=source,
                                )
                                __remember_context_failure(context)
                                continue
                            if isinstance(content, str):
                                logger.warn(f"{meta.org_string} 下载地址是磁力链，无法解析种子文件集数")
                                continue
                            # 种子全部集
                            torrent_episodes = TorrentHelper().get_torrent_episodes(torrent_files)
                            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{torrent_episodes}")
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(effective_need)
                            if not selected_episodes:
                                logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
                                continue
                            logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected_episodes}")
                            # 添加下载
                            logger.info(f"开始下载 {torrent.title} ...")
                            download_id = self.download_single(
                                context=context,
                                torrent_content=content,
                                episodes=selected_episodes,
                                save_path=save_path,
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                                downloader=downloader,
                                custom_words=custom_words,
                                subscribe_id=subscribe_id,
                                version_rule_id=version_rule_id,
                                version_settings=version_settings
                            )
                            if not download_id:
                                __remember_context_failure(context)
                                continue
                            # 下载成功
                            logger.info(f"{torrent.title} 添加下载成功")
                            downloaded_list.append(context)
                            # 更新种子集数范围
                            begin_ep = min(torrent_episodes)
                            end_ep = max(torrent_episodes)
                            meta.set_episodes(begin=begin_ep, end=end_ep)
                            meta.set_episode_list(torrent_episodes)
                            # 更新仍需集数
                            need_episodes = __update_episodes(_mid=need_mid,
                                                              _need=need_episodes,
                                                              _sea=need_season,
                                                              _current=selected_episodes)
                            logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")

        # 返回下载的资源，剩下没下完的
        logger.info(f"成功下载种子数：{len(downloaded_list)}，剩余未下载的剧集：{no_exists}")
        return downloaded_list, no_exists

    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo,
                           no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                           totals: Dict[int, int] = None
                           ) -> Tuple[bool, Dict[Union[int, str], Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询是否存在，对于剧集同时返回不存在的季集信息
        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :param totals: 电视剧每季的总集数
        :return: 当前媒体是否缺失，各标题总的季集和缺失的季集
        """
        media_source, media_id = resolve_media_identity(media=mediainfo)

        def __append_no_exists(_season: int, _episodes: list, _total: int, _start: int):
            """
            添加不存在的季集信息
            {tmdbid: [
                "season": int,
                "episodes": list,
                "total_episode": int,
                "start_episode": int
            ]}
            """
            mediakey = build_media_key(media_source, media_id)
            if not no_exists.get(mediakey):
                no_exists[mediakey] = {
                    _season: NotExistMediaInfo(
                        season=_season,
                        episodes=_episodes,
                        total_episode=_total,
                        start_episode=_start
                    )
                }
            else:
                no_exists[mediakey][_season] = NotExistMediaInfo(
                    season=_season,
                    episodes=_episodes,
                    total_episode=_total,
                    start_episode=_start
                )

        if not no_exists:
            no_exists = {}

        if not totals:
            totals = {}

        mediaserver = MediaServerOper()
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             tmdbid=mediainfo.tmdb_id)
            exists_movies: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.title_year}")
                return True, {}
            return False, {}
        else:
            if not mediainfo.seasons:
                # 补充媒体信息
                mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                            tmdbid=mediainfo.tmdb_id,
                                                            doubanid=mediainfo.douban_id,
                                                            bangumiid=mediainfo.bangumi_id,
                                                            anilistid=mediainfo.anilist_id,
                                                            source=media_source,
                                                            mediaid=media_id,
                                                            episode_group=mediainfo.episode_group)
                if not mediainfo:
                    logger.error(f"媒体信息识别失败！")
                    return False, {}
                if not mediainfo.seasons:
                    logger.error(f"媒体信息中没有季集信息：{mediainfo.title_year}")
                    return False, {}
            # 电视剧
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             tmdbid=mediainfo.tmdb_id,
                                             season=mediainfo.season)
            # 媒体库已存在的剧集
            exists_tvs: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if isinstance(exists_tvs, ExistMediaInfo) and exists_tvs.query_failed:
                logger.warning(f"媒体库季集查询失败，暂不更新缺集信息：{mediainfo.title_year}")
                return False, {}
            if not exists_tvs:
                # 所有季集均缺失
                for season, episodes in mediainfo.seasons.items():
                    if not episodes:
                        continue
                    # 全季不存在
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    # 总集数
                    total_ep = totals.get(season) or len(episodes)
                    __append_no_exists(_season=season, _episodes=[],
                                       _total=total_ep, _start=min(episodes))
                return False, no_exists
            else:
                # 存在一些，检查每季缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    if not episodes:
                        continue
                    # 该季总集数
                    season_total = totals.get(season) or len(episodes)
                    # 该季已存在的集
                    exist_episodes = exists_tvs.seasons.get(season)
                    if exist_episodes:
                        # 已存在取差集
                        if totals.get(season):
                            # 按总集数计算缺失集（开始集为TMDB中的最小集）
                            lack_episodes = list(set(range(min(episodes),
                                                           season_total + min(episodes))
                                                     ).difference(set(exist_episodes)))
                        else:
                            # 按TMDB集数计算缺失集
                            lack_episodes = list(set(episodes).difference(set(exist_episodes)))
                        if not lack_episodes:
                            # 全部集存在
                            continue
                        # 添加不存在的季集信息
                        __append_no_exists(_season=season, _episodes=lack_episodes,
                                           _total=season_total, _start=min(lack_episodes))
                    else:
                        # 全季不存在
                        __append_no_exists(_season=season, _episodes=[],
                                           _total=season_total, _start=min(episodes))
            # 存在不完整的剧集
            if no_exists:
                logger.debug(f"媒体库中已存在部分剧集，缺失：{no_exists}")
                return False, no_exists
            # 全部存在
            return True, no_exists

    _download_page_size = 5

    def remote_downloading(
            self,
            arg_str: str = "",
            channel: MessageChannel = None,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
            username: Optional[str] = None,
    ) -> None:
        """
        创建 /downloading 交互会话并展示下载任务控制台。
        """
        request = download_interaction_manager.create_or_replace(
            user_id=userid,
            command="/downloading",
            channel=channel,
            source=source,
            username=username,
        )
        normalized_arg = (arg_str or "").strip()
        if normalized_arg and self.handle_text_interaction(
                channel=channel,
                source=source,
                userid=userid,
                username=username or "",
                text=normalized_arg,
        ):
            return
        self._render_download_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str]]:
        """
        解析 /downloading 按钮回调，动作参数仍使用短哈希。
        """
        if not callback_data.startswith("downloading:"):
            return None
        parts = callback_data.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理下载控制台的翻页、任务控制和删除确认回调。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False
        request_id, action = parsed
        request = download_interaction_manager.get_by_id(request_id, userid)
        if not request:
            self.post_message(Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载交互已失效，请重新发送 /downloading",
                save_history=False,
            ))
            return True

        request.channel = channel
        request.source = source
        request.username = username
        if action == "close":
            download_interaction_manager.remove(request.request_id)
            update_or_post_message(
                chain=self,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载管理",
                text="下载交互已结束",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        notice = None
        action_name, _, short_hash = action.partition(":")
        if action == "page-prev":
            request.page = max(0, request.page - 1)
            request.awaiting_input = None
        elif action == "page-next":
            request.page += 1
            request.awaiting_input = None
        elif action in {"refresh", "cancel"}:
            request.awaiting_input = None
        elif action_name in {"toggle", "pause", "resume", "askdel", "cfmdel"}:
            torrents = self._get_download_queue()
            if action_name == "cfmdel":
                notice = self._confirm_delete(
                    request=request,
                    torrents=torrents,
                    short_hash=short_hash,
                )
                request.awaiting_input = None
            else:
                torrent = self._find_torrent_by_short_hash(torrents, short_hash)
                if not torrent:
                    request.awaiting_input = None
                    notice = "未找到对应下载任务，列表已刷新"
                elif action_name == "askdel":
                    request.awaiting_input = f"confirm_delete:{torrent.hash}"
                else:
                    request.awaiting_input = None
                    if action_name == "toggle":
                        oper = (
                            "start"
                            if torrent.state == DownloadTaskState.PAUSED.value
                            else "stop"
                        )
                    else:
                        oper = "start" if action_name == "resume" else "stop"
                    notice = self._change_torrent_state(torrent, oper)

        self._render_download_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            notice=notice,
        )
        return True

    def handle_text_interaction(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> bool:
        """
        处理纯文本渠道的下载任务控制指令。
        """
        request = download_interaction_manager.get_by_user(userid)
        if not request:
            return False
        request.channel = channel
        request.source = source
        request.username = username

        normalized = (text or "").strip()
        lowered = normalized.lower()
        if lowered in {"q", "退出", "关闭", "quit", "exit"}:
            download_interaction_manager.remove(request.request_id)
            self.post_message(Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载交互已结束",
                save_history=False,
            ))
            return True

        notice = None
        if lowered in {"n", "next", "下一页"}:
            request.page += 1
            request.awaiting_input = None
        elif lowered in {"b", "prev", "上一页"}:
            request.page = max(0, request.page - 1)
            request.awaiting_input = None
        elif lowered in {"f", "刷新", "refresh", "列表", "list"}:
            request.awaiting_input = None
        elif lowered in {"取消", "cancel", "返回", "back"}:
            request.awaiting_input = None
        elif (
                request.awaiting_input
                and request.awaiting_input.startswith("confirm_delete:")
                and lowered in {"确认", "确认删除", "y", "yes", "confirm"}
        ):
            full_hash = request.awaiting_input.split(":", 1)[1]
            notice = self._confirm_delete(
                request=request,
                torrents=self._get_download_queue(),
                short_hash=full_hash[:10],
            )
            request.awaiting_input = None
        else:
            command_match = re.fullmatch(r"([prd])\s+(\d+)", lowered)
            if not command_match:
                self.post_message(Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="下载管理",
                    text=self._download_usage_hint(),
                    save_history=False,
                ))
                return True
            command, index_text = command_match.groups()
            torrents = self._get_download_queue()
            index = int(index_text)
            torrent = torrents[index - 1] if 0 < index <= len(torrents) else None
            if not torrent or not torrent.hash:
                notice = f"未找到序号 {index} 对应的下载任务"
            elif command == "d":
                request.awaiting_input = f"confirm_delete:{torrent.hash}"
            else:
                request.awaiting_input = None
                notice = self._change_torrent_state(
                    torrent,
                    "stop" if command == "p" else "start",
                )

        self._render_download_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            notice=notice,
        )
        return True

    def _render_download_interaction(
            self,
            request: PendingSlashInteraction,
            channel: MessageChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            notice: Optional[str] = None,
    ) -> None:
        """
        查询最新队列并原地渲染下载控制台当前页面。
        """
        torrents = self._get_download_queue()
        page_torrents, page, total_pages = page_items(
            torrents,
            request.page,
            self._download_page_size,
        )
        request.page = page
        sections = [notice] if notice else []
        awaiting_torrent = self._get_awaiting_torrent(request, torrents)
        if awaiting_torrent:
            sections.append(
                f"⚠️ 确认删除任务：{self._download_torrent_name(awaiting_torrent)}\n"
                "此操作会删除下载任务及文件，请确认。"
            )
        if page_torrents:
            sections.append(self._format_download_list(
                page_torrents,
                channel=channel,
                start_index=page * self._download_page_size + 1,
            ))
        else:
            sections.append("当前下载队列没有未完成任务。")
        sections.append(f"第 {page + 1}/{total_pages} 页，共 {len(torrents)} 个任务")
        if not supports_interaction_buttons(channel):
            sections.append(self._download_usage_hint())

        buttons = None
        if supports_interaction_buttons(channel):
            if awaiting_torrent:
                buttons = [[
                    {
                        "text": "⚠️ 确认删除任务",
                        "callback_data": (
                            f"downloading:{request.request_id}:cfmdel:"
                            f"{awaiting_torrent.hash[:10]}"
                        ),
                    },
                    {
                        "text": "↩️ 取消",
                        "callback_data": f"downloading:{request.request_id}:cancel",
                    },
                ]]
            else:
                buttons = self._build_download_buttons(
                    request=request,
                    torrents=page_torrents,
                    page=page,
                    total_pages=total_pages,
                )

        update_or_post_message(
            chain=self,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="下载管理",
            text="\n\n".join(sections),
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _build_download_buttons(
            self,
            request: PendingSlashInteraction,
            torrents: List[DownloaderTorrent],
            page: int,
            total_pages: int,
    ) -> List[List[dict]]:
        """
        构造任务控制、翻页、刷新和关闭按钮，回调仅携带十位短哈希。
        """
        buttons = []
        for index, torrent in enumerate(torrents, start=page * self._download_page_size + 1):
            if not torrent.hash:
                continue
            short_hash = torrent.hash[:10]
            paused = torrent.state == DownloadTaskState.PAUSED.value
            buttons.append([
                {
                    "text": f"{'▶️ 继续' if paused else '⏸️ 暂停'} {index}",
                    "callback_data": f"downloading:{request.request_id}:toggle:{short_hash}",
                },
                {
                    "text": f"🗑️ 删除 {index}",
                    "callback_data": f"downloading:{request.request_id}:askdel:{short_hash}",
                },
            ])
        buttons.extend(build_navigation_buttons(
            "downloading",
            request,
            page,
            total_pages,
        ))
        buttons.append([
            {
                "text": "🔄 刷新",
                "callback_data": f"downloading:{request.request_id}:refresh",
            },
            {
                "text": "关闭",
                "callback_data": f"downloading:{request.request_id}:close",
            },
        ])
        return buttons

    def _get_download_queue(self) -> List[DownloaderTorrent]:
        """
        合并活动下载和暂停中的未完成任务，并避免重复任务。
        """
        torrents = list(self.downloading() or [])
        existing = {(torrent.downloader, torrent.hash) for torrent in torrents}
        paused_torrents = self.list_torrents(status=TorrentQueryStatus.PAUSED) or []
        pending_paused = [
            torrent
            for torrent in paused_torrents
            if (torrent.progress or 0) < 100
            and (torrent.downloader, torrent.hash) not in existing
        ]
        self._attach_download_history(pending_paused)
        torrents.extend(pending_paused)
        return torrents

    def _change_torrent_state(self, torrent: DownloaderTorrent, oper: str) -> str:
        """
        暂停或继续任务并返回面板顶部操作结果。
        """
        success = self.set_downloading(torrent.hash, oper, name=torrent.downloader)
        name = self._download_torrent_name(torrent)
        if oper == "start":
            return f"✅ 已继续：{name}" if success else f"❌ 继续失败：{name}"
        return f"✅ 已暂停：{name}" if success else f"❌ 暂停失败：{name}"

    def _confirm_delete(
            self,
            request: PendingSlashInteraction,
            torrents: List[DownloaderTorrent],
            short_hash: str,
    ) -> str:
        """
        校验删除确认态后按完整哈希删除任务并返回操作结果。
        """
        awaiting_input = request.awaiting_input or ""
        if not awaiting_input.startswith("confirm_delete:"):
            return "删除确认已失效，请重新选择任务"
        full_hash = awaiting_input.split(":", 1)[1]
        if not short_hash or not full_hash.lower().startswith(short_hash.lower()):
            return "删除确认与所选任务不一致，请重新选择"
        torrent = next(
            (
                item
                for item in torrents
                if item.hash and item.hash.lower() == full_hash.lower()
            ),
            None,
        )
        if not torrent:
            return "任务已不在下载队列中，列表已刷新"
        success = self.remove_torrents(hashs=full_hash, downloader=torrent.downloader)
        name = self._download_torrent_name(torrent)
        return f"✅ 已删除：{name}" if success else f"❌ 删除失败：{name}"

    @staticmethod
    def _get_awaiting_torrent(
            request: PendingSlashInteraction,
            torrents: List[DownloaderTorrent],
    ) -> Optional[DownloaderTorrent]:
        """
        返回当前等待删除确认的任务。
        """
        awaiting_input = request.awaiting_input or ""
        if not awaiting_input.startswith("confirm_delete:"):
            return None
        full_hash = awaiting_input.split(":", 1)[1].lower()
        return next(
            (
                torrent
                for torrent in torrents
                if torrent.hash and torrent.hash.lower() == full_hash
            ),
            None,
        )

    @staticmethod
    def _find_torrent_by_short_hash(
            torrents: List[DownloaderTorrent],
            short_hash: str,
    ) -> Optional[DownloaderTorrent]:
        """
        使用十位短哈希唯一匹配当前队列任务。
        """
        if not short_hash:
            return None
        matches = [
            torrent
            for torrent in torrents
            if torrent.hash and torrent.hash.lower().startswith(short_hash.lower())
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _download_torrent_name(torrent: DownloaderTorrent) -> str:
        """
        优先返回媒体标题和季集，否则返回种子标题。
        """
        media = torrent.media or {}
        if media.get("title"):
            return " ".join(
                str(value)
                for value in (
                    media.get("title"),
                    media.get("season"),
                    media.get("episode"),
                )
                if value
            )
        return torrent.title or torrent.name or torrent.hash or "未命名任务"

    def _format_download_list(
            self,
            torrents: List[DownloaderTorrent],
            channel: Optional[MessageChannel],
            start_index: int,
    ) -> str:
        """
        按全局序号展示任务标题、来源、体积、进度、速率和剩余时间。
        """
        lines = []
        use_markdown = supports_markdown(channel)
        for index, torrent in enumerate(torrents, start=start_index):
            name = self._download_torrent_name(torrent)
            lines.append(f"{index}. **{name}**" if use_markdown else f"{index}. {name}")
            if torrent.media and torrent.media.get("title"):
                lines.append(f"   种子：{torrent.title or torrent.name or '-'}")
            state_text = (
                "⏸️ 已暂停"
                if torrent.state == DownloadTaskState.PAUSED.value
                else "⬇️ 下载中"
            )
            lines.append(
                f"   下载器：{torrent.downloader or '-'} | 站点：{torrent.site_name or '-'}"
            )
            lines.append(
                f"   大小：{StringUtils.str_filesize(torrent.size or 0)}"
                f" | 进度：{float(torrent.progress or 0):.1f}% | 状态：{state_text}"
            )
            lines.append(
                f"   下载：{StringUtils.str_filesize(torrent.dlspeed or 0)}/s"
                f" | 上传：{StringUtils.str_filesize(torrent.upspeed or 0)}/s"
                f" | 剩余：{torrent.left_time or '未知'}"
            )
        return "\n".join(lines)

    @staticmethod
    def _download_usage_hint() -> str:
        """
        返回纯文本下载控制台指令说明。
        """
        return (
            "指令：`p <序号>` 暂停，`r <序号>` 继续，`d <序号>` 删除；"
            "删除后输入 `确认删除`，输入 `取消` 放弃。\n"
            "`n` 下一页，`b` 上一页，`f` 刷新，`q` / `退出` / `关闭` 结束会话。"
        )

    @staticmethod
    def _attach_download_history(torrents: List[DownloaderTorrent]) -> None:
        """
        为队列任务补充下载历史中的媒体和站点信息。
        """
        if not torrents:
            return
        history_map = DownloadHistoryOper().get_by_hashes(
            [torrent.hash for torrent in torrents if torrent.hash]
        )
        for torrent in torrents:
            history = history_map.get(torrent.hash)
            if not history:
                continue
            torrent.media = {
                "tmdbid": history.tmdbid,
                "type": history.type,
                "title": history.title,
                "season": history.seasons,
                "episode": history.episodes,
                "image": history.poster,
                "poster": history.poster,
                "backdrop": history.image,
            }
            torrent.site_name = history.torrent_site
            torrent.userid = history.userid
            torrent.username = history.username

    def downloading(self, name: Optional[str] = None) -> List[DownloaderTorrent]:
        """
        查询正在下载的任务
        """
        torrents = self.list_torrents(downloader=name, status=TorrentStatus.DOWNLOADING)
        if not torrents:
            return []

        self._attach_download_history(torrents)
        return torrents

    def set_downloading(self, hash_str, oper: str, name: Optional[str] = None) -> bool:
        """
        控制下载任务 start/stop
        """
        if oper == "start":
            return self.start_torrents(hashs=[hash_str], downloader=name)
        elif oper == "stop":
            return self.stop_torrents(hashs=[hash_str], downloader=name)
        return False

    def remove_downloading(self, hash_str: str, name: Optional[str] = None) -> bool:
        """
        删除下载任务
        """
        return self.remove_torrents(hashs=[hash_str], downloader=name)

    @eventmanager.register(EventType.DownloadFileDeleted)
    def download_file_deleted(self, event: Event):
        """
        下载文件删除时，同步删除下载任务
        """
        if not event:
            return
        hash_str = event.event_data.get("hash")
        if not hash_str:
            return
        logger.warn(f"检测到下载源文件被删除，删除下载任务（不含文件）：{hash_str}")
        # 先查询种子
        torrents: List[schemas.DownloaderTorrent] = self.list_torrents(hashs=[hash_str])
        if torrents:
            self.remove_torrents(hashs=[hash_str], delete_file=False)
            # 发出下载任务删除事件，如需处理辅种，可监听该事件
            self.eventmanager.send_event(EventType.DownloadDeleted, {
                "hash": hash_str,
                    "torrents": [torrent.model_dump() for torrent in torrents]
            })
        else:
            logger.info(f"没有在下载器中查询到 {hash_str} 对应的下载任务")
