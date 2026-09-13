from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from lxml import etree

from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils


class AniBTSpider:
    """AniBT 官方公开 RSS 索引器。"""

    _rss_url = "https://anibt.net/rss/magnets.xml"
    _anibt_namespace = "https://anibt.net/xmlns/rss/1.0/"
    _torrent_namespace = "https://anibt.moe/xmlns/0.1/"

    def __init__(self, indexer: dict):
        indexer = indexer or {}
        self._name = indexer.get("name") or "AniBT"
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._timeout = indexer.get("timeout") or 15

    @classmethod
    def matches(cls, indexer: dict) -> bool:
        """判断站点配置是否指向 AniBT 主站。"""
        domain = StringUtils.get_url_domain(
            str((indexer or {}).get("domain") or (indexer or {}).get("url") or "")
        ).lower()
        return domain in {"anibt.net", "www.anibt.net"}

    @staticmethod
    def get_search_page_size(keyword: Optional[str] = None) -> None:
        """AniBT RSS 不提供分页游标。"""
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
            and parsed.hostname == "anibt.net"
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
        """将官方 RSS 映射为 MoviePilot 标准种子字段。"""
        if not content:
            return []
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        root = etree.fromstring(content, parser=parser)
        namespaces = {
            "anibt": cls._anibt_namespace,
            "torrent": cls._torrent_namespace,
        }
        torrents = []
        for item in root.xpath("/rss/channel/item"):
            title = cls._text(item, "./title/text()")
            enclosure = cls._text(item, "./anibt:torrentUrl/text()", namespaces)
            if not enclosure:
                enclosure = cls._text(item, "./enclosure/@url")
            page_url = cls._text(item, "./anibt:releasePageUrl/text()", namespaces)
            if not page_url:
                page_url = cls._text(item, "./link/text()")
            if (
                not title
                or not cls._is_official_url(enclosure, "/api/torrent/")
                or not cls._is_official_url(page_url, "/release/")
            ):
                continue

            anime_title = cls._text(item, "./anibt:animeTitle/text()", namespaces)
            group_name = cls._text(item, "./anibt:groupName/text()", namespaces)
            resolution = cls._text(item, "./anibt:resolution/text()", namespaces)
            subtitle = cls._text(item, "./anibt:subtitle/text()", namespaces)
            media_format = cls._text(item, "./anibt:format/text()", namespaces)
            languages = [
                str(value).strip()
                for value in item.xpath("./anibt:language/text()", namespaces=namespaces)
                if str(value).strip()
            ]
            custom_tags = [
                str(value).strip()
                for value in item.xpath("./anibt:customTag/text()", namespaces=namespaces)
                if str(value).strip()
            ]
            labels = list(dict.fromkeys([
                value
                for value in [group_name, resolution, *languages, subtitle, media_format, *custom_tags]
                if value
            ]))
            size = cls._integer(cls._text(item, "./anibt:fileSize/text()", namespaces))
            if not size:
                size = cls._integer(cls._text(item, "./enclosure/@length"))
            if not size:
                size = cls._integer(cls._text(item, "./torrent:torrent/torrent:contentLength/text()", namespaces))

            torrents.append({
                "title": title,
                "description": anime_title,
                "enclosure": enclosure,
                "page_url": page_url,
                "pubdate": StringUtils.unify_datetime_str(
                    cls._text(item, "./pubDate/text()")
                ),
                "size": size,
                "seeders": 0,
                "peers": 0,
                "grabs": 0,
                "downloadvolumefactor": 0,
                "uploadvolumefactor": 1,
                "labels": labels,
                "hit_and_run": False,
                "category": MediaType.TV.value,
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
        return {"q": keyword} if keyword else {}

    def search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """同步搜索 AniBT 动漫资源。"""
        if mtype == MediaType.MOVIE or int(page or 0) > 0:
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
        """异步搜索 AniBT 动漫资源。"""
        if mtype == MediaType.MOVIE or int(page or 0) > 0:
            return False, []
        response = await AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=self._rss_url, params=self._build_params(keyword))
        return self._process_response(response)
