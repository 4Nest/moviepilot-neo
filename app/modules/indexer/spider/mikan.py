"""Mikan Project（mikanani.me）官方 RSS 搜索适配。"""

from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from lxml import etree

from app.core.config import settings
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils


class MikanSpider:
    """Mikan Project RSS 搜索，公开资源无需 Cookie。"""

    _rss_url = "https://mikanani.me/RSS/Search"
    _torrent_namespace = "https://mikanani.me/0.1/"

    def __init__(self, indexer: dict):
        indexer = indexer or {}
        self._name = indexer.get("name") or "Mikan"
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._timeout = indexer.get("timeout") or 15

    @classmethod
    def matches(cls, indexer: dict) -> bool:
        """判断站点配置是否指向 Mikan Project 主站。"""
        domain = StringUtils.get_url_domain(
            str((indexer or {}).get("domain") or (indexer or {}).get("url") or "")
        ).lower()
        return domain in {"mikanani.me", "www.mikanani.me"}

    @staticmethod
    def get_search_page_size(keyword: Optional[str] = None) -> None:
        """Mikan RSS 搜索不提供分页游标。"""
        return None

    def _request_headers(self) -> dict:
        return {
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            "User-Agent": self._user_agent,
        }

    @staticmethod
    def _text(node, xpath: str, namespaces: dict = None) -> Optional[str]:
        values = node.xpath(xpath, namespaces=namespaces or {})
        if not values:
            return None
        value = values[0]
        if isinstance(value, etree._Element):
            value = value.text
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _is_official_url(value: Optional[str], prefix: str) -> bool:
        if not value:
            return False
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"mikanani.me", "www.mikanani.me"}
            and parsed.path.startswith(prefix)
        )

    @staticmethod
    def _integer(value: Optional[str]) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _parse_xml(cls, content: bytes) -> List[dict]:
        """将 Mikan RSS 映射为 MoviePilot 标准种子字段。"""
        if not content:
            return []
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        root = etree.fromstring(content, parser=parser)
        namespaces = {"torrent": cls._torrent_namespace}
        torrents = []
        for item in root.xpath("/rss/channel/item"):
            title = cls._text(item, "./title/text()")
            enclosure = cls._text(item, "./enclosure/@url")
            page_url = cls._text(item, "./link/text()")
            if (
                not title
                or not cls._is_official_url(enclosure, "/Download/")
                or not cls._is_official_url(page_url, "/Home/Episode/")
            ):
                continue

            size = cls._integer(cls._text(item, "./enclosure/@length"))
            if not size:
                size = cls._integer(cls._text(item, "./torrent:torrent/torrent:contentLength/text()", namespaces))
            pubdate = cls._text(item, "./pubDate/text()") or cls._text(
                item, "./torrent:torrent/torrent:pubDate/text()", namespaces
            )
            torrents.append({
                "title": title,
                "description": cls._text(item, "./description/text()"),
                "enclosure": enclosure,
                "page_url": page_url,
                "pubdate": StringUtils.unify_datetime_str(pubdate),
                "size": size,
                "seeders": 0,
                "peers": 0,
                "grabs": 0,
                "downloadvolumefactor": 0,
                "uploadvolumefactor": 1,
                "labels": [],
                "hit_and_run": False,
                "category": MediaType.MOVIE.value if "[movie]" in title.lower() else MediaType.TV.value,
            })
        return torrents

    def _process_response(self, response) -> Tuple[bool, List[dict]]:
        if response is None:
            logger.warning(f"{self._name} 搜索失败，无法连接官方 RSS")
            return True, []
        if response.status_code != 200:
            logger.warning(f"{self._name} 搜索失败，HTTP 错误码：{response.status_code}")
            return True, []
        try:
            return False, self._parse_xml(response.content)
        except (etree.XMLSyntaxError, ValueError, TypeError) as err:
            logger.warning(f"{self._name} 搜索响应不是有效 RSS：{str(err)}")
            return True, []

    @staticmethod
    def _build_params(keyword: Optional[str]) -> dict:
        if not keyword:
            return {}
        cleaned = " ".join(keyword.split())
        return {"searchstr": cleaned} if cleaned else {}

    def search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """同步搜索 Mikan 资源；RSS 覆盖剧集与剧场版，仅跳过分页。"""
        if int(page or 0) > 0:
            return False, []
        response = RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=self._rss_url, params=self._build_params(keyword))
        return self._process_response(response)

    async def async_search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """异步搜索 Mikan 资源，参数与同步路径一致。"""
        if int(page or 0) > 0:
            return False, []
        response = await AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=self._rss_url, params=self._build_params(keyword))
        return self._process_response(response)
