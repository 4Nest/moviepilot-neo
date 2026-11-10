import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app import schemas
from app.api.endpoints import login as login_endpoint
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints import subscribe as subscribe_endpoint
from app.chain.user import UserChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.models.subscribe import Subscribe as SubscribeModel
from app.db.models.user import User as UserModel
from app.db.subscribe_oper import SubscribeOper
from app.db.user_oper import UserOper, get_current_active_user, get_current_active_user_async
from app.modules.emby import EmbyModule
from app.modules.emby.emby import Emby
from app.schemas.types import ChainEventType, MediaType


def _build_login_request() -> Request:
    """构造登录接口测试请求。"""
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


def _top_level_block_handler(event):
    """测试用顶层准入拦截事件处理器。"""
    if event.event_data.username == "emby_blocked":
        event.event_data.cancel = True
        event.event_data.source = "EmbyUserManager"
        event.event_data.reason = "黑名单拦截"


def test_emby_get_users_normalizes_user_list(monkeypatch):
    """Emby.get_users 返回归一化的用户字典列表。"""
    emby = Emby.__new__(Emby)
    emby._host = "http://emby.local/"
    emby._apikey = "api-key"

    class FakeResponse:
        """模拟 Emby /Users 响应。"""

        @staticmethod
        def json():
            """返回测试用户列表 JSON。"""
            return [
                {
                    "Id": "u1",
                    "Name": "emby_alice",
                    "Policy": {"IsAdministrator": False, "IsDisabled": False},
                    "LastLoginDate": "2026-04-01T10:00:00Z",
                    "LastActivityDate": "2026-04-02T12:00:00Z",
                },
                {
                    "Id": "u2",
                    "Name": "emby_admin",
                    "Policy": {"IsAdministrator": True, "IsDisabled": True},
                },
            ]

    monkeypatch.setattr(
        "app.modules.emby.emby.RequestUtils.get_res",
        lambda self, url, params=None: FakeResponse(),
    )

    users = emby.get_users()
    assert users == [
        {
            "id": "u1",
            "name": "emby_alice",
            "is_admin": False,
            "is_disabled": False,
            "last_login_date": "2026-04-01T10:00:00Z",
            "last_activity_date": "2026-04-02T12:00:00Z",
        },
        {
            "id": "u2",
            "name": "emby_admin",
            "is_admin": True,
            "is_disabled": True,
            "last_login_date": None,
            "last_activity_date": None,
        },
    ]


def test_emby_module_user_authenticate_and_intercept(monkeypatch):
    """EmbyModule.user_authenticate 支持正常认证与 AuthIntercept 事件拦截。"""
    module = EmbyModule()
    fake_server = MagicMock()
    fake_server.authenticate.return_value = "emby-access-token-1"
    monkeypatch.setattr(module, "get_instances", lambda: {"EmbyMain": fake_server})

    creds = schemas.AuthCredentials(
        username="emby_alice",
        password="secret",
        grant_type="password",
    )
    result = module.user_authenticate(creds)
    assert result is not None
    assert result.channel == "Emby"
    assert result.service == "EmbyMain"
    eventmanager.add_event_listener(ChainEventType.AuthIntercept, _top_level_block_handler)
    try:
        blocked_creds = schemas.AuthCredentials(
            username="emby_blocked",
            password="secret",
            grant_type="password",
        )
        blocked_result = module.user_authenticate(blocked_creds)
        assert blocked_result is None
    finally:
        eventmanager.remove_event_listener(ChainEventType.AuthIntercept, _top_level_block_handler)


