"""通过猪猪实验室 API 搜索种子、解释目录标签并生成按需换票链接。"""

import base64
import hashlib
import json
import re
from typing import Any, Optional, TypeAlias, cast
from urllib.parse import urljoin, urlparse

from app.core.cache import MemoryBackend
from app.core.config import settings
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils

API_URL = "https://api.piggo.me/v1/api"
TOKEN_REQUIRED = "请在猪猪网页端「实验室」生成长期存取令牌，并填入站点 API Key 或 Token"

# 分类 ID 可跨分区复用；标签字典按实际分区保存，0 表示全站字典。
_Catalog: TypeAlias = tuple[dict[int, MediaType], dict[tuple[int, int], MediaType], dict[int, dict[int, str]]]


def access_token(apikey: Optional[str], token: Optional[str]) -> str:
    """优先选择非空 API Key，兼容 Token 中的实验室长期存取令牌。"""
    return str(apikey or "").strip() or str(token or "").strip()


def response_data(status_code: int, payload: object, token: str = "") -> dict[str, Any]:
    """
    校验实验室 HTTP 与业务信封，保留鉴权、限流和必读公告的失败语义。

    :param status_code: 原始 HTTP 状态码
    :param payload: 已解析的 JSON 响应
    :param token: 本次凭据，仅用于错误文本脱敏
    :return: 成功响应的 data 对象
    :raises ValueError: 响应或业务状态无效
    """
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data")
    details = data if isinstance(data, dict) else {}
    code = str(payload.get("code"))
    inner_code = str(details.get("code", "0"))
    message = str(details.get("message") or payload.get("msg") or payload.get("message") or "").strip()
    if code == "notice_unread" or inner_code == "notice_unread":
        gate = details.get("notice_gate") or {}
        title = str(gate.get("title") or "") if isinstance(gate, dict) else ""
        message = f"有未读必读公告，请到猪猪网页端阅读后重试{f'：{title}' if title else ''}"
    elif status_code == 401 or code == "401" or inner_code == "401":
        message = "猪猪存取令牌已过期或无效，请重新生成长期存取令牌"
    elif status_code == 429 or code == "429" or inner_code == "429":
        message = f"站点限流：{message or 'HTTP 429，请稍后重试'}"
    elif not payload and status_code == 200:
        message = "猪猪 API 响应结构无效"
    elif status_code != 200 or code != "0" or inner_code != "0":
        failure_code = inner_code if inner_code != "0" else code
        message = f"猪猪 API 请求失败，HTTP {status_code}，业务码 {failure_code}{f'：{message}' if message else ''}"
    elif isinstance(data, dict):
        return data
    else:
        message = "猪猪 API 响应缺少有效 data"
    if token:
        message = message.replace(token, "[已隐藏]")
    raise ValueError(" ".join(message.split())[:300])


def integer(value: object, minimum: int = 0) -> int:
    """校验实验室整数 ID、计数与字节字段，拒绝布尔值和浮点截断。"""
    if not isinstance(value, (str, int)) or isinstance(value, bool) \
            or not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise ValueError("猪猪 API 整数字段无效")
    number = int(value)
    if number < minimum:
        raise ValueError("猪猪 API 整数字段无效")
    return number


def download_enclosure(api_url: str, torrent_id: object, token: str, proxy: bool = False,
                       ua: Optional[str] = None) -> str:
    """
    构造 NEO 两段式下载配方，仅在实际下载时调用 download-link 换票。

    不设置 result_base_url，确保站点返回的前端下载主机不会被 API 主机覆盖。
    """
    if not token or not re.fullmatch(r"[1-9][0-9]*", str(torrent_id or "")):
        return ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if ua:
        headers["User-Agent"] = ua
    config = {
        "method": "post",
        "cookie": False,
        "header": headers,
        "json": {},
        "allow_redirects": False,
        "proxy": proxy,
        "protocol": "Piggo",
        "result": "data.url",
    }
    encoded = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrents/{torrent_id}/download-link"


