"""通过 FluxTorrent 的 NexusPHP 只读兼容层搜索种子并检查连接。"""

from typing import Any, Optional

from app.core.config import settings
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils

_COMPAT_PREFIX = "/api/v1/compat/nexusphp"
_PROMOTION_FACTORS: dict[str, tuple[float, float]] = {
    "free": (0.0, 1.0),
    "x2free": (0.0, 2.0),
    "half": (0.5, 1.0),
    "x2half": (0.5, 2.0),
    "x2": (1.0, 2.0),
    "p30": (0.3, 1.0),
}
_NEWZNAB_MEDIA_TYPES: dict[int, MediaType] = {
    2000: MediaType.MOVIE,
    2010: MediaType.MOVIE,
    2030: MediaType.MOVIE,
    2040: MediaType.MOVIE,
    2045: MediaType.MOVIE,
    5000: MediaType.TV,
    5030: MediaType.TV,
    5040: MediaType.TV,
    5060: MediaType.TV,
    5070: MediaType.TV,
    5080: MediaType.TV,
    5090: MediaType.TV,
    7000: MediaType.COLLECTION,
    7010: MediaType.COLLECTION,
    7020: MediaType.COLLECTION,
    7030: MediaType.COLLECTION,
}
_CATEGORY_NAME_MEDIA_TYPES: dict[str, MediaType] = {
    "电影": MediaType.MOVIE,
    "电视剧": MediaType.TV,
    "短剧": MediaType.TV,
    "综艺": MediaType.TV,
    "动漫": MediaType.TV,
    "纪录片": MediaType.TV,
    "教育影音": MediaType.TV,
    "有声书": MediaType.COLLECTION,
    "电子书": MediaType.COLLECTION,
}


