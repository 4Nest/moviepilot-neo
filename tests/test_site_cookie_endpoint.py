import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import schemas
from app.api.endpoints import site as site_endpoint


def test_update_cookie_by_body_uses_request_body():
    """
    POST 更新站点 Cookie 时应从请求体读取登录参数。
    """
    fake_site = SimpleNamespace(id=1, name="TestSite")
    fake_chain = Mock()
    fake_chain.update_cookie.return_value = (True, "ok")
    request = schemas.SiteCookieUpdate(username="user", password="password", code="123456")

    with patch.object(site_endpoint.Site, "get", return_value=fake_site), patch.object(
        site_endpoint, "SiteChain", return_value=fake_chain
    ):
        response = site_endpoint.update_cookie_by_body(
            site_id=1,
            site_cookie_update=request,
            db=Mock(),
            _=Mock(),
        )

    assert response.success is True
    assert response.message == "ok"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code="123456",
    )


def test_update_cookie_legacy_get_keeps_query_params():
    """
    旧 GET 入口仍应兼容查询参数更新站点 Cookie。
    """
    fake_site = SimpleNamespace(id=1, name="TestSite")
    fake_chain = Mock()
    fake_chain.update_cookie.return_value = (False, "failed")

    with patch.object(site_endpoint.Site, "get", return_value=fake_site), patch.object(
        site_endpoint, "SiteChain", return_value=fake_chain
    ):
        response = site_endpoint.update_cookie(
            site_id=1,
            username="user",
            password="password",
            code=None,
            db=Mock(),
            _=Mock(),
        )

    assert response.success is False
    assert response.message == "failed"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code=None,
    )


def test_add_site_to_cookiecloud_blacklist_appends_normalized_domain():
    fake_site = SimpleNamespace(id=1, url="https://tracker.example.com/path")

    with (
        patch.object(
            site_endpoint.Site,
            "async_get",
            new=AsyncMock(return_value=fake_site),
        ),
        patch.object(
            site_endpoint.settings,
            "COOKIECLOUD_BLACKLIST",
            "other.example.org",
        ),
        patch.object(
            type(site_endpoint.settings),
            "update_setting",
            return_value=(True, ""),
        ) as update_setting,
        patch.object(
            site_endpoint.eventmanager,
            "async_send_event",
            new=AsyncMock(),
        ) as send_event,
    ):
        response = asyncio.run(
            site_endpoint.add_site_to_cookiecloud_blacklist(
                site_id=1,
                db=Mock(),
                _=Mock(),
            )
        )

    assert response.success is True
    assert response.data == {
        "added": True,
        "domain": "example.com",
        "value": "other.example.org,example.com",
    }
    update_setting.assert_called_once_with(
        "COOKIECLOUD_BLACKLIST", "other.example.org,example.com"
    )
    send_event.assert_awaited_once()


def test_add_site_to_cookiecloud_blacklist_is_idempotent():
    fake_site = SimpleNamespace(id=1, url="https://tracker.example.com/")

    with (
        patch.object(
            site_endpoint.Site,
            "async_get",
            new=AsyncMock(return_value=fake_site),
        ),
        patch.object(
            site_endpoint.settings,
            "COOKIECLOUD_BLACKLIST",
            "other.example.org,EXAMPLE.COM",
        ),
        patch.object(type(site_endpoint.settings), "update_setting") as update_setting,
        patch.object(
            site_endpoint.eventmanager,
            "async_send_event",
            new=AsyncMock(),
        ) as send_event,
    ):
        response = asyncio.run(
            site_endpoint.add_site_to_cookiecloud_blacklist(
                site_id=1,
                db=Mock(),
                _=Mock(),
            )
        )

    assert response.success is True
    assert response.data == {
        "added": False,
        "domain": "example.com",
        "value": "other.example.org,EXAMPLE.COM",
    }
    update_setting.assert_not_called()
    send_event.assert_not_awaited()