def test_emby_user_login_auto_creates_non_admin_user_and_blocks_disabled(monkeypatch):
    """Emby 用户首次登录自动创建非管理员账号，禁用或拦截账号登录返回 401。"""
    UserModel().delete_by_name(None, "emby_alice")
    UserModel().delete_by_name(None, "emby_blocked")

    def _fake_run_module(self, method: str, **kwargs):
        if method != "user_authenticate":
            return None
        credentials: schemas.AuthCredentials = kwargs["credentials"]
        if credentials.username in {"emby_alice", "emby_blocked"} and credentials.password == "emby_pass":
            credentials.channel = "Emby"
            credentials.service = "EmbyMain"
            credentials.token = f"token-{credentials.username}"
            return credentials
        return None

    monkeypatch.setattr(UserChain, "run_module", _fake_run_module)

    token = login_endpoint.login_access_token(
        request=_build_login_request(),
        response=Response(),
        form_data=SimpleNamespace(username="emby_alice", password="emby_pass"),
    )
    assert token.user_name == "emby_alice"
    assert token.super_user is False
    assert token.permissions == {
        "discovery": True,
        "search": False,
        "subscribe": True,
        "manage": False,
    }

    created_user = UserOper().get_by_name("emby_alice")
    assert created_user is not None
    assert created_user.is_active is True
    assert created_user.is_superuser is False

    # 禁用该用户后再次登录应返回 401
    created_user.update(None, {"is_active": False})
    with pytest.raises(HTTPException) as exc_info:
        login_endpoint.login_access_token(
            request=_build_login_request(),
            response=Response(),
            form_data=SimpleNamespace(username="emby_alice", password="emby_pass"),
        )
    assert exc_info.value.status_code == 401

    # AuthIntercept completed 阶段拦截应返回 401
    eventmanager.add_event_listener(ChainEventType.AuthIntercept, _top_level_block_handler)
    try:
        with pytest.raises(HTTPException) as exc_info:
            login_endpoint.login_access_token(
                request=_build_login_request(),
                response=Response(),
                form_data=SimpleNamespace(username="emby_blocked", password="emby_pass"),
            )
        assert exc_info.value.status_code == 401
    finally:
        eventmanager.remove_event_listener(ChainEventType.AuthIntercept, _top_level_block_handler)

def test_admin_username_never_falls_back_to_emby_authenticate(monkeypatch):
    """管理员用户名密码错误时直接拒绝，绝不回退调用 Emby 辅助认证。"""
    emby_called = False

    def _spy_run_module(self, method: str, **kwargs):
        nonlocal emby_called
        if method == "user_authenticate":
            emby_called = True
        return None

    monkeypatch.setattr(UserChain, "run_module", _spy_run_module)
    monkeypatch.setattr(
        UserChain,
        "password_authenticate",
        staticmethod(lambda credentials: (False, "用户名、密码或验证码错误")),
    )

    with pytest.raises(HTTPException) as exc_info:
        login_endpoint.login_access_token(
            request=_build_login_request(),
            response=Response(),
            form_data=SimpleNamespace(username=settings.SUPERUSER, password="wrong-password"),
        )
    assert exc_info.value.status_code == 401
    assert emby_called is False


def test_active_user_guard_rejects_inactive_user():
    """激活用户依赖守卫在用户被禁用时抛出 403。"""
    inactive_user = SimpleNamespace(name="emby_alice", is_active=False, is_superuser=False)
    with pytest.raises(HTTPException) as exc_info:
        get_current_active_user(current_user=inactive_user)
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as async_exc_info:
        asyncio.run(get_current_active_user_async(current_user=inactive_user))
    assert async_exc_info.value.status_code == 403


