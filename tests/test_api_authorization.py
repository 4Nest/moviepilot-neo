import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints import dashboard as dashboard_endpoint
from app.api.endpoints import history as history_endpoint
from app.api.endpoints import login as login_endpoint
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints import site as site_endpoint
from app.api.endpoints import storage as storage_endpoint
from app.api.endpoints import system as system_endpoint
from app.api.endpoints import transfer as transfer_endpoint
from app.api.endpoints import user as user_endpoint
from app.core.security import verify_resource_token
from app.db.user_oper import (
    get_current_admin,
    get_current_admin_async,
)
from app.schemas.types import SystemConfigKey


def _dependency_of(func, parameter_name: str):
    """读取 FastAPI 函数参数上声明的依赖函数。"""
    return inspect.signature(func).parameters[parameter_name].default.dependency


def _build_request() -> Request:
    """构造最小测试请求。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/login/access-token",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def test_system_sensitive_read_endpoints_require_admin():
    """系统敏感读取接口必须只允许 canonical admin 访问。"""
    assert _dependency_of(system_endpoint.get_env_setting, "_") is get_current_admin_async
    assert _dependency_of(system_endpoint.get_setting, "_") is get_current_admin_async


def test_system_public_read_endpoints_require_admin():
    """公开读取接口同样收敛到 canonical admin 依赖。"""
    assert _dependency_of(system_endpoint.ping, "_") is get_current_admin_async
    assert _dependency_of(system_endpoint.get_public_setting, "_") is get_current_admin_async


def test_dashboard_endpoints_require_admin():
    """仪表板页面相关接口必须只允许 canonical admin 访问。"""
    assert _dependency_of(dashboard_endpoint.statistic, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.storage, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.processes, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.system_info, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.downloader, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.schedule, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.transfer, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.cpu, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.memory, "_") is get_current_admin
    assert _dependency_of(dashboard_endpoint.network, "_") is get_current_admin


def test_plugin_dashboard_endpoints_require_admin():
    """插件仪表板接口必须只允许 canonical admin 访问。"""
    assert _dependency_of(plugin_endpoint.plugin_dashboard_meta, "_") is get_current_admin
    assert _dependency_of(plugin_endpoint.plugin_dashboard_by_key, "_") is get_current_admin
    assert _dependency_of(plugin_endpoint.plugin_dashboard, "_") is get_current_admin


def test_manage_page_endpoints_require_admin():
    """原管理页面接口统一收敛到 canonical admin 依赖。"""
    sync_endpoints = [
        storage_endpoint.list_files,
        storage_endpoint.mkdir,
        storage_endpoint.delete,
        storage_endpoint.download,
        storage_endpoint.image,
        storage_endpoint.rename,
        site_endpoint.update_cookie_by_body,
        site_endpoint.update_cookie,
        site_endpoint.refresh_userdata,
        history_endpoint.delete_transfer_history,
        transfer_endpoint.match_manual_transfer_target_path,
        transfer_endpoint.manual_transfer,
        transfer_endpoint.recommend_episode_format,
    ]
    async_endpoints = [
        site_endpoint.read_sites,
        site_endpoint.add_site,
        site_endpoint.update_site,
        site_endpoint.update_sites_priority,
        site_endpoint.read_userdata_latest,
        site_endpoint.read_userdata,
        site_endpoint.site_resource,
        site_endpoint.read_site,
        site_endpoint.delete_site,
    ]

    for endpoint in sync_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_admin
    for endpoint in async_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_admin_async


def test_system_public_setting_allows_only_non_sensitive_keys(monkeypatch):
    """公开系统设置接口只能读取明确列入白名单的非敏感配置。"""
    calls = []

    class FakeSystemConfigOper:
        """返回测试配置值的系统配置桩。"""

        def get(self, key):
            """返回测试配置值。"""
            calls.append(key)
            return [{"path": "/downloads"}]

    monkeypatch.setattr(system_endpoint, "SystemConfigOper", FakeSystemConfigOper)

    response = asyncio.run(
        system_endpoint.get_public_setting(SystemConfigKey.Directories.value)
    )

    assert response.success is True
    assert response.data == {"value": [{"path": "/downloads"}]}
    assert calls == [SystemConfigKey.Directories]

    response = asyncio.run(system_endpoint.get_public_setting("PLUGIN_MARKET"))

    assert response.success is True
    assert response.data == {"value": system_endpoint.settings.PLUGIN_MARKET}
    assert calls == [SystemConfigKey.Directories]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(system_endpoint.get_public_setting("API_TOKEN"))

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "配置项不存在"


def test_system_ping_returns_success():
    """服务存活检测接口返回标准成功响应。"""
    response = asyncio.run(system_endpoint.ping())

    assert response.success is True


def test_login_sets_resource_token_cookie(monkeypatch):
    """登录成功时应立即写入资源 Cookie，避免插件静态文件抢先加载失败。"""

    class FakeUserChain:
        """返回登录成功用户的用户链桩。"""

        def user_authenticate(self, username, password, mfa_code=None):
            """返回认证成功结果。"""
            return True, SimpleNamespace(
                id=1,
                name=username,
                is_superuser=True,
                avatar="",
            )

    class FakeSystemConfigOper:
        """返回已完成向导状态的系统配置桩。"""

        def get(self, key):
            """返回测试配置值。"""
            return "1"

    form_data = SimpleNamespace(username="user", password="password")
    request = _build_request()
    response = Response()

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)
    monkeypatch.setattr(login_endpoint, "SystemConfigOper", FakeSystemConfigOper)

    token = login_endpoint.login_access_token(
        request=request,
        response=response,
        form_data=form_data,
    )

    assert token.user_id == 1
    # 登录响应不再暴露权限/管理员标记
    assert not hasattr(token, "permissions")
    assert not hasattr(token, "super_user")
    assert "set-cookie" in response.headers

    resource_cookie = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    payload = verify_resource_token(resource_cookie)
    assert payload.sub == 1
    assert payload.username == "user"
    assert payload.purpose == "resource"


def test_plugin_static_file_requires_resource_token_by_default(monkeypatch):
    """插件静态资源必须校验资源令牌。"""
    calls = []

    monkeypatch.setattr(plugin_endpoint, "verify_resource_token", lambda token: calls.append(token))

    plugin_endpoint._verify_plugin_static_file_access(
        plugin_id="DemoPlugin",
        filepath="dist/remoteEntry.js",
        resource_token="resource-token",
    )

    assert calls == ["resource-token"]


def test_upload_avatar_updates_current_user(monkeypatch):
    """头像上传直接更新 token 解析出的当前用户，不再接受 user_id 参数。"""

    class FakeUser:
        """记录头像更新内容的用户桩。"""

        def __init__(self):
            self.values = None

        async def async_update(self, db: object, values: dict[str, str]) -> None:
            """记录待写入的头像数据。"""
            self.values = values

    import io

    current_user = FakeUser()
    upload_file = SimpleNamespace(file=io.BytesIO(b"avatar"), filename="avatar.png")

    response = asyncio.run(
        user_endpoint.upload_avatar(
            db=object(),
            file=upload_file,
            current_user=current_user,
        )
    )

    assert response.success is True
    assert response.data == {"filename": "avatar.png"}
    assert response.message is None
    assert response.message_i18n is None
    assert current_user.values == {"avatar": "data:image/ico;base64,b'YXZhdGFy'"}


def test_update_current_user_rejects_privileged_fields():
    """资料更新请求夹带 name/is_superuser/permissions/id 等字段一律被 schema 拒绝。"""
    from pydantic import ValidationError

    from app import schemas

    for field in ("name", "is_active", "is_superuser", "permissions", "id"):
        with pytest.raises(ValidationError):
            schemas.UserProfileUpdate(**{field: "x" if field != "id" else 1})


def test_update_current_user_merges_nickname_into_settings(monkeypatch):
    """昵称写入个性化设置并与既有 settings 合并，密码只落哈希。"""

    class FakeUser:
        """记录资料更新内容的用户桩。"""

        def __init__(self):
            self.values = None
            self.settings = {"theme": "dark"}

        async def async_update(self, db: object, values: dict) -> None:
            """记录待写入的资料数据。"""
            self.values = values

    current_user = FakeUser()
    user_in = user_endpoint.schemas.UserProfileUpdate(
        nickname="新昵称", password="abc123!", email="a@b.c"
    )

    response = asyncio.run(
        user_endpoint.update_current_user(
            db=object(),
            user_in=user_in,
            current_user=current_user,
        )
    )

    assert response.success is True
    assert current_user.values["settings"] == {"theme": "dark", "nickname": "新昵称"}
    assert current_user.values["email"] == "a@b.c"
    assert "password" not in current_user.values
    assert current_user.values["hashed_password"] != "abc123!"
    assert "name" not in current_user.values


def test_user_management_routes_removed():
    """多用户管理路由已删除，只保留当前用户自助接口。"""
    from fastapi.routing import APIRoute

    paths = {
        (route.path, method)
        for route in user_endpoint.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/", "GET") not in paths
    assert ("/", "POST") not in paths
    assert ("/", "PUT") not in paths
    assert ("/id/{user_id}", "DELETE") not in paths
    assert ("/name/{user_name}", "DELETE") not in paths
    assert ("/{username}", "GET") not in paths
    assert ("/avatar/{user_id}", "POST") not in paths
    assert ("/current", "GET") in paths
    assert ("/current", "PUT") in paths
    assert ("/current/avatar", "POST") in paths
    assert ("/config/{key}", "GET") in paths
    assert ("/config/{key}", "POST") in paths