def site_base_url(site: dict[str, Any]) -> str:
    """规范化站点根地址，缺少协议时补 HTTPS 并去除末尾斜杠。"""
    raw = str(site.get("url") or site.get("domain") or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    return raw.rstrip("/")


def site_api_token(site: dict[str, Any]) -> str:
    """返回站点配置的开放 API 令牌，优先使用 API Key。"""
    return str(site.get("apikey") or site.get("token") or "").strip()


class FluxTorrentSpider:
    """FluxTorrent 兼容层索引器，只调用 JSON 接口而不刮削站点页面。"""

    _size = 50

    def __init__(self, indexer: dict[str, Any]) -> None:
        """保存站点地址、开放 API 令牌及请求上下文。"""
        indexer = indexer or {}
        self._name = indexer.get("name") or "FluxTorrent"
        self._site_url = site_base_url(indexer)
        self._api_token = site_api_token(indexer)
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._timeout = indexer.get("timeout") or 15
        self.error_detail: Optional[str] = None

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """返回兼容层搜索接口固定的每页五十条容量。"""
        return cls._size

    @property
    def _base_url(self) -> str:
        """返回当前站点的 NexusPHP 兼容层接口基址。"""
        return f"{self._site_url}{_COMPAT_PREFIX}"

    def _request_headers(self) -> dict[str, str]:
        """构造带开放 API 令牌的兼容层请求头。"""
        return {
            "Authorization": f"Token {self._api_token}",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }

    def _response_data(self, response: Any, operation: str) -> Any:
        """校验兼容层统一响应信封并返回业务数据，失败时记录可读原因。"""
        if response is None:
            self.error_detail = f"{operation}失败：站点无响应"
            logger.warning(f"{self._name} {self.error_detail}")
            return None
        status_code = getattr(response, "status_code", 0)
        try:
            body = response.json()
        except (TypeError, ValueError) as err:
            self.error_detail = f"{operation}失败：响应不是 JSON（状态码 {status_code}）"
            logger.warning(f"{self._name} {self.error_detail}：{str(err)}")
            return None
        if not isinstance(body, dict):
            self.error_detail = f"{operation}失败：响应结构异常"
            logger.warning(f"{self._name} {self.error_detail}")
            return None
        code = body.get("code")
        if code != 0 and str(code) != "0":
            self.error_detail = f"{operation}失败：{body.get('message') or f'接口返回 {code}'}"
            logger.warning(f"{self._name} {self.error_detail}")
            return None
        if status_code < 200 or status_code >= 300:
            self.error_detail = f"{operation}失败：HTTP {status_code}"
            logger.warning(f"{self._name} {self.error_detail}")
            return None
        return body.get("data")

    def _build_params(self, keyword: Optional[str], page: Optional[int]) -> dict[str, Any]:
        """构造关键字和分页参数，分类由结果字段表达而不错用站点自定义 ID。"""
        params: dict[str, Any] = {"page": (page or 0) + 1, "pagesize": self._size}
        if keyword:
            params["keyword"] = keyword
        return params

    @staticmethod
    def _media_type(item: dict[str, Any]) -> str:
        """按 Newznab 标准分类号映射媒体类型，缺失时以分类名兜底。"""
        newznab_id = item.get("category_newznab")
        media_type = None
        if isinstance(newznab_id, int) and not isinstance(newznab_id, bool):
            media_type = _NEWZNAB_MEDIA_TYPES.get(newznab_id)
        if media_type is None:
            media_type = _CATEGORY_NAME_MEDIA_TYPES.get(str(item.get("category_name") or "").strip())
        return (media_type or MediaType.UNKNOWN).value

    @staticmethod
    def _volume_factors(promotion: Optional[str]) -> tuple[float, float]:
        """将 free、x2free、half、x2half、x2、p30 映射为计费倍率。"""
        return _PROMOTION_FACTORS.get(str(promotion or "").strip(), (1.0, 1.0))

    def _build_download_url(self, torrent_id: object) -> str:
        """构造带开放 API 令牌的 compat download.php 种子地址。"""
        return f"{self._base_url}/download.php?id={torrent_id}&apikey={self._api_token}"

    def _parse_result(self, items: list[Any]) -> list[dict[str, Any]]:
        """将兼容层种子记录转换为 NEO 标准搜索字段。"""
        torrents: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            torrent_id = item.get("id")
            download_factor, upload_factor = self._volume_factors(item.get("promotion"))
            labels: list[str] = []
            promotion_name = str(item.get("promotion_name") or "").strip()
            if promotion_name:
                labels.append(promotion_name)
            if item.get("official") is True:
                labels.append("官方")
            torrents.append({
                "title": item.get("name"),
                "description": item.get("small_descr"),
                "enclosure": self._build_download_url(torrent_id),
                "pubdate": StringUtils.format_timestamp(item.get("added")),
                "size": int(item.get("size") or 0),
                "seeders": int(item.get("seeders") or 0),
                "peers": int(item.get("leechers") or 0),
                "grabs": int(item.get("completed") or 0),
                "downloadvolumefactor": download_factor,
                "uploadvolumefactor": upload_factor,
                "page_url": f"{self._site_url}/torrent/{torrent_id}",
                "labels": labels,
                "category": self._media_type(item),
            })
        return torrents

    def _process_search_response(self, response: Any) -> tuple[bool, list[dict[str, Any]]]:
        """处理搜索响应，只有有效 data.items 才发布当前页结果。"""
        data = self._response_data(response, "搜索")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            if not self.error_detail:
                self.error_detail = "搜索失败：响应缺少有效种子列表"
            return True, []
        return False, self._parse_result(data["items"])

    def search(self, keyword: Optional[str] = None,
               page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """同步搜索 FluxTorrent 站点种子，缺少令牌时不发送请求。"""
        self.error_detail = None
        if not self._api_token:
            self.error_detail = "未配置开放 API 令牌"
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        response = RequestUtils(
            headers=self._request_headers(), proxies=self._proxy, timeout=self._timeout,
        ).get_res(url=f"{self._base_url}/torrents.json", params=self._build_params(keyword, page))
        try:
            return self._process_search_response(response)
        finally:
            if response is not None:
                response.close()

    async def async_search(self, keyword: Optional[str] = None,
                           page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """异步搜索 FluxTorrent 站点种子，并遵循同步接口相同字段契约。"""
        self.error_detail = None
        if not self._api_token:
            self.error_detail = "未配置开放 API 令牌"
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        response = await AsyncRequestUtils(
            headers=self._request_headers(), proxies=self._proxy, timeout=self._timeout,
        ).get_res(url=f"{self._base_url}/torrents.json", params=self._build_params(keyword, page))
        try:
            return self._process_search_response(response)
        finally:
            if response is not None:
                await response.aclose()

    def test_connection(self) -> tuple[bool, str]:
        """通过 user.json 校验开放 API 令牌与当前用户资料，不访问网页首页。"""
        if not self._api_token:
            return False, "请配置站点 API Key 或 Token"
        response = RequestUtils(
            headers=self._request_headers(), proxies=self._proxy, timeout=self._timeout,
        ).get_res(url=f"{self._base_url}/user.json")
        try:
            data = self._response_data(response, "连接测试")
            if not isinstance(data, dict) or not data.get("id"):
                return False, self.error_detail or "FluxTorrent API 未返回有效用户资料"
            return True, "连接成功"
        finally:
            if response is not None:
                response.close()
