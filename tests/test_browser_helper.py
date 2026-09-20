from __future__ import annotations

from typing import Optional
from unittest.mock import patch


from app.helper.browser import PlaywrightHelper


class _FakeResponse:
    """模拟浏览器导航响应。"""

    status = 200


class _FakeElement:
    """模拟页面元素。"""

    def is_visible(self) -> bool:
        """返回元素可见状态。"""
        return True

    def fill(self, value: str) -> None:
        """记录输入值。"""
        self.value = value

    def inner_text(self) -> str:
        """返回元素文本。"""
        return "元素文本"


class _FakePage:
    """模拟 CloakBrowser 页面对象。"""

    def __init__(self, page_id: str = "page-1") -> None:
        self.page_id = page_id
        self.headers = None
        self.loaded_url = ""
        self.url = "about:blank"
        self.closed = False
        self.timeout = None
        self.clicks = []
        self.fills = []
        self.selects = []

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        """记录额外请求头。"""
        self.headers = headers

    def set_default_timeout(self, timeout: int) -> None:
        """记录默认超时时间。"""
        self.timeout = timeout

    def goto(self, url: str, *args, **kwargs) -> _FakeResponse:
        """记录导航目标。"""
        self.loaded_url = url
        self.url = url
        return _FakeResponse()

    def wait_for_load_state(self, _state: str, timeout: int) -> None:
        """记录页面等待超时。"""
        self.timeout = timeout

    def wait_for_selector(self, selector: str, *args, **kwargs) -> _FakeElement:
        """返回模拟元素。"""
        self.waited_selector = selector
        return _FakeElement()

    def fill(self, selector: str, value: str, *args, **kwargs) -> None:
        """记录表单输入。"""
        self.fills.append((selector, value))

    def click(self, selector: str, *args, **kwargs) -> None:
        """记录点击选择器。"""
        self.clicks.append(selector)

    def select_option(self, selector: str, *args, **kwargs) -> None:
        """记录下拉选择。"""
        self.selects.append((selector, kwargs.get("value")))

    def query_selector(self, selector: str) -> _FakeElement:
        """返回模拟元素。"""
        self.queried_selector = selector
        return _FakeElement()

    def title(self) -> str:
        """返回页面标题。"""
        return f"标题 {self.page_id}"

    def inner_text(self, selector: str) -> str:
        """返回页面文本。"""
        return f"正文 {self.page_id}"

    def content(self) -> str:
        """返回页面源码。"""
        return "<html>ok</html>"

    def evaluate(self, expression: str, *args, **kwargs):
        """返回脚本执行结果。"""
        return {"ok": True}

    def screenshot(self, *args, **kwargs) -> bytes:
        """返回模拟截图内容。"""
        return b"image"

    def close(self) -> None:
        """记录页面关闭状态。"""
        self.closed = True


class _FakeContext:
    """模拟 CloakBrowser 上下文。"""

    def __init__(self, pages: Optional[list[_FakePage]] = None) -> None:
        self.pages = pages or [_FakePage()]
        self.closed = False

    def new_page(self) -> _FakePage:
        """返回或创建模拟页面。"""
        if self.pages:
            return self.pages.pop(0)
        return _FakePage("extra")

    def cookies(self) -> list[dict]:
        """返回空 Cookie 列表。"""
        return []

    def close(self) -> None:
        """记录上下文关闭状态。"""
        self.closed = True




def test_default_emulation_uses_cloakbrowser_context():
    """默认浏览器仿真应使用 CloakBrowser 上下文。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch("app.helper.browser.settings.BROWSER_EMULATION", "cloakbrowser"), patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ) as launch_context:
        source = PlaywrightHelper().get_page_source(
            url="https://example.com",
            cookies="uid=1",
            ua="UA",
            timeout=3,
        )

    assert source == "<html>ok</html>"
    launch_context.assert_called_once_with(
        headless=False,
        user_agent="UA",
        proxies=None,
    )
    assert page.headers == {"cookie": "uid=1"}
    assert page.loaded_url == "https://example.com"
    assert page.closed
    assert context.closed


def test_legacy_playwright_emulation_uses_cloakbrowser_context():
    """兼容旧 Playwright 仿真配置。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch("app.helper.browser.settings.BROWSER_EMULATION", "Playwright"), patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ):
        source = PlaywrightHelper().get_page_source(url="https://example.com")

    assert source == "<html>ok</html>"
