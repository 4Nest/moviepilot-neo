import asyncio
import base64
import json
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils


class HDAreaSpider:
    """
    HDArea API v1 索引器，负责种子搜索、两段式短效下载与外挂字幕检索
    """

    _size = 100
    _DEFAULT_RETRY_AFTER = 2.0
    _MAX_RETRY_AFTER = 5.0
    _IMDB_RE = re.compile(r"^tt\d+$", re.IGNORECASE)
    _MOVIE_ONLY_CATEGORY_IDS = {300, 401, 410, 411, 412, 413, 414, 415, 416, 417}
    _TV_ONLY_CATEGORY_IDS = {402, 403}
    _DEFAULT_MOVIE_CATEGORIES = [
        "300", "401", "415", "416", "410", "411",
        "414", "412", "413", "417", "405", "404",
    ]
    _DEFAULT_TV_CATEGORIES = ["402", "403", "404", "405"]
    _TAG_LABELS = {
        "sticky": "置顶",
        "hot": "热门",
        "gf": "官方",
        "official": "官方",
        "gy": "国语",
        "yy": "粤语",
        "ja": "日语",
        "ko": "韩语",
        "zz": "中字",
        "diy": "DIY",
        "hdr10": "HDR10",
        "hdr10+": "HDR10+",
        "dv": "Dolby Vision",
        "dolby_vision": "Dolby Vision",
        "hlg": "HLG",
    }

    def __init__(self, indexer: dict):
        """
        初始化 HDArea API 索引器

        :param indexer: 合并站点认证配置后的索引器字典
        """
        indexer = indexer or {}
        self._indexer_id = indexer.get("id")
        self._name = indexer.get("name") or "高清视界"
        site_url = str(indexer.get("domain") or indexer.get("url") or "https://hdarea.club/").strip()
        if not site_url.endswith("/"):
            site_url = f"{site_url}/"
        self._site_url = site_url
        api_url = indexer.get("api_url") or f"{self._site_url.rstrip('/')}/api/v1"
        self._api_url = str(api_url).rstrip("/")
        self._use_proxy = bool(indexer.get("proxy"))
        self._proxy = settings.PROXY if self._use_proxy else None
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._api_key = (indexer.get("apikey") or "").strip()
        self._timeout = int(indexer.get("timeout") or 15)
        self._configured_categories = self._parse_configured_categories(
            indexer.get("category") or {}
        )

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量

        :param keyword: 搜索关键词，HDArea 不按关键词改变分页容量
        :return: 单页最大返回数量
        """
        return cls._size

    @classmethod
    def _parse_configured_categories(cls, category_config: dict) -> dict:
        """
        从站点分类配置提取电影和剧集分类 ID 列表

        :param category_config: 站点配置中的 category 字典
        :return: 包含 movie 与 tv 分类 ID 字符串列表的字典
        """
        category_map = {
            "movie": [],
            "tv": [],
        }
        for media_key in ("movie", "tv"):
            items = category_config.get(media_key) if isinstance(category_config, dict) else None
            for item in items or []:
                cat_id = item.get("id") if isinstance(item, dict) else item
                if cat_id is not None:
                    cat_str = str(cat_id).strip()
                    if cat_str and cat_str not in category_map[media_key]:
                        category_map[media_key].append(cat_str)
        if not category_map["movie"]:
            category_map["movie"] = list(cls._DEFAULT_MOVIE_CATEGORIES)
        if not category_map["tv"]:
            category_map["tv"] = list(cls._DEFAULT_TV_CATEGORIES)
        return category_map

    def _request_headers(self) -> dict:
        """
        构造 HDArea API 请求头，仅通过 X-API-Key 鉴权避免过期 Cookie 干扰

        :return: HTTP 请求头字典
        """
        return {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
            "X-API-Key": self._api_key,
        }

    @classmethod
    def _parse_retry_after(cls, response) -> float:
        """
        提取 HTTP 429 Retry-After 秒数并夹紧在合理区间，避免长时间阻塞搜索

        :param response: HTTP 响应对象
        :return: 等待秒数
        """
        headers = getattr(response, "headers", None) or {}
        raw_val = headers.get("Retry-After") or headers.get("retry-after")
        if raw_val is not None:
            try:
                seconds = float(str(raw_val).strip())
                return min(max(seconds, 1.0), cls._MAX_RETRY_AFTER)
            except (TypeError, ValueError):
                pass
        return cls._DEFAULT_RETRY_AFTER

    def _get_with_retry(self, url: str, params: dict, operation: str):
        """
        同步发送 GET 请求，遇到 HTTP 429 限流时按 Retry-After 短暂等待并重试一次

        :param url: 请求地址
        :param params: 查询参数
        :param operation: 日志操作名称
        :return: HTTP 响应对象
        """
        req = RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        )
        response = req.get_res(url=url, params=params)
        if response is not None and response.status_code == 429:
            delay = self._parse_retry_after(response)
            logger.info(f"{self._name} {operation}触发频率限制 (429)，等待 {delay:.1f} 秒后自动重试")
            time.sleep(delay)
            response = req.get_res(url=url, params=params)
        return response

    async def _async_get_with_retry(self, url: str, params: dict, operation: str):
        """
        异步发送 GET 请求，遇到 HTTP 429 限流时按 Retry-After 短暂等待并重试一次

        :param url: 请求地址
        :param params: 查询参数
        :param operation: 日志操作名称
        :return: HTTP 响应对象
        """
        req = AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        )
        response = await req.get_res(url=url, params=params)
        if response is not None and response.status_code == 429:
            delay = self._parse_retry_after(response)
            logger.info(f"{self._name} {operation}触发频率限制 (429)，等待 {delay:.1f} 秒后自动重试")
            await asyncio.sleep(delay)
            response = await req.get_res(url=url, params=params)
        return response

    def _response_data(self, response, operation: str) -> Optional[dict]:
        """
        校验 HDArea API 通用包裹响应并提取 data 字段

        :param response: RequestUtils 返回的响应对象
        :param operation: 日志中展示的操作名称
        :return: 成功时返回 data 字典，失败时返回 None
        """
        if response is None:
            logger.warning(f"{self._name} {operation}失败，无法连接 {self._site_url}")
            return None
        if response.status_code != 200:
            detail = ""
            try:
                err_payload = response.json() or {}
                if isinstance(err_payload, dict) and err_payload.get("message"):
                    detail = f"（{err_payload.get('message')}）"
            except Exception:
                detail = ""
            logger.warning(
                f"{self._name} {operation}失败，HTTP 错误码：{response.status_code}{detail}"
            )
            return None
        try:
            payload = response.json() or {}
        except (TypeError, ValueError) as err:
            logger.warning(f"{self._name} {operation}响应不是有效 JSON：{str(err)}")
            return None
        if not isinstance(payload, dict):
            logger.warning(f"{self._name} {operation}响应结构无效")
            return None
        if payload.get("code") != 0 and str(payload.get("code")) != "0":
            logger.warning(
                f"{self._name} {operation}失败：{payload.get('message') or '未知错误'}"
            )
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _build_search_params(
        self,
        keyword: Optional[str | list] = None,
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> dict:
        """
        构造 HDArea /api/v1/torrents 查询参数；该接口 page 从 0 开始计数

        :param keyword: 搜索关键词、IMDb ID 或关键词列表
        :param mtype: 媒体类型
        :param cat: 显式指定的分类 ID（逗号分隔）
        :param page: 从 0 开始的页码
        :return: 查询参数字典
        """
        params = {
            "page": max(int(page or 0), 0),
            "per_page": self._size,
            "incldead": 1,
        }
        if isinstance(keyword, list):
            words = [str(word).strip() for word in keyword if str(word).strip()]
            if words:
                params["q"] = " ".join(words)
                params["search_area"] = 0
                params["search_mode"] = 1
        elif isinstance(keyword, str) and keyword.strip():
            clean_keyword = keyword.strip()
            if self._IMDB_RE.fullmatch(clean_keyword):
                params["imdb"] = clean_keyword.lower()
            else:
                params["q"] = clean_keyword
                params["search_area"] = 0
                params["search_mode"] = 0

        if cat:
            params["cat"] = str(cat).strip()
        elif mtype == MediaType.MOVIE:
            params["cat"] = ",".join(self._configured_categories["movie"])
        elif mtype == MediaType.TV:
            params["cat"] = ",".join(self._configured_categories["tv"])
        return params

    @classmethod
    def _resolve_media_category(cls, item: dict) -> str:
        """
        根据站点分类 ID 与 Torznab 分类号映射 MoviePilot 媒体类型

        :param item: 单条种子字典
        :return: MediaType 对应的字符串值
        """
        try:
            cat_id = int(item.get("category_id") or 0)
        except (TypeError, ValueError):
            cat_id = 0
        if cat_id in cls._MOVIE_ONLY_CATEGORY_IDS:
            return MediaType.MOVIE.value
        if cat_id in cls._TV_ONLY_CATEGORY_IDS:
            return MediaType.TV.value
        torznab_cat = item.get("torznab_cat")
        if torznab_cat == 2000:
            return MediaType.MOVIE.value
        if torznab_cat == 5000:
            return MediaType.TV.value
        return MediaType.UNKNOWN.value

    @classmethod
    def _extract_labels(cls, item: dict) -> List[str]:
        """
        提取并中文化种子的属性标签

        :param item: 单条种子字典
        :return: 去重后的标签列表
        """
        labels: List[str] = []
        raw_tags = item.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [seg.strip() for seg in re.split(r"[;,|]", raw_tags) if seg.strip()]
        for raw_tag in raw_tags:
            tag_str = str(raw_tag).strip()
            if not tag_str:
                continue
            mapped = cls._TAG_LABELS.get(tag_str.lower(), tag_str)
            if mapped not in labels:
                labels.append(mapped)
        if item.get("first_upload") and "首发" not in labels:
            labels.append("首发")
        if item.get("forbid_transfer") and "禁转" not in labels:
            labels.append("禁转")
        return labels

    @staticmethod
    def _normalize_imdb_id(imdb_value: Optional[str]) -> Optional[str]:
        """
        提取标准 tt 前缀的 IMDb 编号

        :param imdb_value: 接口返回的 imdb_id 或链接
        :return: 标准 IMDb ID，不存在时返回 None
        """
        if not imdb_value:
            return None
        match = re.search(r"tt\d+", str(imdb_value), re.IGNORECASE)
        return match.group(0).lower() if match else None

    def _build_download_url(self, torrent_id: int) -> str:
        """
        构造两段式下载链接，在实际下载时调用 genDlToken 实时签发短效下载凭证

        :param torrent_id: 种子 ID
        :return: [base64_config]url 格式的两段式下载链接
        """
        token_url = f"{self._api_url}/torrents/genDlToken"
        request_config = {
            "method": "post",
            "cookie": False,
            "params": {
                "id": torrent_id,
            },
            "header": {
                "Accept": "application/json",
                "User-Agent": self._user_agent,
                "X-API-Key": self._api_key,
            },
            "proxy": self._use_proxy,
            "result": "data.download_url",
            "result_base_url": self._site_url,
        }
        encoded_config = base64.b64encode(
            json.dumps(request_config).encode("utf-8")
        ).decode("ascii")
        return f"[{encoded_config}]{token_url}"

    def _parse_torrents(self, items: List[dict]) -> List[dict]:
        """
        将 HDArea 种子列表转换为 MoviePilot 标准种子字典列表

        :param items: 接口返回的 data.items 数组
        :return: 标准种子字典列表
        """
        torrents: List[dict] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            torrent_id = item.get("id")
            title = item.get("name")
            if torrent_id is None or not title:
                continue
            dl_factor_raw = item.get("download_volume_factor")
            ul_factor_raw = item.get("upload_volume_factor")
            dl_factor = float(dl_factor_raw) if dl_factor_raw is not None else 1.0
            ul_factor = float(ul_factor_raw) if ul_factor_raw is not None else 1.0
            promo_until = item.get("promotion_until")
            freedate = (
                StringUtils.unify_datetime_str(promo_until)
                if promo_until and not str(promo_until).startswith("0000-00-00")
                else None
            )
            page_url = item.get("detail_url") or f"{self._site_url}details.php?id={torrent_id}"
            torrents.append({
                "title": title,
                "description": item.get("small_descr") or "",
                "enclosure": self._build_download_url(torrent_id),
                "pubdate": StringUtils.unify_datetime_str(item.get("added")),
                "size": int(item.get("size") or 0),
                "seeders": int(item.get("seeders") or 0),
                "peers": int(item.get("leechers") or 0),
                "grabs": int(item.get("times_completed") or 0),
                "downloadvolumefactor": dl_factor,
                "uploadvolumefactor": ul_factor,
                "freedate": freedate,
                "page_url": page_url,
                "imdbid": self._normalize_imdb_id(item.get("imdb_id")),
                "labels": self._extract_labels(item),
                "hit_and_run": bool(item.get("hr") or item.get("hit_and_run")),
                "category": self._resolve_media_category(item),
            })
        return torrents

    def _process_search_response(self, response) -> Tuple[bool, List[dict]]:
        """
        处理种子搜索响应

        :param response: HTTP 响应对象
        :return: (是否出错, 种子列表)
        """
        data = self._response_data(response, "搜索")
        if data is None:
            return True, []
        return False, self._parse_torrents(data.get("items") or [])

    def search(
        self,
        keyword: Optional[str] = None,
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        同步搜索 HDArea 种子资源

        :param keyword: 搜索关键词或 IMDb ID
        :param mtype: 媒体类型
        :param cat: 分类 ID
        :param page: 从 0 开始的页码
        :return: (是否出错, 种子列表)
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key，无法执行搜索")
            return True, []
        params = self._build_search_params(keyword=keyword, mtype=mtype, cat=cat, page=page)
        response = self._get_with_retry(
            url=f"{self._api_url}/torrents",
            params=params,
            operation="搜索",
        )
        return self._process_search_response(response)

    async def async_search(
        self,
        keyword: Optional[str] = None,
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索 HDArea 种子资源

        :param keyword: 搜索关键词或 IMDb ID
        :param mtype: 媒体类型
        :param cat: 分类 ID
        :param page: 从 0 开始的页码
        :return: (是否出错, 种子列表)
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key，无法执行搜索")
            return True, []
        params = self._build_search_params(keyword=keyword, mtype=mtype, cat=cat, page=page)
        response = await self._async_get_with_retry(
            url=f"{self._api_url}/torrents",
            params=params,
            operation="搜索",
        )
        return self._process_search_response(response)

    def _parse_subtitles(self, items: List[dict]) -> List[dict]:
        """
        将 HDArea 字幕条目转换为 MoviePilot SubtitleInfo 兼容字典

        :param items: 接口返回的字幕 items 数组
        :return: 标准字幕字典列表
        """
        subtitles: List[dict] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            download_url = item.get("download_url")
            title = item.get("title") or item.get("filename")
            language = item.get("language") or "未知"
            size = int(item.get("size") or 0)
            if not download_url or not title or size <= 0:
                continue
            torrent_id = item.get("torrent_id")
            sub_id = item.get("id")
            subtitles.append({
                "title": title,
                "description": item.get("filename") or "",
                "enclosure": download_url,
                "page_url": (
                    f"{self._site_url}details.php?id={torrent_id}"
                    if torrent_id is not None
                    else self._site_url
                ),
                "language": language,
                "size": size,
                "pubdate": StringUtils.unify_datetime_str(item.get("added")),
                "grabs": int(item.get("hits") or 0),
                "uploader": item.get("uploader"),
                "torrent_id": str(torrent_id) if torrent_id is not None else None,
                "subtitle_id": str(sub_id) if sub_id is not None else None,
                "file_name": item.get("filename"),
            })
        return subtitles

    def search_subtitles(
        self,
        keyword: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        同步搜索 HDArea 外挂字幕列表

        :param keyword: 搜索关键词
        :param page: 从 0 开始的页码
        :return: (是否出错, 字幕字典列表)
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key，无法搜索字幕")
            return True, []
        params = {
            "page": max(int(page or 0), 0),
            "per_page": self._size,
        }
        if keyword:
            params["q"] = str(keyword).strip()
        response = self._get_with_retry(
            url=f"{self._api_url}/subtitles",
            params=params,
            operation="字幕搜索",
        )
        data = self._response_data(response, "字幕搜索")
        if data is None:
            return True, []
        return False, self._parse_subtitles(data.get("items") or [])

    async def async_search_subtitles(
        self,
        keyword: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索 HDArea 外挂字幕列表

        :param keyword: 搜索关键词
        :param page: 从 0 开始的页码
        :return: (是否出错, 字幕字典列表)
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key，无法搜索字幕")
            return True, []
        params = {
            "page": max(int(page or 0), 0),
            "per_page": self._size,
        }
        if keyword:
            params["q"] = str(keyword).strip()
        response = await self._async_get_with_retry(
            url=f"{self._api_url}/subtitles",
            params=params,
            operation="字幕搜索",
        )
        data = self._response_data(response, "字幕搜索")
        if data is None:
            return True, []
        return False, self._parse_subtitles(data.get("items") or [])

    @staticmethod
    def _extract_torrent_id(page_url: str) -> Optional[str]:
        """
        从种子详情页 URL 中提取数字种子 ID

        :param page_url: 详情页地址（如 details.php?id=12345）
        :return: 种子 ID 字符串，无法提取时返回 None
        """
        if not page_url:
            return None
        parsed = urlparse(page_url)
        query_ids = parse_qs(parsed.query).get("id") or []
        for candidate in query_ids:
            if str(candidate).strip().isdigit():
                return str(candidate).strip()
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return tail
        return None

    def get_subtitle_links(self, page_url: str) -> List[str]:
        """
        根据种子详情页地址获取关联的外挂字幕下载直链

        :param page_url: 种子详情页地址
        :return: 字幕下载直链列表
        """
        torrent_id = self._extract_torrent_id(page_url)
        if not torrent_id:
            return []
        return self.get_subtitle_links_by_id(torrent_id)

    def get_subtitle_links_by_id(self, torrent_id: str) -> List[str]:
        """
        根据种子 ID 查询所有关联的外挂字幕短效下载链接

        :param torrent_id: 种子 ID
        :return: 字幕下载直链列表
        """
        if not self._api_key or not torrent_id:
            return []
        response = self._get_with_retry(
            url=f"{self._api_url}/subtitles",
            params={"torrent_id": torrent_id},
            operation="获取种子字幕",
        )
        data = self._response_data(response, "获取种子字幕")
        if not data:
            return []
        links: List[str] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            dl_url = item.get("download_url")
            if dl_url and dl_url not in links:
                links.append(dl_url)
        return links
