"""通过 FluxTorrent 的 NexusPHP 只读兼容层搜索种子并检查连接。"""

import asyncio
import threading
import time
from typing import Any, Optional

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils

_COMPAT_PREFIX = "/api/v1/compat/nexusphp"
# 网关偶发 503 时，只读请求最多重试两次，分别退避三秒和六秒。
_GATEWAY_RETRY_SECONDS = 3
_GATEWAY_RETRIES = 2

_site_request_lock = threading.Lock()
_site_request_windows: dict[str, list[float]] = {}

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


def reset_fluxtorrent_throttle_for_tests() -> None:
    """清理内存中的站点请求流控窗口，供单元测试隔离使用。"""
    with _site_request_lock:
        _site_request_windows.clear()


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


def is_fluxtorrent_download_url(url: Optional[str]) -> bool:
    """判断下载地址是否为 FluxTorrent 兼容层种子下载接口。"""
    return bool(url and f"{_COMPAT_PREFIX}/" in str(url))


def _safe_non_negative_int(value: Any) -> int:
    """将可选流控参数安全转换为非负整数。"""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def site_request_limits(site: Optional[dict[str, Any]]) -> dict[str, Any]:
    """
    构造兼容层请求共用的无凭据流控配置。

    保留站点设置的请求间隔与单位时间窗口上限，使连通性测试、搜索与用户资料刷新共享配额。
    """
    site = site or {}
    base_url = site_base_url(site)
    domain = StringUtils.get_url_domain(base_url or str(site.get("domain") or ""))
    return {
        "url": base_url,
        "domain": domain,
        "limit_interval": _safe_non_negative_int(site.get("limit_interval")),
        "limit_count": _safe_non_negative_int(site.get("limit_count")),
        "limit_seconds": _safe_non_negative_int(site.get("limit_seconds")),
    }


def site_request_config(site_url: Optional[str]) -> dict[str, Any]:
    """
    按站点地址读取数据库中的流控配置快照。

    数据库不可用或站点未入库时回退为空流控配置，不抛出异常阻断调用链。
    """
    domain = StringUtils.get_url_domain(str(site_url or ""))
    if not domain:
        return {"url": str(site_url or "")}
    try:
        site_obj = SiteOper().get_by_domain(domain)
    except Exception as err:
        logger.debug(f"读取站点 {domain} 流控配置失败：{err}")
        site_obj = None
    if not site_obj:
        return {"url": str(site_url or ""), "domain": domain}
    raw_url = getattr(site_obj, "url", None)
    raw_domain = getattr(site_obj, "domain", None)
    resolved_url = raw_url if isinstance(raw_url, str) and raw_url.strip() else str(site_url or "")
    resolved_domain = (
        StringUtils.get_url_domain(raw_domain)
        if isinstance(raw_domain, str) and raw_domain.strip()
        else domain
    )
    return {
        "url": resolved_url,
        "domain": resolved_domain or domain,
        "limit_interval": getattr(site_obj, "limit_interval", None),
        "limit_count": getattr(site_obj, "limit_count", None),
        "limit_seconds": getattr(site_obj, "limit_seconds", None),
    }


def _reserve_site_request_slot(
    limits: Optional[dict[str, Any]],
    now: Optional[float] = None,
) -> tuple[str, float, float]:
    """在锁内计算并预订下一次站点请求槽位，返回域名、预订时刻与需等待秒数。"""
    limits = limits or {}
    domain = str(
        limits.get("domain")
        or StringUtils.get_url_domain(str(limits.get("url") or ""))
        or ""
    ).strip()
    if not domain:
        return "", 0.0, 0.0
    window = _safe_non_negative_int(limits.get("limit_interval"))
    count = _safe_non_negative_int(limits.get("limit_count"))
    min_spacing = _safe_non_negative_int(limits.get("limit_seconds"))
    if min_spacing <= 0 and (count <= 0 or window <= 0):
        return domain, 0.0, 0.0

    with _site_request_lock:
        current = time.monotonic() if now is None else now
        timestamps = _site_request_windows.setdefault(domain, [])
        horizon = max(window, min_spacing, 1)
        timestamps[:] = [ts for ts in timestamps if ts > current - horizon]
        scheduled = current
        if min_spacing > 0 and timestamps:
            scheduled = max(scheduled, timestamps[-1] + min_spacing)
        if count > 0 and window > 0 and len(timestamps) >= count:
            scheduled = max(scheduled, timestamps[-count] + window)
        timestamps.append(scheduled)
        return domain, scheduled, max(0.0, scheduled - current)


def _release_cancelled_slot(domain: str, scheduled: float) -> None:
    """异步等待被取消时释放尚未消费的预订请求槽位。"""
    if not domain or scheduled <= 0:
        return
    with _site_request_lock:
        timestamps = _site_request_windows.get(domain)
        if timestamps and scheduled in timestamps:
            timestamps.remove(scheduled)


def wait_for_site_request(limits: Optional[dict[str, Any]]) -> None:
    """同步按站点流控配置等待可用请求额度。"""
    _, _, delay = _reserve_site_request_slot(limits)
    if delay > 0:
        time.sleep(delay)


async def async_wait_for_site_request(limits: Optional[dict[str, Any]]) -> None:
    """异步按站点流控配置等待可用请求额度，协程取消时归还预订槽位。"""
    domain, scheduled, delay = _reserve_site_request_slot(limits)
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            _release_cancelled_slot(domain, scheduled)
            raise