class PiggoSpider:
    """猪猪实验室长期令牌索引器，不访问旧 PHP 页面或伪造促销信息。"""

    _size = 100
    _catalog_cache = MemoryBackend(maxsize=128, ttl=3600)
    _catalog_region = "piggo-catalog"
    manages_request_throttle = True

    def __init__(self, indexer: dict[str, Any]) -> None:
        """保存站点认证与请求配置，目录缓存按 API 地址及凭据摘要隔离。"""
        indexer = indexer or {}
        self._name = indexer.get("name") or "猪猪"
        self._site_url = str(indexer.get("domain") or indexer.get("url") or "https://piggo.me/")
        self._api_url = str(indexer.get("api_url") or API_URL).rstrip("/")
        self._token = access_token(indexer.get("apikey"), indexer.get("token"))
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._ua = indexer.get("ua") or settings.USER_AGENT
        self._timeout = indexer.get("timeout") or 15
        self._cache_key = hashlib.sha256(f"{self._api_url}\n{self._token}".encode("utf-8")).hexdigest()
        self.error_detail: Optional[str] = None

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """返回实验室搜索与最新列表共用的每页一百条容量。"""
        return cls._size

    def _headers(self) -> dict[str, str]:
        """构造 Bearer 与 JSON 请求头，不携带站点 Cookie。"""
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._ua,
        }

    def _response_data(self, response: Any) -> dict[str, Any]:
        """提取有效业务数据，响应失败时删除当前凭据的目录缓存。"""
        if response is None:
            raise ValueError("无法连接猪猪实验室 API")
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        try:
            return response_data(response.status_code, payload, self._token)
        except ValueError:
            self._catalog_cache.delete(self._cache_key, region=self._catalog_region)
            raise

    def _request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """发送一次同步 POST JSON，禁止认证请求跟随重定向并释放响应。"""
        response = RequestUtils(
            headers=self._headers(), proxies=self._proxy, timeout=self._timeout,
        ).post_res(url=f"{self._api_url}/{path}", json=body, allow_redirects=False)
        try:
            return self._response_data(response)
        finally:
            if response is not None:
                response.close()

    async def _async_request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """发送一次异步 POST JSON，失败或取消时同样释放响应。"""
        response = await AsyncRequestUtils(
            headers=self._headers(), proxies=self._proxy, timeout=self._timeout,
        ).post_res(url=f"{self._api_url}/{path}", json=body, allow_redirects=False)
        try:
            return self._response_data(response)
        finally:
            if response is not None:
                await response.aclose()

    @staticmethod
    def _parse_tags(value: object) -> dict[int, str]:
        """校验真实标签字典，拒绝重复 ID、空名称和非列表结构。"""
        if not isinstance(value, list):
            raise ValueError("猪猪标签字典无效")
        tags: dict[int, str] = {}
        try:
            for tag in value:
                tag_id = integer(tag["id"], minimum=1)
                name = tag["name"]
                if tag_id in tags or not isinstance(name, str) or not name.strip():
                    raise ValueError("猪猪标签数据无效")
                tags[tag_id] = name.strip()
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("猪猪标签字典无效") from err
        return tags

    @staticmethod
    def _tag_ids(item: dict[str, Any]) -> list[int]:
        """读取并去重种子的独立标签 ID，不从标题或二级分类猜造标签。"""
        values = item.get("tag_ids", [])
        if not isinstance(values, list):
            raise ValueError("猪猪种子标签数据无效")
        return list(dict.fromkeys(integer(value, minimum=1) for value in values))

    @classmethod
    def _tag_labels(cls, item: dict[str, Any], catalogs: dict[int, dict[int, str]]) -> list[str]:
        """按实际分区字典解释标签，未知名称不显示数值占位。"""
        section_id = integer(item.get("section_id", 0))
        tags = catalogs.get(section_id, catalogs.get(0, {}))
        return list(dict.fromkeys(tags[tag_id] for tag_id in cls._tag_ids(item) if tag_id in tags))

    @staticmethod
    def _tag_sections(data: dict[str, Any], catalog: _Catalog) -> list[int]:
        """提取当前页尚未缓存且确有标签的分区，避免逐种子请求目录。"""
        return list(dict.fromkeys(
            integer(item["section_id"], minimum=1)
            for item in data["items"]
            if item.get("tag_ids") and item.get("section_id") and int(item["section_id"]) not in catalog[2]
        ))

    @classmethod
    def _apply_tag_labels(cls, torrents: list[dict[str, Any]], data: dict[str, Any], catalog: _Catalog) -> None:
        """分区字典全部读取成功后，一次性更新当前页标签。"""
        for torrent, item in zip(torrents, data["items"], strict=True):
            torrent["labels"] = cls._tag_labels(item, catalog[2])

    @staticmethod
    def _name_media_type(name: str) -> MediaType:
        """按目录名称识别明确媒体类型，动漫和纪录片等歧义目录保持未知。"""
        name = name.casefold()
        for media_type, labels in (
            (MediaType.MOVIE, ("电影", "電影", "movie", "cinema")),
            (MediaType.TV, ("剧", "劇", "综艺", "綜藝", "series", "television", "tv")),
        ):
            if any(label in name for label in labels):
                return media_type
        return MediaType.UNKNOWN

    @classmethod
    def _parse_catalog(cls, data: dict[str, Any]) -> _Catalog:
        """校验当前账号可见目录，返回分区类型、分类成员关系和全站标签。"""
        sections, categories = data.get("sections"), data.get("categories")
        if not isinstance(sections, list) or not isinstance(categories, list):
            raise ValueError("猪猪目录响应结构无效")
        section_map: dict[int, MediaType] = {}
        category_map: dict[tuple[int, int], MediaType] = {}
        try:
            for section in sections:
                section_id = integer(section["id"], minimum=1)
                if section_id in section_map or not isinstance(section["name"], str):
                    raise ValueError("猪猪分区数据无效")
                section_map[section_id] = cls._name_media_type(section["name"])
            for category in categories:
                category_id = integer(category["id"], minimum=1)
                section_id = integer(category["section_id"], minimum=1)
                membership = section_id, category_id
                if membership in category_map or section_id not in section_map \
                        or not isinstance(category["name"], str):
                    raise ValueError("猪猪分类数据无效")
                media_type = cls._name_media_type(category["name"])
                category_map[membership] = media_type if media_type != MediaType.UNKNOWN else section_map[section_id]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("猪猪目录响应结构无效") from err
        return section_map, category_map, {0: cls._parse_tags(data.get("tags", []))}

    def _load_catalog(self) -> _Catalog:
        """读取一小时有界目录缓存，失败目录不缓存、不回退旧分类。"""
        catalog = cast(Optional[_Catalog], self._catalog_cache.get(self._cache_key, region=self._catalog_region))
        if catalog is None:
            catalog = self._parse_catalog(self._request("catalog", {}))
            self._catalog_cache.set(self._cache_key, catalog, region=self._catalog_region)
        return catalog

    async def _async_load_catalog(self) -> _Catalog:
        """异步读取当前 API 地址与凭据对应的可见目录。"""
        catalog = cast(Optional[_Catalog], self._catalog_cache.get(self._cache_key, region=self._catalog_region))
        if catalog is None:
            catalog = self._parse_catalog(await self._async_request("catalog", {}))
            self._catalog_cache.set(self._cache_key, catalog, region=self._catalog_region)
        return catalog

    def _params(self, keyword: Optional[str], mtype: Optional[MediaType], cat: Optional[str],
                page: Optional[int], catalog: _Catalog) -> dict[str, Any]:
        """构造正式搜索参数，多分区使用完整分类并集以保持全局分页。"""
        params: dict[str, Any] = {"page": int(page or 0) + 1, "per_page": self._size}
        value = str(keyword or "").strip()
        if value:
            if re.fullmatch(r"tt[0-9]+", value, re.IGNORECASE):
                params["imdb_id"] = value.lower()
            else:
                params["q"] = value
        if cat:
            if not re.fullmatch(r"[1-9][0-9]*(\s*,\s*[1-9][0-9]*)*", cat.strip()):
                raise ValueError("猪猪分类须填写当前目录中的正整数 ID，以逗号分隔")
            params["category_id"] = list(dict.fromkeys(int(value) for value in cat.split(",")))
            return params
        if mtype is None or mtype == MediaType.UNKNOWN:
            return params
        sections, categories, _tags = catalog
        # 有未能判型的可见分区时不过滤，避免误排除纪录片、动漫等混合目录。
        if MediaType.UNKNOWN in sections.values():
            return params
        matched = {section_id for section_id, media_type in sections.items() if media_type == mtype}
        if len(matched) == 1:
            params["section_id"] = next(iter(matched))
        elif matched and all(any(section_id == membership[0] for membership in categories) for section_id in matched):
            params["category_id"] = list(dict.fromkeys(
                category_id for section_id, category_id in categories if section_id in matched
            ))
        elif not matched:
            category_ids = list(dict.fromkeys(
                membership[1] for membership, media_type in categories.items() if media_type == mtype
            ))
            if category_ids:
                params["category_id"] = category_ids
        return params

    @staticmethod
    def _torrent_media_type(item: dict[str, Any], media: dict[str, Any], catalog: _Catalog) -> MediaType:
        """优先采用作品类型，再按当前目录判定，冲突或未知分类不强行归类。"""
        media_type = {"movie": MediaType.MOVIE, "tv": MediaType.TV}.get(str(media.get("tmdb_kind") or ""))
        if media_type is not None:
            return media_type
        sections, categories, _tags = catalog
        category_ids = item.get("category_ids", [])
        if not isinstance(category_ids, list):
            raise ValueError("猪猪种子分类数据无效")
        section_id = integer(item.get("section_id", 0))
        known = set()
        for cid in category_ids:
            category = categories.get((section_id, integer(cid, minimum=1)))
            if category is not None and category != MediaType.UNKNOWN:
                known.add(category)
        if len(known) == 1:
            return next(iter(known))
        if known:
            return MediaType.UNKNOWN
        return sections.get(section_id, MediaType.UNKNOWN)

    @staticmethod
    def _media_imdb_id(media: dict[str, Any]) -> Optional[str]:
        """校验并提取标准 IMDb ID，避免将任意文本当作精确身份。"""
        value = media.get("imdb")
        if value in (None, ""):
            return None
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError("猪猪作品标识数据无效")
        media_id = str(value).strip()
        if not re.fullmatch(r"tt[0-9]+", media_id, re.IGNORECASE):
            raise ValueError("猪猪作品标识数据无效")
        return media_id.lower()

    def _parse_item(self, item: dict[str, Any], catalog: _Catalog) -> dict[str, Any]:
        """将一条实验室种子记录转换为 NEO 标准搜索字段。"""
        torrent_id = integer(item["id"], minimum=1)
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError("猪猪种子名称无效")
        for field in ("small_descr", "page_url", "added_at"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ValueError("猪猪种子文本字段无效")
        media = item.get("media") or {}
        if not isinstance(media, dict):
            raise ValueError("猪猪作品标识数据无效")
        media_type = self._torrent_media_type(item, media, catalog)
        return {
            "title": item["name"],
            "description": item.get("small_descr"),
            "enclosure": self.get_download_url(torrent_id),
            "page_url": item.get("page_url") or urljoin(self._site_url, f"detail/{torrent_id}"),
            "pubdate": StringUtils.unify_datetime_str(item.get("added_at")),
            "size": integer(item["size"]),
            "seeders": integer(item.get("seeders", 0)),
            "peers": integer(item.get("leechers", 0)),
            "grabs": integer(item.get("times_completed", 0)),
            "imdbid": self._media_imdb_id(media),
            "category": media_type.value,
            "labels": self._tag_labels(item, catalog[2]),
        }

    def _parse_result(self, data: dict[str, Any], catalog: _Catalog) -> list[dict[str, Any]]:
        """校验整页后再发布结果，坏条目或重复 ID 不形成半页成功。"""
        items = data.get("items")
        if not isinstance(items, list) or len(items) > self._size:
            raise ValueError("猪猪种子列表响应缺少有效 items")
        torrents: list[dict[str, Any]] = []
        ids: set[int] = set()
        try:
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("猪猪种子条目无效")
                torrent_id = integer(item["id"], minimum=1)
                if torrent_id in ids:
                    raise ValueError("猪猪种子 ID 重复")
                ids.add(torrent_id)
                torrents.append(self._parse_item(item, catalog))
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise ValueError("猪猪种子列表响应结构无效") from err
        return torrents

    def search(self, keyword: Optional[str] = None, mtype: Optional[MediaType] = None,
               cat: Optional[str] = None, page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """同步搜索或浏览最新种子，目录、列表和分区标签全部成功才返回结果。"""
        self.error_detail = None
        try:
            if not self._token:
                raise ValueError(TOKEN_REQUIRED)
            catalog = self._load_catalog()
            data = self._request("torrents", self._params(keyword, mtype, cat, page, catalog))
            torrents = self._parse_result(data, catalog)
            for section_id in self._tag_sections(data, catalog):
                tags = self._request("catalog", {"section_id": section_id})
                catalog[2][section_id] = self._parse_tags(tags.get("tags", []))
            self._apply_tag_labels(torrents, data, catalog)
            return False, torrents
        except ValueError as err:
            self.error_detail = str(err)
            logger.warning(f"{self._name} 搜索失败：{self.error_detail}")
            return True, []

    async def async_search(self, keyword: Optional[str] = None, mtype: Optional[MediaType] = None,
                           cat: Optional[str] = None, page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """异步执行与同步入口一致的实验室搜索和标签映射协议。"""
        self.error_detail = None
        try:
            if not self._token:
                raise ValueError(TOKEN_REQUIRED)
            catalog = await self._async_load_catalog()
            data = await self._async_request("torrents", self._params(keyword, mtype, cat, page, catalog))
            torrents = self._parse_result(data, catalog)
            for section_id in self._tag_sections(data, catalog):
                tags = await self._async_request("catalog", {"section_id": section_id})
                catalog[2][section_id] = self._parse_tags(tags.get("tags", []))
            self._apply_tag_labels(torrents, data, catalog)
            return False, torrents
        except ValueError as err:
            self.error_detail = str(err)
            logger.warning(f"{self._name} 搜索失败：{self.error_detail}")
            return True, []

    def get_download_url(self, torrent_id: object) -> str:
        """使用当前凭据生成按需换票地址，最终下载不携带 Bearer 或 Cookie。"""
        return download_enclosure(self._api_url, torrent_id, self._token, self._use_proxy, ua=self._ua)

    def test_connection(self) -> tuple[bool, str]:
        """通过 /me 校验长期令牌、账号状态及下载权限，不访问网页首页。"""
        if not self._token:
            return False, TOKEN_REQUIRED
        try:
            data = self._request("me", {})
            user = data.get("user")
            if not isinstance(user, dict):
                return False, "猪猪 API 未返回有效用户资料"
            integer(user.get("id"), minimum=1)
            if user.get("enabled") is False:
                return False, "猪猪账号已被禁用"
            if user.get("download_allowed") is False:
                return False, "当前猪猪账号没有下载权限"
            return True, "连接成功"
        except ValueError as err:
            return False, str(err)

    @staticmethod
    def parse_download_response(status_code: Optional[int], payload: object,
                                request_params: dict[str, Any]) -> str:
        """校验换票响应并保留站点返回的完整下载主机和路径。"""
        if status_code is None:
            raise ValueError("无法连接猪猪实验室 API")
        headers = request_params.get("header") or {}
        token = str(headers.get("Authorization") or "").removeprefix("Bearer ")
        data = response_data(status_code, payload, token)
        url = data.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("猪猪 API 未返回有效种子下载地址")
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("猪猪 API 未返回有效种子下载地址")
        if parts.username is not None or parts.password is not None:
            raise ValueError("猪猪 API 返回的下载地址含无效认证信息")
        logger.info("已获取到站点临时下载地址")
        return url
