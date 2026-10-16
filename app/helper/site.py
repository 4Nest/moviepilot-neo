"""补充 v2 资源目录尚未收录的站点，认证和流控仍由原资源帮助器负责。"""

from copy import deepcopy
from typing import Any, Optional

from app.db.models.site import Site
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper as ResourceSitesHelper
from app.utils.string import StringUtils


SITE_SUPPLEMENTS = {
    "colapt.com": {
        "id": "colapt", "name": "Cola", "url": "https://www.colapt.com/",
        "domain": "https://www.colapt.com/", "public": False,
        "parser": "mTorrent", "schema": "MTorrent", "language": "zh",
        "subtitles": True,
    },
}


class SitesHelper:
    """合并原站点目录与 NEO 补充目录，不修改随资源更新覆盖的二进制文件。"""

    def __init__(self) -> None:
        self._resource = ResourceSitesHelper()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resource, name)

    def get_indexsites(self) -> dict:
        """返回可添加站点目录，上游已收录的条目优先。"""
        sites = dict(self._resource.get_indexsites())
        supplements = {
            domain: {key: value for key, value in item.items() if key in {"id", "name", "url", "public"}}
            for domain, item in SITE_SUPPLEMENTS.items()
        }
        return {**supplements, **sites}

    @staticmethod
    def _build_indexer(domain: str, site: Optional[Site] = None) -> dict:
        entry = deepcopy(SITE_SUPPLEMENTS[domain])
        if site:
            entry.update(site.to_dict())
            # 搜索器的 domain 是完整站点地址，数据库 domain 则是注册域名。
            entry["domain"] = site.url
        return entry

    def get_indexer(self, domain: str) -> dict:
        """获取站点定义及实时配置；补充目录同样要求通过用户认证。"""
        indexer = self._resource.get_indexer(domain)
        if indexer:
            return indexer
        domain = StringUtils.get_url_domain(domain)
        if self._resource.auth_level < 2 or domain not in SITE_SUPPLEMENTS:
            return {}
        return self._build_indexer(domain, SiteOper().get_by_domain(domain))

    async def async_get_indexer(self, domain: str) -> dict:
        """异步获取站点定义及实时配置。"""
        indexer = await self._resource.async_get_indexer(domain)
        if indexer:
            return indexer
        domain = StringUtils.get_url_domain(domain)
        if self._resource.auth_level < 2 or domain not in SITE_SUPPLEMENTS:
            return {}
        return self._build_indexer(domain, await SiteOper().async_get_by_domain(domain))

    def _merge_indexers(self, indexers: list[dict], sites: list[Site]) -> list[dict]:
        domains = {StringUtils.get_url_domain(item.get("domain") or item.get("url")) for item in indexers}
        supplements = [self._build_indexer(site.domain, site) for site in sites
                       if site.domain in SITE_SUPPLEMENTS and site.domain not in domains and site.is_active]
        return sorted([*indexers, *supplements], key=lambda item: item.get("pri") or 0)

    def get_indexers(self) -> list[dict]:
        """返回已启用站点，未添加或已停用的补充站点不参与搜索和订阅。"""
        indexers = self._resource.get_indexers()
        if self._resource.auth_level < 2:
            return indexers
        return self._merge_indexers(indexers, SiteOper().list_active())

    async def async_get_indexers(self) -> list[dict]:
        """异步返回已启用站点。"""
        indexers = await self._resource.async_get_indexers()
        if self._resource.auth_level < 2:
            return indexers
        return self._merge_indexers(indexers, await SiteOper().async_list_active())