def test_subscribe_isolation_for_emby_users_and_global_visibility_for_admin(monkeypatch):
    """普通用户仅能查看/管理自己的订阅且创建时剔除特权字段；管理员可查看/管理全部用户的订阅。"""
    alice = SimpleNamespace(name="emby_alice", is_active=True, is_superuser=False)
    bob = SimpleNamespace(name="emby_bob", is_active=True, is_superuser=False)
    admin = SimpleNamespace(name="admin", is_active=True, is_superuser=True)

    captured_kwargs = {}

    async def _fake_async_add(self, **kwargs):
        captured_kwargs.update(kwargs)
        return 101, "新增订阅成功"

    monkeypatch.setattr(
        "app.api.endpoints.subscribe.SubscribeChain.async_add",
        _fake_async_add,
    )

    sub_in = schemas.Subscribe(
        name="星际穿越",
        year="2014",
        type=MediaType.MOVIE.value,
        tmdbid=157336,
        save_path="/custom/admin/path",
        downloader="qb-special",
        sites=[1, 2],
        filter_groups=["group-a"],
        quality="Remux",
        skip_library_check=1,
    )
    resp = asyncio.run(
        subscribe_endpoint.create_subscribe(subscribe_in=sub_in, current_user=alice)
    )
    assert resp.success is True
    assert captured_kwargs["username"] == "emby_alice"
    assert captured_kwargs["owner_scope"] is True
    for privileged_key in (
        "save_path",
        "downloader",
        "sites",
        "filter_groups",
        "quality",
        "skip_library_check",
    ):
        assert privileged_key not in captured_kwargs

    alice_sub = SimpleNamespace(id=1, username="emby_alice", name="星际穿越", state="R")
    bob_sub = SimpleNamespace(id=2, username="emby_bob", name="盗梦空间", state="R")

    monkeypatch.setattr(
        SubscribeModel,
        "async_list",
        AsyncMock(return_value=[alice_sub, bob_sub]),
    )
    monkeypatch.setattr(
        SubscribeModel,
        "async_list_by_username",
        AsyncMock(
            side_effect=lambda _db, uname: [
                s for s in (alice_sub, bob_sub) if s.username == uname
            ]
        ),
    )

    alice_list = asyncio.run(
        subscribe_endpoint.read_subscribes(db=object(), current_user=alice)
    )
    assert [s.id for s in alice_list] == [1]

    admin_list = asyncio.run(
        subscribe_endpoint.read_subscribes(db=object(), current_user=admin)
    )
    assert [s.id for s in admin_list] == [1, 2]

    # Alice 不能查看或删除 Bob 的订阅
    monkeypatch.setattr(
        SubscribeModel,
        "async_get",
        AsyncMock(side_effect=lambda _db, sid: {1: alice_sub, 2: bob_sub}.get(sid)),
    )
    denied_detail = asyncio.run(
        subscribe_endpoint.read_subscribe(subscribe_id=2, db=object(), current_user=alice)
    )
    assert getattr(denied_detail, "id", None) is None

    status_resp = asyncio.run(
        subscribe_endpoint.update_subscribe_status(
            subid=2, state="S", db=object(), current_user=alice
        )
    )
    assert status_resp.success is False


def test_plugin_sidebar_nav_hides_admin_items_for_non_superuser(monkeypatch):
    """非管理员调用 /plugin/sidebar_nav 时自动过滤管理类与系统区插件导航项。"""
    nav_items = [
        schemas.PluginSidebarNavItem(
            plugin_id="EmbyUserManager",
            nav_key="main",
            title="用户管理",
            icon="mdi-account-group-outline",
            section="system",
            permission="admin",
            order=10,
        ),
        schemas.PluginSidebarNavItem(
            plugin_id="PublicDiscoverPlugin",
            nav_key="discover",
            title="公共发现",
            icon="mdi-compass-outline",
            section="discover",
            permission=None,
            order=20,
        ),
    ]
    monkeypatch.setattr(
        "app.api.endpoints.plugin.PluginManager.get_plugin_sidebar_nav",
        lambda self: nav_items,
    )

    user_items = plugin_endpoint.plugin_sidebar_nav(
        token_data=schemas.TokenPayload(sub=2, username="emby_alice", super_user=False)
    )
    assert [item.plugin_id for item in user_items] == ["PublicDiscoverPlugin"]

    admin_items = plugin_endpoint.plugin_sidebar_nav(
        token_data=schemas.TokenPayload(sub=1, username="admin", super_user=True)
    )
    assert [item.plugin_id for item in admin_items] == [
        "EmbyUserManager",
        "PublicDiscoverPlugin",
    ]


