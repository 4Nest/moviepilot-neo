"""解析 FluxTorrent 兼容层返回的用户资料、流量和未读通知。"""

import json
from typing import Any, Optional

from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema


class FluxTorrentSiteUserInfo(SiteParserBase):
    """FluxTorrent 用户数据解析器，以 user.json 替代 NexusPHP 页面刮削。"""

    schema = SiteSchema.FluxTorrent
    request_mode = "apikey"

    def _parse_site_page(self, html_text: Any) -> None:
        """配置兼容层用户接口与 Token 认证头，禁用所有旧页面入口。"""
        self._user_basic_page = "api/v1/compat/nexusphp/user.json"
        self._user_basic_params = {}
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None
        token = self.apikey or self.token or ""
        headers: dict[str, Optional[str]] = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        if self._ua:
            headers["User-Agent"] = self._ua
        self._addition_headers = headers

    def _load_api_payload(self, html_text: Any, operation: str) -> Optional[dict[str, Any]]:
        """解析并校验兼容层统一响应信封，失败时写入明确错误信息。"""
        if not html_text:
            self.err_msg = f"{operation}失败，未收到接口响应"
            return None
        if isinstance(html_text, dict):
            payload = html_text
        else:
            try:
                payload = json.loads(html_text)
            except (TypeError, json.JSONDecodeError) as err:
                self.err_msg = f"{operation}失败，接口响应不是有效 JSON"
                logger.warning(f"{self._site_name} {self.err_msg}：{str(err)}")
                return None
        if not isinstance(payload, dict):
            self.err_msg = f"{operation}失败，接口响应结构无效"
            logger.warning(f"{self._site_name} {self.err_msg}")
            return None
        if payload.get("code") != 0 and str(payload.get("code")) != "0":
            self.err_msg = payload.get("message") or f"{operation}失败"
            logger.warning(f"{self._site_name} {operation}失败：{self.err_msg}")
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            self.err_msg = f"{operation}失败，接口缺少有效用户资料"
            return None
        return data

    def _parse_user_base_info(self, html_text: Any) -> None:
        """解析用户身份、等级、上传下载量、分享率、魔力与未读通知数量。"""
        user_info = self._load_api_payload(html_text, "获取用户信息")
        if not user_info:
            return
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("class_name") or str(user_info.get("class") or "")
        self.upload = int(user_info.get("uploaded") or 0)
        self.download = int(user_info.get("downloaded") or 0)
        # 只上传未下载时站点以无穷大表示分享率，内部沿用零值合同。
        if user_info.get("ratio_infinite") is True:
            self.ratio = 0.0
        else:
            self.ratio = float(user_info.get("ratio") or 0)
        self.bonus = float(user_info.get("seedbonus") or 0)
        self.message_unread = int(
            user_info.get("unread_messages", user_info.get("unread_notifications", 0)) or 0
        )

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """跳过独立流量页面，用户接口已返回完整统计。"""

    def _parse_user_detail_info(self, html_text: str) -> None:
        """跳过独立用户详情页面，用户接口已返回完整资料。"""

    def _parse_user_torrent_seeding_info(self, html_text: str,
                                         multi_page: bool = False) -> Optional[str]:
        """兼容层未提供做种列表，不访问旧 NexusPHP AJAX 页面。"""
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """兼容层未提供站内信列表，仅保留用户接口返回的未读数量。"""
        return None

    def _parse_message_content(self, html_text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """兼容层未提供消息正文接口，返回空标题、时间和内容。"""
        return None, None, None
