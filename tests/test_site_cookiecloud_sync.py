"""CookieCloud 同步已存在停用站点的离线回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import app.chain.site as site_module
from app.chain.site import SiteChain


def test_sync_cookies_updates_inactive_site_with_saved_user_agent(monkeypatch):
    """停用站点沿用已保存 UA，验证成功后更新原记录而非重复新增。"""
    domain = "example.test"
    site_info = SimpleNamespace(id=7, name="示例站点", is_active=False, ua="Saved-UA")
    site_oper = Mock()
    site_oper.get_by_domain.return_value = site_info
    rss_helper = Mock()
    rss_helper.get_rss_link.return_value = ("https://example.test/rss.xml", "")
    request = Mock()
    request.get_res.return_value = SimpleNamespace(status_code=200, text="logged in")
    request_kwargs = []

    def request_factory(**kwargs):
        """记录请求构造参数并返回离线响应替身。"""
        request_kwargs.append(kwargs)
        return request

    monkeypatch.setattr(
        site_module.CookieCloudHelper,
        "download",
        lambda self: ({domain: "new-cookie"}, ""),
    )
    monkeypatch.setattr(
        site_module.SitesHelper,
        "get_indexer",
        lambda self, current_domain: {
            "name": "示例站点",
            "domain": "https://example.test/",
            "public": False,
        },
    )
    monkeypatch.setattr(site_module, "SiteOper", lambda: site_oper)
    monkeypatch.setattr(site_module, "RssHelper", lambda: rss_helper)
    monkeypatch.setattr(site_module, "RequestUtils", request_factory)
    monkeypatch.setattr(site_module.SiteUtils, "is_logged_in", lambda content: True)
    monkeypatch.setattr(site_module.eventmanager, "send_event", Mock())
    monkeypatch.setattr(site_module.settings, "COOKIECLOUD_BLACKLIST", None)
    monkeypatch.setattr(site_module.settings, "PROXY_HOST", None)

    success, message = SiteChain.__new__(SiteChain).sync_cookies()

    assert success is True
    assert message == "更新了1个站点，新增了0个站点"
    assert request_kwargs == [{"cookies": "new-cookie", "ua": "Saved-UA"}]
    rss_helper.get_rss_link.assert_called_once_with(
        url="https://example.test/",
        cookie="new-cookie",
        ua="Saved-UA",
        proxy=False,
    )
    site_oper.update.assert_called_once_with(7, {
        "cookie": "new-cookie",
        "rss": "https://example.test/rss.xml",
        "proxy": 0,
    })
    site_oper.add.assert_not_called()
