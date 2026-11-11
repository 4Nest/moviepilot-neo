# -*- coding: utf-8 -*-
import html
import json
import re
from typing import Optional, Tuple
from urllib.parse import urlencode, urljoin

from app.core.config import settings
from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.utils.string import StringUtils


class HDAreaSiteUserInfo(SiteParserBase):
    """
    HDArea API v1 用户数据解析器，负责账户资料、做种分布与未读私信同步
    """

    schema = SiteSchema.HDArea
    request_mode = "apikey"
    _MAX_SEEDING_PAGES = 20
    _MAX_MESSAGE_PAGES = 10

    _USER_ROLE_MAP = {
        "0": "Peasant",
        "1": "User",
        "2": "Power User",
        "3": "Elite User",
        "4": "Crazy User",
        "5": "Insane User",
        "6": "Veteran User",
        "7": "Extreme User",
        "8": "Ultimate User",
        "9": "Nexus Master",
        "10": "VIP",
        "11": "Retiree",
        "12": "Uploader",
        "13": "Moderator",
        "14": "Administrator",
        "15": "Sysop",
        "16": "Staff Leader",
    }

    def _parse_site_page(self, html_text: str) -> None:
        """
        配置 HDArea API v1 根地址与请求头

        :param html_text: API Key 模式下的占位首页数据
        """
        api_base = self._api_url or f"{self._base_url.rstrip('/')}/api/v1"
        self._base_url = f"{str(api_base).rstrip('/')}/"
        self._user_basic_page = "user/profile"
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None
        self._addition_headers = {
            "Accept": "application/json",
            "User-Agent": self._ua or settings.USER_AGENT,
            "X-API-Key": self.apikey,
        }

    def _load_api_payload(
        self,
        html_text: str,
        operation: str,
        record_error: bool = True,
    ) -> Tuple[Optional[dict], Optional[int]]:
        """
        解析并校验 HDArea API 统一 JSON 包裹结构

        :param html_text: 接口响应文本
        :param operation: 操作名称
        :param record_error: 失败时是否写入 self.err_msg
        :return: (data 字典或 None, 错误码或 HTTP 状态码)
        """
        if not html_text:
            msg = f"{operation}失败，未收到 API 响应"
            if record_error:
                self.err_msg = msg
            logger.warning(f"{self._site_name} {msg}")
            return None, None
        try:
            payload = json.loads(html_text)
        except (TypeError, json.JSONDecodeError) as err:
            msg = f"{operation}失败，API 响应不是有效 JSON"
            if record_error:
                self.err_msg = msg
            logger.warning(f"{self._site_name} {msg}：{str(err)}")
            return None, None
        if not isinstance(payload, dict):
            msg = f"{operation}失败，API 响应结构无效"
            if record_error:
                self.err_msg = msg
            logger.warning(f"{self._site_name} {msg}")
            return None, None
        code = payload.get("code")
        http_code = payload.get("http_code")
        if code != 0 and str(code) != "0":
            msg = payload.get("message") or f"{operation}失败"
            if record_error:
                self.err_msg = msg
            logger.warning(f"{self._site_name} {operation}失败：{msg}")
            err_code = http_code if isinstance(http_code, int) else (
                code if isinstance(code, int) else None
            )
            return None, err_code
        data = payload.get("data")
        return (data if isinstance(data, dict) else None), 0

    def _parse_user_base_info(self, html_text: str) -> None:
        """
        从 /api/v1/user/profile 解析用户基础资料、流量、魔力值与做种汇总

        :param html_text: profile 接口 JSON 响应文本
        """
        profile, _ = self._load_api_payload(html_text, "获取用户信息", record_error=True)
        if not profile:
            return
        user_id = profile.get("id")
        self.userid = str(user_id) if user_id is not None else None
        self.username = profile.get("username")
        role_id = str(profile.get("class") or "")
        self.user_level = (
            profile.get("class_name")
            or self._USER_ROLE_MAP.get(role_id)
            or role_id
        )
        self.join_at = StringUtils.unify_datetime_str(profile.get("added"))
        self.upload = int(profile.get("uploaded") or 0)
        self.download = int(profile.get("downloaded") or 0)
        raw_ratio = profile.get("share_ratio")
        if raw_ratio is not None:
            try:
                self.ratio = round(float(raw_ratio), 3)
            except (TypeError, ValueError):
                self.ratio = round(self.upload / self.download, 2) if self.download else 0
        else:
            self.ratio = round(self.upload / self.download, 2) if self.download else 0
        self.bonus = float(profile.get("seedbonus") or 0.0)
        self.seeding = int(profile.get("seeding_count") or 0)
        self.seeding_size = int(profile.get("seeding_size") or 0)
        self.leeching = int(profile.get("leeching_count") or 0)
        self.uploaded = int(profile.get("uploaded_torrents_count") or 0)
        self.completed = int(profile.get("completed_torrents_count") or 0)
        self.message_unread = int(profile.get("unread_messages_count") or 0)

    def _parse_seeding_pages(self) -> None:
        """
        当用户存在做种条目时，按页拉取 /api/v1/user/torrents?type=seeding 填充做种分布明细
        """
        if self.err_msg or self.seeding <= 0:
            return
        page = 0
        total_size_acc = 0
        while page < self._MAX_SEEDING_PAGES:
            query = urlencode({
                "type": "seeding",
                "page": page,
                "per_page": 100,
            })
            html_text = self._get_page_content(
                url=urljoin(self._base_url, f"user/torrents?{query}")
            )
            data, _ = self._load_api_payload(
                html_text,
                "获取做种列表",
                record_error=False,
            )
            if not data:
                break
            items = data.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                size = int(item.get("size") or 0)
                seeders = int(item.get("seeders") or 1)
                total_size_acc += size
                self.seeding_info.append([seeders, size])
            pagination = data.get("pagination") or {}
            if not pagination.get("has_more") or not items:
                break
            page += 1
        if not self.seeding_size and total_size_acc > 0:
            self.seeding_size = total_size_acc

    @staticmethod
    def _format_message_body(message: dict) -> str:
        """
        将站内信 HTML 或 BBCode 正文清洗为适合通知渠道阅读的纯文本

        :param message: 单条消息字典
        :return: 清洗后的正文文本
        """
        body_html = message.get("body_html")
        if body_html:
            normalized = re.sub(r"(?i)<br\s*/?>", "\n", str(body_html))
            normalized = re.sub(r"(?i)</(p|div|li|tr|blockquote)>", "\n", normalized)
            plain = html.unescape(StringUtils.strip_html(normalized))
            lines = [line.rstrip() for line in plain.splitlines()]
            return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

        raw_body = str(message.get("body") or "")
        cleaned = raw_body.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"\[\*\]\s*", "• ", cleaned)
        cleaned = re.sub(r"\[/?(?:b|i|u|s|quote|code|center|hr)\]", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\[(?:color|size|font)=[^\]]+\]", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\[/(?:color|size|font)\]", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"\2 (\1)", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"\[/?url\]", "", cleaned, flags=re.I)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    def _fallback_nexus_unread_msgs(self) -> None:
        """
        子令牌缺少 user 权限但配置了站点 Cookie 时，回退通过网页接口读取站内信正文
        """
        if not self._site_cookie:
            return
        nexus = NexusPhpSiteUserInfo(
            site_name=self._site_name,
            url=self._site_url,
            site_cookie=self._site_cookie,
            apikey=self.apikey,
            token=self.token,
            session=self._session,
            ua=self._ua,
            emulate=self._emulate,
            proxy=self._proxy,
        )
        nexus.message_unread = self.message_unread
        nexus.message_read_force = self.message_unread > 0
        nexus._pase_unread_msgs()
        self.message_unread_contents = nexus.message_unread_contents.copy()

    def _pase_unread_msgs(self) -> None:
        """
        分页读取 HDArea 未读私信正文；子令牌无 user 权限时不阻塞基础用户数据入库
        """
        if self.err_msg or (self.message_unread <= 0 and not self.message_read_force):
            return
        page = 0
        while page < self._MAX_MESSAGE_PAGES:
            query = urlencode({
                "unread": 1,
                "page": page,
                "per_page": 100,
            })
            html_text = self._get_page_content(
                url=urljoin(self._base_url, f"user/messages?{query}")
            )
            data, err_code = self._load_api_payload(
                html_text,
                "获取未读消息",
                record_error=False,
            )
            if not data:
                if err_code == 403 and page == 0:
                    self._fallback_nexus_unread_msgs()
                return
            has_more = self._parse_message_unread_links(html_text, [])
            if not has_more:
                return
            page += 1

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        解析未读消息列表 JSON 并写入带去重标识与直达链接的消息元组

        :param html_text: user/messages 接口 JSON 文本
        :param msg_links: 兼容基类签名的链接列表
        :return: 存在下一页时返回非空标记，否则返回 None
        """
        data, _ = self._load_api_payload(html_text, "获取未读消息", record_error=False)
        if not data:
            return None
        items = data.get("items") or []
        site_root = f"{self._site_url.rstrip('/')}/"
        for item in items:
            if not isinstance(item, dict) or not item.get("unread", True):
                continue
            subject = (item.get("subject") or "").strip()
            added = StringUtils.unify_datetime_str(item.get("added"))
            content = self._format_message_body(item)
            msg_id = item.get("id")
            if not (subject and added and content):
                continue
            msg_source = f"hdarea-message:{msg_id}" if msg_id is not None else None
            msg_link = (
                f"{site_root}messages.php?action=viewmessage&id={msg_id}"
                if msg_id is not None
                else site_root
            )
            self.message_unread_contents.append(
                (subject, added, content, msg_source, msg_link)
            )
        pagination = data.get("pagination") or {}
        total = int(pagination.get("total") or 0)
        cur_page = int(pagination.get("page") or 0)
        per_page = int(pagination.get("per_page") or 100)
        if pagination.get("has_more") or (items and (cur_page + 1) * per_page < total):
            return "next"
        return None

    def _parse_logged_in(self, html_text: str) -> bool:
        """
        API Key 模式跳过首页 HTML 登录态检查

        :param html_text: 占位文本
        :return: 始终返回 True
        """
        return True

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        跳过独立流量页解析，profile 接口已包含完整流量统计

        :param html_text: 未使用的响应文本
        """

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        跳过独立详情页解析，profile 接口已包含完整用户资料

        :param html_text: 未使用的响应文本
        """

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: Optional[bool] = False,
    ) -> Optional[str]:
        """
        做种列表已在 _parse_seeding_pages 中通过 JSON 分页拉取，此处无需重复处理

        :param html_text: 未使用的响应文本
        :param multi_page: 是否为翻页请求
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(
        self,
        html_text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        站内信接口已直接返回消息正文，无需逐条二次抓取详情

        :param html_text: 未使用的响应文本
        :return: 空三元组
        """
        return None, None, None
