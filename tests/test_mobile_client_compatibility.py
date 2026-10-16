"""验证第三方客户端登录及当前账号资料的兼容契约。"""

from types import SimpleNamespace
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import schemas
from app.api.endpoints import login, user
from app.core.config import settings
from app.db.user_oper import get_current_user_async


@pytest.fixture
def client_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, SimpleNamespace]]:
    """只替换认证及数据库边界，保留真实路由、响应序列化和管理员校验。"""
    account = SimpleNamespace(
        id=1, name=settings.SUPERUSER, email=None, avatar="", is_active=True,
        is_superuser=True, is_otp=False, settings={}, hashed_password="secret", otp_secret="secret",
    )
    monkeypatch.setattr(login, "UserChain", lambda: SimpleNamespace(
        user_authenticate=lambda **_: (True, account),
    ))
    monkeypatch.setattr(login, "SitesHelper", lambda: SimpleNamespace(auth_level=1))
    monkeypatch.setattr(login, "SystemConfigOper", lambda: SimpleNamespace(get=lambda _: True))
    app = FastAPI()
    app.include_router(login.router, prefix="/api/v1/login")
    app.include_router(user.router, prefix="/api/v1/user")
    app.dependency_overrides[get_current_user_async] = lambda: account
    with TestClient(app) as client:
        yield client, account


def test_lite_login_and_profile_restore_permissions(client_context: tuple[TestClient, SimpleNamespace]) -> None:
    """Lite 登录、按名称刷新及切换账号所需的权限字段必须一致。"""
    client, account = client_context
    response = client.post("/api/v1/login/access-token", data={"username": account.name, "password": "test"})
    assert response.status_code == 200
    token = response.json()
    assert token["super_user"] is True
    assert token["permissions"] == {key: True for key in ("discovery", "search", "subscribe", "manage")}

    for path in ("/api/v1/user/current", f"/api/v1/user/{account.name}"):
        response = client.get(path)
        assert response.status_code == 200
        profile = response.json()
        assert profile["id"] == token["user_id"]
        assert profile["is_superuser"] is True
        assert profile["is_active"] is True
        assert profile["is_otp"] is False
        assert profile["email"] == ""
        assert profile["permissions"] == token["permissions"]
        assert "hashed_password" not in profile
        assert "otp_secret" not in profile

    assert client.get("/api/v1/user/").json() == [profile]
    assert client.get("/api/v1/user/another-user").status_code == 404


@pytest.mark.parametrize("field,value", [
    ("is_active", False), ("is_superuser", False), ("name", "another-admin"),
])
def test_legacy_reads_still_require_canonical_admin(
    client_context: tuple[TestClient, SimpleNamespace], field: str, value: object,
) -> None:
    """旧版只读接口仍拒绝停用账号、普通账号及非配置管理员。"""
    client, account = client_context
    setattr(account, field, value)
    for path in ("/api/v1/user/current", "/api/v1/user/", f"/api/v1/user/{account.name}"):
        assert client.get(path).status_code == 403


def test_legacy_user_management_writes_remain_unavailable(client_context: tuple[TestClient, SimpleNamespace]) -> None:
    """客户端兼容不能恢复新增、删除或修改其它用户的能力。"""
    client, _ = client_context
    assert client.post("/api/v1/user/", json={"name": "new"}).status_code == 405
    assert client.put("/api/v1/user/", json={"is_superuser": True}).status_code == 405
    assert client.delete("/api/v1/user/id/1").status_code == 404


def test_token_without_admin_identity_does_not_advertise_permissions() -> None:
    """缺失管理员身份的令牌响应不可默认宣告管理员权限。"""
    token = schemas.Token(access_token="test", token_type="bearer", user_id=1, user_name="user")
    assert token.super_user is False
    assert not any(token.model_dump()["permissions"].values())
