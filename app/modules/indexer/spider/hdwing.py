# -*- coding: utf-8 -*-
"""在通用 NexusPHP 解析前补全 HDWinG 异步加载的促销标识。"""

import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urljoin

from app.log import logger
from app.modules.indexer.spider import SiteSpider
from app.schemas.types import MediaType
from app.utils.http import RequestUtils

# 保留原始标签结构，使站点既有选择器可以继续识别促销图标。
_PLACEHOLDER_PATTERN = re.compile(
    r"""(?P<opening><span\b(?=[^>]*\sclass\s*=\s*(['"])(?:[^'"]*\s)?sp_state_placeholder"""
    r"""(?:\s[^'"]*)?\2)(?=[^>]*\sid\s*=\s*(['"])(?P<id>\d+)\3)[^>]*>)\s*(?P<closing></span\s*>)""",
    re.IGNORECASE,
)
_CSRF_META_PATTERN = re.compile(
    r"""<meta\b(?=[^>]*\sname\s*=\s*(['"])x-csrf\1)"""
    r"""(?=[^>]*\scontent\s*=\s*(['"])(?P<token>[^'"]+)\2)[^>]*>""",
    re.IGNORECASE,
)
_CSRF_LINK_PATTERN = re.compile(r"""logout\.php\?[^'"<>]*?\bcsrf=([\w-]+)""", re.IGNORECASE)
_PROMOTION_PAGE = "ajax_promotion.php"
_TORRENTS_PAGE = "torrents.php"


def _extract_csrf(html_text: str) -> Optional[str]:
    """从 meta 声明或退出链接提取促销接口 CSRF 令牌。"""
    meta = _CSRF_META_PATTERN.search(html_text)
    if meta:
        return meta.group("token")
    link = _CSRF_LINK_PATTERN.search(html_text)
    return link.group(1) if link else None


class HDWingPromotionMixin:
    """为 SiteSpider 提供 HDWinG 促销占位符回填能力，失败时保留原页面。"""

    domain: Optional[str]
    ua: Optional[str]
    cookie: Optional[str]
    proxies: Any
    _timeout: int
    indexername: Optional[str]

    def inject_promotions(self, html_text: str) -> str:
        """
        批量获取并注入促销状态；无占位符或接口不可用时返回原始页面。

        :param html_text: 已解码的种子列表 HTML
        :return: 回填后的 HTML，无法回填时为原始文本
        """
        if "sp_state_placeholder" not in html_text:
            return html_text
        try:
            promotion_html = self.__fetch_promotion_html(html_text)
        except Exception as err:
            logger.warning(f"{self.indexername} 促销状态补全失败，按原始页面解析：{str(err)}")
            return html_text
        if not promotion_html:
            return html_text
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: (
                f"{match.group('opening')}{promotion_html[match.group('id')]}{match.group('closing')}"
                if match.group("id") in promotion_html else match.group(0)
            ),
            html_text,
        )

    def __fetch_promotion_html(self, html_text: str) -> dict[str, str]:
        """请求 ajax_promotion.php，并返回种子 ID 到促销 HTML 的映射。"""
        torrent_ids = list(dict.fromkeys(
            match.group("id") for match in _PLACEHOLDER_PATTERN.finditer(html_text)
        ))
        csrf = _extract_csrf(html_text)
        if not torrent_ids or not csrf:
            logger.warning(f"{self.indexername} 页面缺少促销占位符或 CSRF 令牌，跳过促销状态补全")
            return {}
        domain = self.domain or ""
        client = RequestUtils(
            headers={"X-Requested-With": "XMLHttpRequest"},
            ua=self.ua,
            cookies=self.cookie,
            timeout=self._timeout,
            referer=urljoin(domain, _TORRENTS_PAGE),
            proxies=self.proxies,
        )
        response = client.post_res(
            url=urljoin(domain, _PROMOTION_PAGE),
            data={"ids[]": torrent_ids, "csrf": csrf},
        )
        try:
            if response is None or response.status_code != 200:
                logger.warning(
                    f"{self.indexername} 促销状态请求失败，HTTP "
                    f"{getattr(response, 'status_code', '无响应')}"
                )
                return {}
            try:
                result = response.json()
            except (TypeError, ValueError):
                logger.warning(f"{self.indexername} 促销状态响应不是有效 JSON，跳过促销状态补全")
                return {}
            if not isinstance(result, dict) or result.get("status") != 200:
                logger.warning(f"{self.indexername} 促销状态接口返回异常")
                return {}
            states = result.get("message") or {}
            if not isinstance(states, dict):
                logger.warning(f"{self.indexername} 促销状态列表结构异常，跳过促销状态补全")
                return {}
            promotion_html: dict[str, str] = {}
            for torrent_id, state in states.items():
                state = state or {}
                if not isinstance(state, dict):
                    logger.warning(f"{self.indexername} 种子 {torrent_id} 的促销状态结构异常")
                    return {}
                sp_state = str(state.get("sp_state") or "")
                timeout = str(state.get("timeout") or "")
                if sp_state or timeout:
                    promotion_html[str(torrent_id)] = f"<p>{sp_state}</p>{timeout}"
            return promotion_html
        finally:
            if response is not None:
                response.close()


class HDWingPageSpider(HDWingPromotionMixin, SiteSpider):
    """在通用页面解析前补全 HDWinG 的异步促销状态。"""

    def parse(self, html_text: str) -> List[dict]:
        """回填促销图标与免费截止时间后复用 SiteSpider 标准解析。"""
        if html_text:
            html_text = self.inject_promotions(html_text)
        return super().parse(html_text)


class HDWingSpider:
    """将 HDWinG 同步与异步搜索接入带促销回填的页面爬虫。"""

    _size = 100

    def __init__(self, indexer: dict[str, Any]) -> None:
        """保存站点配置，每次搜索创建独立页面爬虫。"""
        self._indexer = indexer
        self._spider: Optional[HDWingPageSpider] = None

    @property
    def error_detail(self) -> Optional[str]:
        """返回最近一次页面搜索的错误详情，未执行搜索时为空。"""
        return getattr(self._spider, "error_detail", None) if self._spider else None

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """返回 HDWinG 列表页固定的一百条分页容量。"""
        return cls._size

    def search(
        self,
        keyword: Optional[str] = None,
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """同步搜索并在通用 NexusPHP 解析前补全促销信息。"""
        spider = HDWingPageSpider(self._indexer, keyword=keyword, mtype=mtype, cat=cat, page=page)
        self._spider = spider
        result = spider.get_torrents()
        return spider.is_error, result

    async def async_search(
        self,
        keyword: Optional[str] = None,
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """异步获取列表，并在线程池中执行同步促销回填与页面解析。"""
        spider = HDWingPageSpider(self._indexer, keyword=keyword, mtype=mtype, cat=cat, page=page)
        self._spider = spider
        result = await spider.async_get_torrents()
        return spider.is_error, result
