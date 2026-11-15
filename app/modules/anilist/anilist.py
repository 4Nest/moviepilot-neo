import asyncio
import json
import threading
from datetime import date
from math import isfinite
from time import time
from typing import Any, Optional

from app.core.cache import AsyncFileCache, FileCache, cached
from app.core.config import settings
from app.log import logger
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.url import UrlUtils


class AniListApi:
    """
    通过官方 GraphQL 接口查询 AniList，并结合本地持久化缓存补充中文标题。

    中文标题映射表默认缓存 24 小时，远端下载失败时保留旧缓存并在 5 分钟后重试。
    """

    _official_url = "https://graphql.anilist.co"
    _translations_url = (
        "https://raw.githubusercontent.com/soruly/anilist-chinese/"
        "master/anilist-chinese.json"
    )
    _translations_ttl = 24 * 60 * 60
    _translations_retry = 5 * 60
    _translations_region = "anilist"
    _translations_key = "anilist-chinese.json"
    _translations_sync_lock = threading.Lock()
    _translations_async_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
    _media_summary_fields = """
        id
        idMal
        title { romaji english native }
        format
        status
        description(asHtml: false)
        startDate { year month day }
        endDate { year month day }
        season
        seasonYear
        episodes
        duration
        countryOfOrigin
        coverImage { extraLarge large }
        bannerImage
        genres
        synonyms
        averageScore
        popularity
        isAdult
        siteUrl
        studios(isMain: true) { nodes { name } }
    """
    _media_fields = f"""
        {_media_summary_fields}
        staff(perPage: 25, sort: [RELEVANCE]) {{
          edges {{ role node {{ id name {{ full native }} image {{ large }} siteUrl }} }}
        }}
        characters(perPage: 25, sort: [ROLE, RELEVANCE]) {{
          edges {{
            role
            node {{ id name {{ full native }} image {{ large }} siteUrl }}
            voiceActors(language: JAPANESE, sort: [RELEVANCE]) {{
              id
              name {{ full native alternative }}
              image {{ large medium }}
              siteUrl
            }}
          }}
        }}
        externalLinks {{ site url type }}
    """
    _page_query = f"""
        query (
          $page: Int!,
          $count: Int!,
          $search: String,
          $genre: String,
          $format: MediaFormat,
          $season: MediaSeason,
          $seasonYear: Int,
          $status: MediaStatus,
          $country: CountryCode,
          $sort: [MediaSort]
        ) {{
          Page(page: $page, perPage: $count) {{
            media(
              search: $search,
              type: ANIME,
              genre: $genre,
              format: $format,
              season: $season,
              seasonYear: $seasonYear,
              status: $status,
              countryOfOrigin: $country,
              isAdult: false,
              sort: $sort
            ) {{ {_media_summary_fields} }}
          }}
        }}
    """
    _media_by_ids_query = f"""
        query ($ids: [Int!]!, $count: Int!) {{
          Page(page: 1, perPage: $count) {{
            media(id_in: $ids, type: ANIME) {{ {_media_summary_fields} }}
          }}
        }}
    """

    def __init__(self) -> None:
        """初始化同步与异步请求客户端及中文标题文件缓存。"""
        headers = {
            "User-Agent": settings.NORMAL_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._request = RequestUtils(
            proxies=settings.PROXY,
            headers=headers,
        )
        self._async_request = AsyncRequestUtils(
            proxies=settings.PROXY,
            headers=headers,
        )
        self._translations: Optional[dict[Any, dict[str, Any]]] = None
        self._translations_expires_at = 0.0
        self._file_cache: Optional[FileCache] = None
        self._async_file_cache: Optional[AsyncFileCache] = None
        try:
            self._file_cache = FileCache(base=settings.CACHE_PATH)
            self._async_file_cache = AsyncFileCache(base=settings.CACHE_PATH)
        except OSError as err:
            logger.warning(f"初始化 anilist-chinese 磁盘缓存失败：{err}")

    @classmethod
    def _get_async_lock(cls) -> asyncio.Lock:
        """
        按当前事件循环获取中文标题异步缓存刷新锁。
        """
        loop = asyncio.get_running_loop()
        with cls._translations_sync_lock:
            lock = cls._translations_async_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                cls._translations_async_locks[loop] = lock
            return lock

    @staticmethod
    def _extract_response(response) -> Optional[dict]:
        """
        提取 GraphQL 响应数据并统一处理上游错误。

        :param response: HTTP 响应对象
        :return: GraphQL data 字段
        """
        if response is None or response.status_code != 200:
            return None
        try:
            result = response.json()
        except Exception as err:
            logger.error(f"解析 AniList 响应失败：{str(err)}")
            return None
        if result.get("errors"):
            logger.warning(f"AniList 接口返回错误：{result.get('errors')}")
            return None
        return result.get("data")

    def _invoke(self, query: str, variables: dict) -> Optional[dict]:
        """
        通过官方 GraphQL 端点执行同步查询并合并中文标题。

        :param query: GraphQL 查询
        :param variables: 查询变量
        :return: GraphQL data 字段
        """
        payload = {"query": query, "variables": variables}
        response = self._request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        if not result:
            return result
        return self._inject_chinese(result, self._translation_map())

    async def _async_invoke(self, query: str, variables: dict) -> Optional[dict]:
        """
        通过官方 GraphQL 端点执行异步查询并合并中文标题。

        :param query: GraphQL 查询
        :param variables: 查询变量
        :return: GraphQL data 字段
        """
        payload = {"query": query, "variables": variables}
        response = await self._async_request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        if not result:
            return result
        translations = await self._async_translation_map()
        return self._inject_chinese(result, translations)

    @staticmethod
    def _build_translation_map(items) -> dict[Any, dict[str, Any]]:
        """
        将 anilist-chinese 数据集转换为按 AniList ID 索引的字典。

        :param items: anilist-chinese JSON 数据
        :return: 中文标题数据索引
        """
        if not isinstance(items, list):
            return {}
        return {
            item.get("id"): item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("id"), (int, str)) and item.get("id")
            and isinstance(item.get("title"), str)
            and isinstance(item.get("synonyms", []), list)
            and all(isinstance(alias, str) for alias in item.get("synonyms", []))
        }

    def _translation_routes(self) -> list[tuple[str, bool]]:
        """
        返回中文标题数据集下载路由（GITHUB_PROXY 镜像优先且不叠加 HTTP 代理，失败回退原地址）。
        """
        mirror = str(getattr(settings, "GITHUB_PROXY", "") or "").strip()
        routes = [(self._translations_url, False)]
        if mirror:
            routes.insert(0, (f"{UrlUtils.standardize_base_url(mirror)}{self._translations_url}", True))
        return routes

    @classmethod
    def _decode_translation_cache(cls, content: Optional[bytes | str]) -> tuple[dict[Any, dict[str, Any]], float]:
        """
        校验并解析本地磁盘缓存快照及过期时间戳。
        """
        if content:
            try:
                raw_text = content.decode("utf-8") if isinstance(content, bytes) else content
                payload = json.loads(raw_text)
                if isinstance(payload, dict):
                    expires_at = payload.get("expires_at")
                    if isinstance(expires_at, (int, float)) and isfinite(expires_at):
                        return cls._build_translation_map(payload.get("items")), float(expires_at)
            except (ValueError, TypeError, UnicodeDecodeError) as err:
                logger.warning(f"解析 anilist-chinese 磁盘缓存失败：{err}")
        return {}, 0.0

    def _translation_snapshot(self, items: object) -> Optional[bytes]:
        """
        校验远端下载数据并编码为带过期时间的磁盘快照字节流。
        """
        translations = self._build_translation_map(items)
        if not translations:
            return None
        self._translations = translations
        self._translations_expires_at = time() + self._translations_ttl
        return json.dumps({
            "expires_at": self._translations_expires_at,
            "items": list(translations.values()),
        }, ensure_ascii=False).encode("utf-8")

    def _reuse_translation_cache(self, translations: dict[Any, dict[str, Any]], expires_at: float) -> bool:
        """
        优先复用有效内存缓存；若磁盘快照存在则载入内存，过期数据保留作网络失败回退。
        """
        if self._translations is not None and time() < self._translations_expires_at:
            return True
        if translations and (not self._translations or expires_at > self._translations_expires_at):
            self._translations = translations
            self._translations_expires_at = expires_at
            return time() < expires_at
        return False

    def _translation_fallback(self) -> dict[Any, dict[str, Any]]:
        """
        远端下载失败时保留已有中文标题数据，并设置 5 分钟重试退避窗口。
        """
        logger.warning("加载 anilist-chinese 中文数据集失败，暂用已有数据，五分钟后重试")
        self._translations = self._translations or {}
        self._translations_expires_at = time() + self._translations_retry
        return self._translations

    def _translation_map(self) -> dict[Any, dict[str, Any]]:
        """
        同步加载并复用 anilist-chinese 中文标题数据（内存 + 本地文件持久化）。

        :return: 中文标题数据索引
        """
        with self._translations_sync_lock:
            if self._reuse_translation_cache({}, 0.0):
                return self._translations or {}
            content = None
            try:
                if self._file_cache is not None:
                    content = self._file_cache.get(self._translations_key, region=self._translations_region)
            except OSError as err:
                logger.warning(f"读取 anilist-chinese 磁盘缓存失败：{err}")
            if self._reuse_translation_cache(*self._decode_translation_cache(content)):
                return self._translations or {}
            for url, direct in self._translation_routes():
                request = RequestUtils(ua=settings.NORMAL_USER_AGENT) if direct else self._request
                snapshot = self._translation_snapshot(request.get_json(url))
                if snapshot is None:
                    continue
                try:
                    if self._file_cache is not None:
                        self._file_cache.set(
                            self._translations_key,
                            snapshot,
                            region=self._translations_region,
                        )
                except OSError as err:
                    logger.warning(f"写入 anilist-chinese 磁盘缓存失败：{err}")
                return self._translations or {}
            return self._translation_fallback()

    async def _async_translation_map(self) -> dict[Any, dict[str, Any]]:
        """
        异步加载并复用 anilist-chinese 中文标题数据（内存 + 本地文件持久化）。

        :return: 中文标题数据索引
        """
        async with self._get_async_lock():
            if self._reuse_translation_cache({}, 0.0):
                return self._translations or {}
            content = None
            try:
                if self._async_file_cache is not None:
                    content = await self._async_file_cache.get(
                        self._translations_key,
                        region=self._translations_region,
                    )
            except OSError as err:
                logger.warning(f"读取 anilist-chinese 磁盘缓存失败：{err}")
            if self._reuse_translation_cache(*self._decode_translation_cache(content)):
                return self._translations or {}
            for url, direct in self._translation_routes():
                request = AsyncRequestUtils(ua=settings.NORMAL_USER_AGENT) if direct else self._async_request
                snapshot = self._translation_snapshot(await request.get_json(url))
                if snapshot is None:
                    continue
                try:
                    if self._async_file_cache is not None:
                        await self._async_file_cache.set(
                            self._translations_key,
                            snapshot,
                            region=self._translations_region,
                        )
                except OSError as err:
                    logger.warning(f"写入 anilist-chinese 磁盘缓存失败：{err}")
                return self._translations or {}
            return self._translation_fallback()

    @classmethod
    def _inject_chinese(cls, value, translations: dict[int, dict]):
        """
        递归合并 anilist-chinese 标题，覆盖代理不会处理的嵌套媒体。

        :param value: AniList GraphQL data 字段或其子节点
        :param translations: 中文标题数据索引
        :return: 合并中文标题后的原数据结构
        """
        if isinstance(value, list):
            for item in value:
                cls._inject_chinese(item, translations)
            return value
        if not isinstance(value, dict):
            return value

        translation = translations.get(value.get("id"))
        title = value.get("title")
        if translation and isinstance(title, dict):
            title["chinese"] = translation.get("title")
        synonyms = value.get("synonyms")
        if translation and isinstance(synonyms, list):
            value["synonyms"] = list(
                dict.fromkeys([*synonyms, *(translation.get("synonyms") or [])])
            )
        for child in value.values():
            cls._inject_chinese(child, translations)
        return value

    @staticmethod
    def _page_variables(
        page: int,
        count: int,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> dict:
        """
        构造 AniList 分页媒体查询变量。

        :return: 去除空值后的 GraphQL 变量
        """
        variables = {
            "page": page,
            "count": count,
            "search": search,
            "genre": genre,
            "format": media_format,
            "season": season,
            "seasonYear": season_year,
            "status": status,
            "country": country,
            "sort": [sort] if sort else ["POPULARITY_DESC"],
        }
        return {key: value for key, value in variables.items() if value is not None}

    @staticmethod
    def _page_medias(result: Optional[dict]) -> list[dict]:
        """
        从分页响应中提取媒体列表。

        :param result: GraphQL data 字段
        :return: AniList 媒体列表
        """
        return result.get("Page", {}).get("media") or [] if result else []

    @staticmethod
    def _ordered_medias(media_ids: list[int], medias: list[dict]) -> list[dict]:
        """
        按上游关系顺序重排批量查询返回的媒体。

        :param media_ids: 关系查询返回的 AniList 媒体 ID
        :param medias: Page.media 批量查询结果
        :return: 保持原关系顺序的媒体列表
        """
        media_map = {media.get("id"): media for media in medias if media.get("id")}
        return [media_map[media_id] for media_id in media_ids if media_id in media_map]

    def _medias_by_ids(self, media_ids: list[int]) -> list[dict]:
        """
        通过根级 Page.media 批量查询媒体，使中文代理能够注入标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        if not unique_ids:
            return []
        result = self._invoke(
            self._media_by_ids_query,
            {"ids": unique_ids, "count": len(unique_ids)},
        )
        return self._ordered_medias(media_ids, self._page_medias(result))

    async def _async_medias_by_ids(self, media_ids: list[int]) -> list[dict]:
        """
        异步通过根级 Page.media 批量查询媒体，使中文代理能够注入标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        if not unique_ids:
            return []
        result = await self._async_invoke(
            self._media_by_ids_query,
            {"ids": unique_ids, "count": len(unique_ids)},
        )
        return self._ordered_medias(media_ids, self._page_medias(result))

    @staticmethod
    def _current_season(today: Optional[date] = None) -> tuple[str, int]:
        """
        根据当前日期计算 AniList 季度与年份。

        :param today: 用于测试或指定季度的日期
        :return: AniList 季度枚举和年份
        """
        current = today or date.today()
        seasons = ("WINTER", "SPRING", "SUMMER", "FALL")
        return seasons[(current.month - 1) // 3], current.year

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="detail",
    )
    def detail(self, anilist_id: int) -> Optional[dict]:
        """
        根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = self._invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="detail",
    )
    async def async_detail(self, anilist_id: int) -> Optional[dict]:
        """
        异步根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = await self._async_invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="search",
    )
    def search(self, name: str, count: int = 20) -> list[dict]:
        """
        按标题搜索 AniList 动画。

        :param name: 动画标题
        :param count: 返回条数
        :return: AniList 媒体列表
        """
        query = f"""
            query ($search: String!, $count: Int!) {{
              Page(page: 1, perPage: $count) {{
                media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{ {self._media_fields} }}
              }}
            }}
        """
        result = self._invoke(query, {"search": name, "count": count})
        return self._page_medias(result)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="search",
    )
    async def async_search(self, name: str, count: int = 20) -> list[dict]:
        """
        异步按标题搜索 AniList 动画。

        :param name: 动画标题
        :param count: 返回条数
        :return: AniList 媒体列表
        """
        query = f"""
            query ($search: String!, $count: Int!) {{
              Page(page: 1, perPage: $count) {{
                media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{ {self._media_fields} }}
              }}
            }}
        """
        result = await self._async_invoke(query, {"search": name, "count": count})
        return self._page_medias(result)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="discover",
    )
    def discover(
        self,
        page: int = 1,
        count: int = 20,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict]:
        """
        按组合条件探索 AniList 动画。

        :return: AniList 媒体列表
        """
        variables = self._page_variables(
            page=page,
            count=count,
            search=search,
            genre=genre,
            media_format=media_format,
            season=season,
            season_year=season_year,
            status=status,
            country=country,
            sort=sort,
        )
        return self._page_medias(self._invoke(self._page_query, variables))

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="discover",
    )
    async def async_discover(
        self,
        page: int = 1,
        count: int = 20,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict]:
        """
        异步按组合条件探索 AniList 动画。

        :return: AniList 媒体列表
        """
        variables = self._page_variables(
            page=page,
            count=count,
            search=search,
            genre=genre,
            media_format=media_format,
            season=season,
            season_year=season_year,
            status=status,
            country=country,
            sort=sort,
        )
        result = await self._async_invoke(self._page_query, variables)
        return self._page_medias(result)

    def trending(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 当前趋势榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        return self.discover(page=page, count=count, sort="TRENDING_DESC")

    async def async_trending(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 当前趋势榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        return await self.async_discover(page=page, count=count, sort="TRENDING_DESC")

    def popular_this_season(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 本季热门榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        season, season_year = self._current_season()
        return self.discover(
            page=page,
            count=count,
            season=season,
            season_year=season_year,
            sort="POPULARITY_DESC",
        )

    async def async_popular_this_season(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 本季热门榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        season, season_year = self._current_season()
        return await self.async_discover(
            page=page,
            count=count,
            season=season,
            season_year=season_year,
            sort="POPULARITY_DESC",
        )

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="credits",
    )
    def credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 动画的日语配音演员。

        :return: AniList 人物边列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                characters(page: $page, perPage: $count, sort: [ROLE, RELEVANCE]) {
                  edges {
                    role
                    node { id name { full native } }
                    voiceActors(language: JAPANESE, sort: [RELEVANCE]) {
                      id name { full native alternative } image { large medium } siteUrl
                    }
                  }
                }
              }
            }
        """
        result = self._invoke(query, {"id": anilist_id, "page": page, "count": count})
        return result.get("Media", {}).get("characters", {}).get("edges") or [] if result else []

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="credits",
    )
    async def async_credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 动画的日语配音演员。

        :return: AniList 人物边列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                characters(page: $page, perPage: $count, sort: [ROLE, RELEVANCE]) {
                  edges {
                    role
                    node { id name { full native } }
                    voiceActors(language: JAPANESE, sort: [RELEVANCE]) {
                      id name { full native alternative } image { large medium } siteUrl
                    }
                  }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": anilist_id, "page": page, "count": count})
        return result.get("Media", {}).get("characters", {}).get("edges") or [] if result else []

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="recommendations",
    )
    def recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 动画相关推荐。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                recommendations(page: $page, perPage: $count, sort: [RATING_DESC, ID]) {
                  nodes { mediaRecommendation { id } }
                }
              }
            }
        """
        result = self._invoke(query, {"id": anilist_id, "page": page, "count": count})
        nodes = result.get("Media", {}).get("recommendations", {}).get("nodes") or [] if result else []
        media_ids = [node.get("mediaRecommendation", {}).get("id") for node in nodes]
        return self._medias_by_ids(media_ids)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="recommendations",
    )
    async def async_recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 动画相关推荐。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                recommendations(page: $page, perPage: $count, sort: [RATING_DESC, ID]) {
                  nodes { mediaRecommendation { id } }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": anilist_id, "page": page, "count": count})
        nodes = result.get("Media", {}).get("recommendations", {}).get("nodes") or [] if result else []
        media_ids = [node.get("mediaRecommendation", {}).get("id") for node in nodes]
        return await self._async_medias_by_ids(media_ids)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_detail",
    )
    def person_detail(self, person_id: int) -> Optional[dict]:
        """
        获取 AniList 演员详情。

        :param person_id: AniList 人物 ID
        :return: AniList 人物详情
        """
        query = """
            query ($id: Int!) {
              Staff(id: $id) {
                id name { full native alternative } image { large medium }
                description(asHtml: false) dateOfBirth { year month day }
                dateOfDeath { year month day } gender homeTown primaryOccupations siteUrl
              }
            }
        """
        result = self._invoke(query, {"id": person_id})
        return result.get("Staff") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_detail",
    )
    async def async_person_detail(self, person_id: int) -> Optional[dict]:
        """
        异步获取 AniList 演员详情。

        :param person_id: AniList 人物 ID
        :return: AniList 人物详情
        """
        query = """
            query ($id: Int!) {
              Staff(id: $id) {
                id name { full native alternative } image { large medium }
                description(asHtml: false) dateOfBirth { year month day }
                dateOfDeath { year month day } gender homeTown primaryOccupations siteUrl
              }
            }
        """
        result = await self._async_invoke(query, {"id": person_id})
        return result.get("Staff") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_credits",
    )
    def person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 演员参与的动画作品。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Staff(id: $id) {
                characterMedia(page: $page, perPage: $count, sort: [POPULARITY_DESC]) {
                  nodes { id }
                }
              }
            }
        """
        result = self._invoke(query, {"id": person_id, "page": page, "count": count})
        nodes = result.get("Staff", {}).get("characterMedia", {}).get("nodes") or [] if result else []
        return self._medias_by_ids([node.get("id") for node in nodes])

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_credits",
    )
    async def async_person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 演员参与的动画作品。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Staff(id: $id) {
                characterMedia(page: $page, perPage: $count, sort: [POPULARITY_DESC]) {
                  nodes { id }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": person_id, "page": page, "count": count})
        nodes = result.get("Staff", {}).get("characterMedia", {}).get("nodes") or [] if result else []
        return await self._async_medias_by_ids([node.get("id") for node in nodes])

    def clear_cache(self) -> None:
        """清理 AniList 接口缓存"""
        for method in (
            self.detail,
            self.search,
            self.discover,
            self.credits,
            self.recommendations,
            self.person_detail,
            self.person_credits,
        ):
            method.cache_clear()