def test_emby_user_manager_plugin_overview_status_blacklist_and_delete(monkeypatch):
    """EmbyUserManager 插件强制管理员鉴权，支持用户概览、黑白名单准入拦截、禁用与连带清理订阅。"""
    import inspect
    from app.core.event import Event
    from app.db.user_oper import get_current_admin
    EmbyUserManager = pytest.importorskip(
        "app.plugins.embyusermanager",
        reason="EmbyUserManager 属于独立插件仓库，未安装时跳过插件集成测试",
    ).EmbyUserManager

    plugin = EmbyUserManager()
    plugin.init_plugin({"enabled": True, "allow_mode": "blacklist", "blacklist": ["emby_bad"]})

    # 所有插件管理 API 必须绑定 get_current_admin 守卫
    for api_def in plugin.get_api():
        endpoint_fn = api_def["endpoint"]
        dep = inspect.signature(endpoint_fn).parameters["_"].default.dependency
        assert dep is get_current_admin
        with pytest.raises(HTTPException) as exc_info:
            get_current_admin(
                current_user=SimpleNamespace(
                    name="emby_alice", is_active=True, is_superuser=False
                )
            )
        assert exc_info.value.status_code == 403

    # 验证黑名单拦截事件
    bad_event = Event(
        ChainEventType.AuthIntercept,
        schemas.AuthInterceptCredentials(
            username="emby_bad", channel="Emby", service="EmbyMain"
        ),
    )
    plugin.handle_auth_intercept(bad_event)
    assert bad_event.event_data.cancel is True

    # 模拟 Emby 实例与订阅记录，测试 overview、禁用、删除与连带清理订阅
    fake_emby = SimpleNamespace(
        get_users=lambda: [
            {
                "id": "e1",
                "name": "emby_charlie",
                "is_admin": False,
                "is_disabled": False,
                "last_login_date": "2026-04-01T00:00:00Z",
                "last_activity_date": "2026-04-02T00:00:00Z",
            }
        ]
    )
    monkeypatch.setattr(
        "app.plugins.embyusermanager.MediaServerHelper.get_services",
        lambda self, type_filter=None: {"EmbyMain": SimpleNamespace(instance=fake_emby)},
    )

    UserModel().delete_by_name(None, "emby_charlie")
    for sub in SubscribeOper().list_by_username("emby_charlie"):
        SubscribeModel.delete(None, rid=sub.id)

    sub_record = SubscribeModel(
        name="沙丘2",
        year="2024",
        type=MediaType.MOVIE.value,
        tmdbid=693134,
        username="emby_charlie",
        state="R",
    )
    sub_record.create(None)

    overview_resp = plugin.api_overview()
    assert overview_resp.success is True
    charlie_info = next(
        u for u in overview_resp.data["users"] if u["username"] == "emby_charlie"
    )
    assert charlie_info["movie_sub_count"] == 1
    assert charlie_info["access_status"] == "allowed"

    # 禁用尚未登录过 MP 的 Emby 用户，应预创建禁用记录以阻断登录
    disable_resp = plugin.api_update_user_status(
        {"username": "emby_charlie", "is_active": False}
    )
    assert disable_resp.success is True
    disabled_user = UserOper().get_by_name("emby_charlie")
    assert disabled_user is not None
    assert disabled_user.is_active is False

    # 删除该用户并连带清理其订阅
    delete_resp = plugin.api_delete_user(
        {"username": "emby_charlie", "delete_subscribes": True}
    )
    assert delete_resp.success is True
    assert delete_resp.data["deleted_subscribes"] == 1
    assert UserOper().get_by_name("emby_charlie") is None
    assert SubscribeOper().list_by_username("emby_charlie") == []