def _close_sync_response(response: Any) -> None:
    """安全释放同步 HTTP 响应连接。"""
    close_fn = getattr(response, "close", None)
    if callable(close_fn):
        close_fn()


async def _close_async_response(response: Any) -> None:
    """安全释放异步 HTTP 响应连接。"""
    aclose_fn = getattr(response, "aclose", None)
    if callable(aclose_fn):
        await aclose_fn()


def get_api_response(
    request: RequestUtils,
    url: str,
    limits: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
) -> Any:
    """
    同步申请站点流控额度请求兼容层只读 API，遇到网关 503 最多退避重试两次。

    :param request: 已配置认证头与代理的同步请求客户端
    :param url: 只读 API 完整地址
    :param limits: 无凭据的站点流控配置
    :param params: 可选查询参数
    :return: 最后一次 HTTP 响应对象或 None，由调用方解析并关闭
    """
    response = None
    for attempt in range(_GATEWAY_RETRIES + 1):
        wait_for_site_request(limits)
        response = request.get_res(url=url, params=params, allow_redirects=False)
        if response is None or getattr(response, "status_code", 0) != 503 or attempt == _GATEWAY_RETRIES:
            break
        _close_sync_response(response)
        time.sleep(_GATEWAY_RETRY_SECONDS * (attempt + 1))
    return response


async def async_get_api_response(
    request: AsyncRequestUtils,
    url: str,
    limits: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
) -> Any:
    """
    异步申请站点流控额度请求兼容层只读 API，遇到网关 503 最多退避重试两次。

    :param request: 已配置认证头与代理的异步请求客户端
    :param url: 只读 API 完整地址
    :param limits: 无凭据的站点流控配置
    :param params: 可选查询参数
    :return: 最后一次 HTTP 响应对象或 None，由调用方解析并关闭
    """
    response = None
    for attempt in range(_GATEWAY_RETRIES + 1):
        await async_wait_for_site_request(limits)
        response = await request.get_res(url=url, params=params, allow_redirects=False)
        if response is None or getattr(response, "status_code", 0) != 503 or attempt == _GATEWAY_RETRIES:
            break
        await _close_async_response(response)
        await asyncio.sleep(_GATEWAY_RETRY_SECONDS * (attempt + 1))
    return response


class FluxTorrentSpider:
    """FluxTorrent 兼容层索引器，只调用 JSON 接口而不刮削站点页面。"""

    _size = 50
    # 每次实际 API 请求自行申请流控额度，索引入口无需重复检查跳过。
    manages_request_throttle = True

    def __init__(self, indexer: dict[str, Any]) -> None:
        """保存站点地址、开放 API 令牌、浏览器 User-Agent 及流控上下文。"""
        indexer = indexer or {}
        self._name = indexer.get("name") or "FluxTorrent"
        self._site_url = site_base_url(indexer)
        self._api_token = site_api_token(indexer)
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._user_agent = self.get_user_agent(indexer.get("ua"))
        self._timeout = indexer.get("timeout") or 30
        self._rate_site = site_request_limits(indexer)
        self.error_detail: Optional[str] = None

    @staticmethod
    def get_user_agent(ua: Optional[str]) -> str:
        """
        为 API 请求和种子下载选择站点自定义或默认浏览器 User-Agent。

        好学等网关会直接拦截默认 MoviePilot 标识并返回 HTTP 503，因此未配置或仍为宿主默认
        User-Agent 时自动切换为标准浏览器标识，显式自定义值保持优先。
        """
        if not ua or ua == settings.USER_AGENT:
            return str(settings.NORMAL_USER_AGENT)
        return ua

    site_request_limits = staticmethod(site_request_limits)
    site_request_config = staticmethod(site_request_config)
    get_api_response = staticmethod(get_api_response)
    async_get_api_response = staticmethod(async_get_api_response)

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
        response = get_api_response(
            RequestUtils(
                headers=self._request_headers(),
                proxies=self._proxy,
                timeout=self._timeout,
            ),
            url=f"{self._base_url}/torrents.json",
            limits=self._rate_site,
            params=self._build_params(keyword, page),
        )
        try:
            return self._process_search_response(response)
        finally:
            _close_sync_response(response)

    async def async_search(self, keyword: Optional[str] = None,
                           page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """异步搜索 FluxTorrent 站点种子，并遵循同步接口相同字段契约。"""
        self.error_detail = None
        if not self._api_token:
            self.error_detail = "未配置开放 API 令牌"
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        response = await async_get_api_response(
            AsyncRequestUtils(
                headers=self._request_headers(),
                proxies=self._proxy,
                timeout=self._timeout,
            ),
            url=f"{self._base_url}/torrents.json",
            limits=self._rate_site,
            params=self._build_params(keyword, page),
        )
        try:
            return self._process_search_response(response)
        finally:
            await _close_async_response(response)

    def test_connection(self) -> tuple[bool, str]:
        """通过 user.json 校验开放 API 令牌与当前用户资料，不访问网页首页。"""
        if not self._api_token:
            return False, "请配置站点 API Key 或 Token"
        response = get_api_response(
            RequestUtils(
                headers=self._request_headers(),
                ua=self._user_agent,
                proxies=self._proxy,
                timeout=self._timeout,
            ),
            url=f"{self._base_url}/user.json",
            limits=self._rate_site,
        )
        try:
            data = self._response_data(response, "连接测试")
            if not isinstance(data, dict) or not data.get("id"):
                return False, self.error_detail or "FluxTorrent API 未返回有效用户资料"
            return True, "连接成功"
        finally:
            _close_sync_response(response)
