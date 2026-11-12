"""读取猪猪实验室用户资料及完整做种、下载分页，失败时不发布半份统计。"""

import json
import math
import re
from typing import Any, Optional

import app.modules.indexer.spider.piggo as piggo_rules
from app.core.config import settings
from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.http import RequestUtils


class PiggoSiteUserInfo(SiteParserBase):
    """猪猪长期存取令牌用户数据解析器，不回退到已移除的 NexusPHP 页面。"""

    schema = SiteSchema.Piggo
    request_mode = "apikey"
    allow_schema_fallback = False
    _size = 100

    def __init__(self, site_name: Optional[str], url: str, site_cookie: Optional[str],
                 apikey: Optional[str], token: Optional[str],
                 request_utils: Optional[RequestUtils] = None, ua: Optional[str] = None,
                 emulate: bool = False, proxy: Optional[bool] = None,
                 api_url: Optional[str] = None) -> None:
        """
        使用无网页会话的客户端读取资料，Cookie 与浏览器仿真不参与实验室鉴权。

        :param site_name: 站点展示名称
        :param url: 站点网页地址
        :param site_cookie: 保留基类签名，不用于实验室请求
        :param apikey: 首选长期存取令牌
        :param token: API Key 为空时使用的长期存取令牌
        :param request_utils: 可注入的离线或同步 HTTP 客户端
        :param ua: 请求 User-Agent
        :param emulate: 保留基类签名，不启动浏览器
        :param proxy: 是否使用系统代理
        :param api_url: 资源声明的独立 API 地址
        """
        super().__init__(site_name=site_name, url=url, site_cookie=None, apikey=apikey, token=token,
                         ua=ua, emulate=False, proxy=proxy, api_url=api_url)
        self._request_utils = request_utils or RequestUtils()

    def parse(self) -> None:
        """仅在资料和两类种子的全部分页成功后发布用户 ID 与统计。"""
        self.userid = None
        self.err_msg = None
        try:
            self._parse_site_page({})
            if not self._access_token:
                raise ValueError(piggo_rules.TOKEN_REQUIRED)
            self._parse_user_base_info(self._get_page_content(f"{self._base_url}/me"))
            self._parse_seeding_pages()
        except ValueError as err:
            self.err_msg = str(err)
            logger.warning(f"{self._site_name} 用户数据刷新失败：{self.err_msg}")
        finally:
            self.close()

    def _parse_site_page(self, html_text: Any) -> None:
        """配置独立 API 与长期令牌，忽略旧网页参数和 Cookie。"""
        self._base_url = str(self._api_url or piggo_rules.API_URL).rstrip("/")
        self._access_token = piggo_rules.access_token(self.apikey, self.token)
        self._profile: dict[str, Any] = {}

    def _get_page_content(self, url: str, params: Optional[dict[str, object]] = None,
                          headers: Optional[dict[str, Optional[str]]] = None,
                          method: Optional[str] = None) -> str:
        """强制 POST JSON，校验 HTTP 与业务信封后返回 data 的 JSON 文本。"""
        response = self._request_utils.post_res(
            url=url,
            json=params or {},
            cookies=None,
            timeout=60,
            allow_redirects=False,
            proxies=settings.PROXY if self._proxy else None,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self._ua or settings.USER_AGENT,
            },
        )
        try:
            if response is None:
                raise ValueError("无法连接猪猪实验室 API")
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            data = piggo_rules.response_data(response.status_code, payload, self._access_token)
            return json.dumps(data)
        finally:
            if response is not None:
                response.close()

    def _parse_user_base_info(self, html_text: Any) -> None:
        """暂存已校验的用户资料，上传下载容量转换为字节并保留等级和魔力。"""
        if not isinstance(html_text, str):
            raise ValueError("猪猪用户资料响应结构无效")
        data = json.loads(html_text)
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not user.get("id") or not user.get("username"):
            raise ValueError("猪猪 API 未返回有效用户资料")
        if user.get("enabled") is False:
            raise ValueError("猪猪账号已被禁用")
        stats = user.get("stats")
        level = user.get("class")
        if not isinstance(stats, dict) or not isinstance(level, dict):
            raise ValueError("猪猪用户资料缺少流量或等级信息")
        if not isinstance(user["username"], str) or not user["username"].strip():
            raise ValueError("猪猪 API 未返回有效用户名")
        user_id = piggo_rules.integer(user["id"], minimum=1)
        upload = self._traffic_size(stats.get("uploaded"))
        download = self._traffic_size(stats.get("downloaded"))
        try:
            bonus = float(stats.get("bonus", 0))
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("猪猪用户魔力值无效") from err
        if not math.isfinite(bonus) or bonus < 0 or not isinstance(level.get("name"), str):
            raise ValueError("猪猪用户魔力值或等级信息无效")
        unread = data.get("unread_notifications", user.get("unread_notifications", 0))
        self._profile = {
            "userid": user_id,
            "username": user["username"],
            "user_level": level["name"],
            "upload": upload,
            "download": download,
            "bonus": bonus,
            "message_unread": piggo_rules.integer(unread),
        }

    def _traffic_size(self, value: object) -> int:
        """校验有限非负容量文本，缺失或非法流量不得默认为零。"""
        text = str(value).strip()
        if not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:[KMGTP]I?B|B)?", text, re.IGNORECASE):
            raise ValueError("猪猪用户资料缺少有效上传或下载流量")
        try:
            return self.num_filesize(text)
        except (ValueError, OverflowError) as err:
            raise ValueError("猪猪用户流量容量无效") from err

    @classmethod
    def _torrent_page(cls, data: object, kind: str,
                      page: int) -> tuple[list[dict[str, Any]], tuple[int, int, int]]:
        """校验单页元数据、完整条目和容量，空列表同样必须具有分页信息。"""
        if not isinstance(data, dict) or data.get("kind") != kind:
            raise ValueError("猪猪种子分页类型无效")
        pages = piggo_rules.integer(data.get("pages"))
        total = piggo_rules.integer(data.get("total"))
        per_page = piggo_rules.integer(data.get("per_page"), minimum=1)
        if per_page > cls._size or piggo_rules.integer(data.get("page"), minimum=1) != page:
            raise ValueError("猪猪种子分页容量或页码无效")
        expected_pages = max(1, (total + per_page - 1) // per_page)
        if pages != expected_pages and not (total == 0 and pages == 0):
            raise ValueError("猪猪种子页数无效")
        page_items = data.get("items")
        expected_count = min(per_page, max(0, total - (page - 1) * per_page))
        if not isinstance(page_items, list) or len(page_items) != expected_count:
            raise ValueError("猪猪种子分页不完整")
        items = []
        for item in page_items:
            if not isinstance(item, dict):
                raise ValueError("猪猪种子分页条目无效")
            items.append({
                "id": piggo_rules.integer(item.get("id"), minimum=1),
                "size": piggo_rules.integer(item.get("size")),
                "seeders": piggo_rules.integer(item.get("seeders", 0)),
            })
        return items, (pages, total, per_page)

    def _read_torrents(self, kind: str) -> list[dict[str, Any]]:
        """读取全部种子分页，拒绝错页、重复 ID 及变化的总数或分页容量。"""
        items: list[dict[str, Any]] = []
        page, expected = 1, None
        ids = set()
        while True:
            data = json.loads(self._get_page_content(
                f"{self._base_url}/me/torrents",
                {"kind": kind, "page": page, "per_page": self._size},
            ))
            try:
                page_items, metadata = self._torrent_page(data, kind, page)
                if expected is not None and expected != metadata:
                    raise ValueError("猪猪种子分页发生变化")
                expected = metadata
                for item in page_items:
                    torrent_id = item["id"]
                    if torrent_id in ids:
                        raise ValueError("猪猪种子分页数据无效")
                    ids.add(torrent_id)
                items.extend(page_items)
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("猪猪用户种子分页不完整或发生变化，请稍后重试") from err
            if page >= metadata[0]:
                return items
            page += 1

    def _parse_seeding_pages(self) -> None:
        """做种和下载全部读取成功后一次更新统计，最后发布用户 ID。"""
        seeding = self._read_torrents("seeding")
        leeching = self._read_torrents("leeching")
        seeding_info = [[int(item.get("seeders") or 0), int(item["size"])] for item in seeding]
        self.username = self._profile["username"]
        self.user_level = self._profile["user_level"]
        self.upload = self._profile["upload"]
        self.download = self._profile["download"]
        self.bonus = self._profile["bonus"]
        self.ratio = self.upload / self.download if self.download else 0.0
        self.seeding, self.seeding_info = len(seeding), seeding_info
        self.seeding_size = sum(item[1] for item in seeding_info)
        self.leeching = len(leeching)
        self.leeching_size = sum(int(item["size"]) for item in leeching)
        self.message_unread = self._profile["message_unread"]
        self.userid = self._profile["userid"]

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """不访问独立流量页面，实验室用户资料已提供完整流量。"""

    def _parse_user_detail_info(self, html_text: str) -> None:
        """不访问旧用户详情页，实验室未提供的注册时间保持为空。"""

    def _parse_user_torrent_seeding_info(self, html_text: str,
                                         multi_page: bool = False) -> Optional[str]:
        """不使用 HTML 翻页，全部做种及下载分页由专用流程读取。"""
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """实验室没有站内信列表接口，不访问旧消息页面或自动阅读公告。"""
        return None

    def _parse_message_content(self, html_text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """实验室没有站内信详情接口，保持消息内容为空。"""
        return None, None, None