def test_emby_account_crud_methods_and_plugin_endpoints(monkeypatch):
    """验证 Emby 实例与 EmbyUserManager 插件的 Emby 账号增删改查及本地用户名迁移。"""
    import json as json_mod
    EmbyUserManager = pytest.importorskip(
        "app.plugins.embyusermanager",
        reason="EmbyUserManager 属于独立插件仓库，未安装时跳过插件集成测试",
    ).EmbyUserManager

    emby = Emby.__new__(Emby)
    emby._host = "http://emby.local/"
    emby._apikey = "api-key"

    store = {
        "u100": {
            "Id": "u100",
            "Name": "emby_newbie",
            "HasPassword": False,
            "Policy": {
                "IsAdministrator": False,
                "IsDisabled": False,
                "IsHidden": True,
                "EnableAllFolders": True,
                "EnabledFolders": [],
                "EnableRemoteAccess": True,
                "EnableContentDownloading": True,
                "EnableVideoPlaybackTranscoding": True,
                "EnableAudioPlaybackTranscoding": True,
                "SimultaneousStreamLimit": 0,
            },
        }
    }
    passwords = {}

    class FakeResp:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            return self._payload

    def _fake_get_res(self, url, params=None, **kwargs):
        if url.endswith("/Library/SelectableMediaFolders"):
            return FakeResp(
                200,
                [
                    {"Id": "1", "Guid": "guid-movie", "Name": "电影库"},
                    {"Id": "2", "Guid": "guid-tv", "Name": "剧集库"},
                ],
            )
        if url.endswith("/Users"):
            return FakeResp(200, list(store.values()))
        for uid, udata in store.items():
            if url.endswith(f"/Users/{uid}"):
                return FakeResp(200, dict(udata))
        return FakeResp(404, None, "Not found")

    def _fake_post_res(self, url, data=None, params=None, **kwargs):
        body = json_mod.loads(data) if isinstance(data, str) and data else {}
        if url.endswith("/Users/New"):
            uid = "u200" if body["Name"] == "emby_david" else "u300"
            store[uid] = {
                "Id": uid,
                "Name": body["Name"],
                "HasPassword": False,
                "Policy": {
                    "IsAdministrator": False,
                    "IsDisabled": False,
                    "IsHidden": True,
                    "EnableAllFolders": True,
                    "EnabledFolders": [],
                },
            }
            return FakeResp(200, store[uid])
        for uid in list(store.keys()):
            if url.endswith(f"/Users/{uid}/Password"):
                if body.get("ResetPassword"):
                    passwords.pop(uid, None)
                    store[uid]["HasPassword"] = False
                elif "NewPw" in body:
                    passwords[uid] = body["NewPw"]
                    store[uid]["HasPassword"] = bool(body["NewPw"])
                return FakeResp(204, {})
            if url.endswith(f"/Users/{uid}/Policy"):
                store[uid]["Policy"] = body
                return FakeResp(204, {})
            if url.endswith(f"/Users/{uid}"):
                store[uid]["Name"] = body.get("Name", store[uid]["Name"])
                return FakeResp(204, {})
        return FakeResp(404, None, "Not found")

    def _fake_delete_res(self, url, params=None, **kwargs):
        for uid in list(store.keys()):
            if url.endswith(f"/Users/{uid}"):
                store.pop(uid)
                return FakeResp(204, {})
        return FakeResp(404, None, "Not found")

    monkeypatch.setattr("app.modules.emby.emby.RequestUtils.get_res", _fake_get_res)
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils.post_res", _fake_post_res)
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils.delete_res", _fake_delete_res)
    monkeypatch.setattr(
        "app.plugins.embyusermanager.MediaServerHelper.get_services",
        lambda self, type_filter=None: {"EmbyMain": SimpleNamespace(instance=emby)},
    )
    monkeypatch.setattr(
        "app.plugins.embyusermanager.MediaServerHelper.get_service",
        lambda self, name=None, type_filter=None: SimpleNamespace(instance=emby),
    )

    plugin = EmbyUserManager()
    plugin.init_plugin({"enabled": True, "allow_mode": "whitelist", "whitelist": []})

    # 1. 新建 Emby 用户（增）并自动加入白名单 + 限定媒体库
    create_resp = plugin.api_create_emby_user(
        {
            "server": "EmbyMain",
            "username": "emby_david",
            "password": "init-password",
            "enable_all_folders": False,
            "enabled_folders": ["guid-movie"],
            "simultaneous_stream_limit": 2,
            "add_to_whitelist": True,
        }
    )
    assert create_resp.success is True
    assert create_resp.data["user_id"] == "u200"
    assert create_resp.data["password"] == "init-password"
    assert create_resp.data["generated_password"] is False
    assert passwords["u200"] == "init-password"
    assert store["u200"]["Policy"]["EnableAllFolders"] is False
    assert store["u200"]["Policy"]["EnabledFolders"] == ["guid-movie"]
    assert store["u200"]["Policy"]["SimultaneousStreamLimit"] == 2
    assert "emby_david" in plugin._whitelist

    random_resp = plugin.api_create_emby_user(
        {
            "server": "EmbyMain",
            "username": "emby_random",
            "password": "",
        }
    )
    assert random_resp.success is True
    assert random_resp.data["generated_password"] is True
    generated_password = random_resp.data["password"]
    assert isinstance(generated_password, str) and len(generated_password) >= 12
    assert passwords["u300"] == generated_password

    # 2. 查询单个 Emby 用户详情（查）
    detail_resp = plugin.api_get_emby_user(user_id="u200", server="EmbyMain")
    assert detail_resp.success is True
    assert detail_resp.data["username"] == "emby_david"
    assert detail_resp.data["policy"]["enabled_folders"] == ["guid-movie"]
    assert [f["id"] for f in detail_resp.data["folders"]] == ["guid-movie", "guid-tv"]

    # 为 emby_david 创建本地订阅，测试改名联动迁移（改）
    UserModel().delete_by_name(None, "emby_david")
    UserModel().delete_by_name(None, "emby_david_renamed")
    for uname in ("emby_david", "emby_david_renamed"):
        for s in SubscribeOper().list_by_username(uname):
            SubscribeModel.delete(None, rid=s.id)
    UserOper().add(
        name="emby_david",
        is_active=True,
        is_superuser=False,
        hashed_password="x",
    )
    SubscribeModel(
        name="奥本海默",
        year="2023",
        type=MediaType.MOVIE.value,
        tmdbid=872585,
        username="emby_david",
        state="R",
    ).create(None)

    update_resp = plugin.api_update_emby_user(
        {
            "server": "EmbyMain",
            "user_id": "u200",
            "username": "emby_david",
            "new_username": "emby_david_renamed",
            "change_password": True,
            "new_password": "updated-password",
            "mp_is_active": False,
            "policy": {
                "is_disabled": True,
                "enable_all_folders": True,
            },
        }
    )
    assert update_resp.success is True
    assert store["u200"]["Name"] == "emby_david_renamed"
    assert passwords["u200"] == "updated-password"
    assert store["u200"]["Policy"]["IsDisabled"] is True
    assert store["u200"]["Policy"]["EnableAllFolders"] is True
    assert "emby_david_renamed" in plugin._whitelist
    assert UserOper().get_by_name("emby_david") is None
    renamed_mp_user = UserOper().get_by_name("emby_david_renamed")
    assert renamed_mp_user is not None and renamed_mp_user.is_active is False
    assert len(SubscribeOper().list_by_username("emby_david_renamed")) == 1

    # 3. 从 Emby 服务器与本地同时删除该账号并清理订阅（删）
    del_resp = plugin.api_delete_user(
        {
            "username": "emby_david_renamed",
            "delete_from_emby": True,
            "delete_mp_user": True,
            "delete_subscribes": True,
        }
    )
    assert del_resp.success is True
    assert "u200" not in store
    assert UserOper().get_by_name("emby_david_renamed") is None
    assert SubscribeOper().list_by_username("emby_david_renamed") == []
