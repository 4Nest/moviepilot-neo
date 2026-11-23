from typing import Mapping, Optional

from lxml import etree

from app.utils.string import StringUtils


def is_valid_http_header_value(value: str) -> bool:
    """
    校验字符串能否作为合法的 HTTP 请求头值发送。

    拒绝首尾空白、控制字符（保留水平制表符）以及无法按 Latin-1 编码的字符，
    避免底层 HTTP 客户端在组装请求头阶段直接抛出异常。

    :param value: 待校验的请求头文本；空字符串视为合法
    :return: 可安全写入 HTTP 请求头时返回 True，否则返回 False
    """
    if not value:
        return True
    if value != value.strip():
        return False
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in value):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def find_invalid_http_header_names(headers: Optional[Mapping[str, object]]) -> list[str]:
    """
    返回请求头映射中取值非法的字段名称，不暴露可能包含凭据的字段值。

    :param headers: 待检查的请求头映射；None 或空映射返回空列表
    :return: 取值不符合 HTTP 请求头规范的字段名称列表
    """
    if not headers:
        return []
    invalid_names: list[str] = []
    for key, value in headers.items():
        if value is None:
            continue
        if not isinstance(value, str) or not is_valid_http_header_value(value):
            invalid_names.append(str(key))
    return invalid_names


class SiteUtils:

    @classmethod
    def is_logged_in(cls, html_text: str) -> bool:
        """
        判断站点是否已经登陆
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        try:
            if not StringUtils.is_valid_html_element(html):
                return False
            # 存在明显的密码输入框，说明未登录
            if html.xpath("//input[@type='password']"):
                return False
            # 是否存在登出和用户面板等链接
            xpaths = [
                '//a[contains(@href, "logout")'
                ' or contains(@data-url, "logout")'
                ' or contains(@href, "mybonus") '
                ' or contains(@onclick, "logout")'
                ' or contains(@href, "usercp")'
                ' or contains(@lay-on, "logout")]',
                '//form[contains(@action, "logout")]',
                '//div[@class="user-info-side"]',
                '//a[@id="myitem"]'
            ]
            for xpath in xpaths:
                if html.xpath(xpath):
                    return True
            return False
        finally:
            if html is not None:
                del html

    @classmethod
    def is_checkin(cls, html_text: str) -> bool:
        """
        判断站点是否已经签到
        :return True已签到 False未签到
        """
        html = etree.HTML(html_text)
        try:
            if not StringUtils.is_valid_html_element(html):
                return False
            # 站点签到支持的识别XPATH
            xpaths = [
                '//a[@id="signed"]',
                '//a[contains(@href, "attendance")]',
                '//a[contains(text(), "签到")]',
                '//a/b[contains(text(), "签 到")]',
                '//span[@id="sign_in"]/a',
                '//a[contains(@href, "addbonus")]',
                '//input[@class="dt_button"][contains(@value, "打卡")]',
                '//a[contains(@href, "sign_in")]',
                '//a[contains(@onclick, "do_signin")]',
                '//a[@id="do-attendance"]',
                '//shark-icon-button[@href="attendance.php"]'
            ]
            for xpath in xpaths:
                if html.xpath(xpath):
                    return False
            return True
        finally:
            if html is not None:
                del html
