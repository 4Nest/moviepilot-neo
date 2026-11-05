"""
私有 AppPush 的认证边界、自动接入、用户身份迁移和通知路由测试。
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.api.endpoints import notification
from app.db.models.systemconfig import SystemConfig
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import UserOper
from app.helper.server import MoviePilotServerHelper
from app.modules.apppush import AppPushModule
from app.schemas.message import Notification
from app.schemas.system import NotificationConf
from app.schemas.types import MessageChannel, NotificationType, SystemConfigKey

SERVER = "https://relay.test"
UID = "existing-deployment-user-unique-id"


def test_app_device_endpoint_forces_authenticated_owner(monkeypatch):
    """
    当前用户可管理自己的安装，正文提交其他 owner 必须在进入渠道前拒绝。
    """
    calls = []

    class Chain:
        """
        记录最终进入通知链的参数，不触发真实模块和网络。
        """

        def manage_channel(self, **params):
            calls.append(params)
            return {"success": True, "data": {"owner": params["owner"]}}

    monkeypatch.setattr(notification, "NotificationChain", Chain)
    app = FastAPI()
    app.include_router(notification.router, prefix="/notification")
    app.dependency_overrides[notification.get_current_active_user] = lambda: SimpleNamespace(
        notification_identity="a" * 32,
        is_active=True,
    )
    body = {"action": "status", "installation": "installation-001", "origin": "https://mp.test"}
    with TestClient(app) as client:
        assert client.post("/notification/app", json={**body, "owner": "b" * 32}).status_code == 422
        result = client.post("/notification/app", json=body)
    assert result.status_code == 200
    assert result.json()["data"]["owner"] == "a" * 32
    assert len(calls) == 1
    assert calls[0]["channel"] == "AppPush"


def test_instance_rotation_requires_superuser_and_does_not_accept_keys(monkeypatch):
    """
    管理员请求轮换凭据不接受自选用户、密钥或 URL。
    """
    calls = []

    class Chain:
        def manage_channel(self, **params):
            calls.append(params)
            return {"success": True, "data": {"rotated": True}}

    monkeypatch.setattr(notification, "NotificationChain", Chain)
    app = FastAPI()
    app.include_router(notification.router, prefix="/notification")
    app.dependency_overrides[notification.get_current_active_superuser] = lambda: SimpleNamespace(
        notification_identity="a" * 32,
        is_superuser=True,
    )
    body = {"action": "rotate", "source": "automatic"}
    with TestClient(app) as client:
        assert client.post("/notification/app/instance", json={**body, "key": "private"}).status_code == 422
        response = client.post("/notification/app/instance", json=body)
        assert response.json()["data"] == {"rotated": True}
    assert calls == [
        {
            "channel": "AppPush",
            "action": "rotate_instance",
            "source": "automatic",
            "owner": "a" * 32,
            "administrative": True,
        }
    ]


def test_apppush_reuses_message_switches_and_never_broadcasts(monkeypatch):
    """
    仅命中现有消息开关、渠道、来源与定向用户时向 Server 发送。
    """
    conf = NotificationConf(name="private", type="apppush", enabled=True, switchs=["资源下载"])
    module = AppPushModule.__new__(AppPushModule)
    module._channel = MessageChannel.AppPush
    module._configs = {"private": conf}
    monkeypatch.setattr(module, "get_configs", lambda: {"private": conf})
    calls = []
    monkeypatch.setattr(module, "_request", lambda *args: calls.append(args) or {"total": 1, "accepted": 1})
    base = {"mtype": NotificationType.Download, "title": "下载完成", "targets": {"apppush": "a" * 32}}
    message = Notification(**base, link="https://foreign.test")
    message.set_notification_route_scope("user")
    module.post_message(message)
    assert len(calls) == 1
    assert calls[0][3]["owner"] == "a" * 32
    assert calls[0][3]["route"] == "/?shotcut=message"
    module.post_message(Notification(**{**base, "targets": {}}))
    module.post_message(Notification(**base, channel=MessageChannel.Telegram))
    module.post_message(Notification(**base, source="other"))
    conf.switchs = []
    module.post_message(Notification(**base))
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("poster", "image", "expected"),
    [
        ("https://images.test/poster.jpg", "https://images.test/backdrop.jpg", "https://images.test/poster.jpg"),
        (None, "https://images.test/backdrop.jpg", "https://images.test/backdrop.jpg"),
        ("data:image/png;base64,AA", "https://images.test/legacy.jpg", "https://images.test/legacy.jpg"),
        (None, "javascript:alert(1)", None),
        (None, "https://user:secret@images.test/poster.jpg", None),
        (None, "https://images.test/" + "a" * 2048, None),
        (None, None, None),
    ],
)
def test_apppush_preserves_poster_and_keeps_invalid_images_out_of_relay(monkeypatch, poster, image, expected):
    """
    真实消息投递优先海报并兼容旧图片，无效附件不阻断正文和实例内跳转。
    """
    conf = NotificationConf(name="App", type="apppush", enabled=True, switchs=["资源下载"])
    module = AppPushModule.__new__(AppPushModule)
    module._channel = MessageChannel.AppPush
    module._configs = {conf.name: conf}
    calls = []
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "_request", lambda *args: calls.append(args) or {"accepted": 1})
    message = Notification(
        mtype=NotificationType.Download,
        targets={"apppush": "a" * 32},
        title="下载完成",
        text="第一行\n第二行" + "长正文与海报应完整交接。\n" * 500,
        link="/history",
        poster=poster,
        image=image,
    )
    message.set_notification_route_scope("user")
    module.post_message(message)
    assert len(calls) == 1
    payload = calls[0][3]
    assert payload["title"] == message.title and payload["body"] == message.text and payload["route"] == "/history"
    assert payload.get("image") == expected
    assert ("image" in payload) == (expected is not None)


@pytest.mark.parametrize("scope", [None, "user", "admin", "all"])
def test_apppush_account_sets_require_explicit_all_scope(monkeypatch, scope):
    """
    账号集合只在明确的所有用户范围生效，重复身份去重且单账号失败不阻断后续账号。
    """
    conf = NotificationConf(name="App", type="apppush", enabled=True, switchs=["资源下载"])
    module = AppPushModule.__new__(AppPushModule)
    module._channel = MessageChannel.AppPush
    module._configs = {conf.name: conf}
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    owners = []

    def request(_conf, _method, _path, data):
        owners.append(data["owner"])
        if len(owners) == 1:
            raise ValueError("临时投递失败")
        return {"total": 1, "accepted": 1}

    monkeypatch.setattr(module, "_request", request)
    message = Notification(mtype=NotificationType.Download, targets={"apppush": ("a" * 32, "a" * 32, "b" * 32)})
    message.set_notification_route_scope(scope)
    module.post_message(message)
    assert owners == (["a" * 32, "b" * 32] if scope == "all" else [])


def test_device_management_endpoint_reaches_private_relay_without_exposing_key(monkeypatch):
    """
    真实端点经渠道和 HTTP 适配器管理本账号设备，私有密钥只进入中转请求头。
    """
    owner = "a" * 32
    key = "server-private-key-" * 3
    conf = NotificationConf(
        name="private",
        type="apppush",
        enabled=True,
        config={"PUSH_SERVER_URL": "https://relay.test/", "PUSH_RELAY_KEY": key},
    )
    module = AppPushModule.__new__(AppPushModule)
    monkeypatch.setattr(module, "get_configs", lambda: {"private": conf})
    requests = []
    reply = {"status": 200}

    class Transport:
        def __init__(self, **options):
            self.headers = options["headers"]
            assert self.headers == {"Authorization": "Bearer " + key}

        def request(self, method, url, **options):
            data = options.get("params") if method == "GET" else options.get("json")
            requests.append((method, url, data, options))
            assert data["owner"] == owner
            assert key not in url and key not in str(data)
            assert options["allow_redirects"] is False
            payload = (
                {"devices": []}
                if method == "GET"
                else {"code": "once-only-binding-code", "expires_in": 600}
                if url.endswith("/pairings")
                else {"total": 1, "accepted": 1, "failed": []}
                if url.endswith("/messages")
                else {"removed": True}
                if method == "DELETE"
                else {"id": "device-1", "transport": "apns"}
            )
            return SimpleNamespace(status_code=reply["status"], json=lambda: payload, text=key)

    class Chain:
        def manage_channel(self, **params):
            return module.channel_manage(**params)

    monkeypatch.setattr("app.helper.server.RequestUtils", Transport)
    monkeypatch.setattr(notification, "NotificationChain", Chain)
    app = FastAPI()
    app.include_router(notification.router, prefix="/notification")
    app.dependency_overrides[notification.get_current_active_user] = lambda: SimpleNamespace(
        notification_identity=owner,
        is_active=True,
    )
    body = {"installation": "installation-001", "origin": "https://mp.test/base"}
    with TestClient(app) as client:
        status = client.post("/notification/app", json={**body, "action": "status"}).json()
        assert status["data"] == {"devices": [], "owner": owner, "relay_url": "https://relay.test/push/app/"}
        paired = client.post("/notification/app", json={**body, "action": "pair"}).json()
        assert paired["data"]["expires_in"] == 600
        assert paired["data"]["relay_url"] == status["data"]["relay_url"]
        missing = client.post("/notification/app", json={**body, "action": "register"}).json()
        assert missing["success"] is False and len(requests) == 2
        registered = client.post(
            "/notification/app",
            json={
                **body,
                "action": "register",
                "token": "c" * 64,
                "topic": "com.example.moviepilot",
                "environment": "sandbox",
            },
        ).json()
        assert registered["data"]["id"] == "device-1"
        tested = client.post("/notification/app", json={**body, "action": "test"}).json()
        assert tested["data"]["accepted"] == 1
        removed = client.post("/notification/app", json={**body, "action": "unregister"}).json()
        assert removed["data"]["removed"] is True
        assert requests[-1][:3] == (
            "DELETE",
            "https://relay.test/push/devices",
            {"owner": owner, "installation": body["installation"]},
        )
        assert all(key not in str(result) for result in (status, paired, missing, registered, tested, removed))
        assert client.post("/notification/app", json={**body, "action": "pair", "source": "unknown"}).json()["success"] is False
        reply["status"] = 503
        rejected = client.post("/notification/app", json={**body, "action": "status"}).json()
        assert rejected["success"] is False and key not in str(rejected)
    assert module.channel_manage("Telegram", "status", owner=owner) is None
    assert module.channel_manage("AppPush", "status", owner="")["success"] is False
    assert module.channel_manage("AppPush", "unsupported", owner=owner)["success"] is False


def test_registration_persists_before_network_and_reuses_key_after_restart(monkeypatch):
    """
    渠道及凭据真实落库，重启后恢复相同配置和设备列表，不产生第二个实例。
    """
    oper = SystemConfigOper()
    original_creds = oper.get(SystemConfigKey.AppPushCredentials)
    original_notifs = oper.get(SystemConfigKey.Notifications)
    oper.set(SystemConfigKey.AppPushCredentials, {})
    monkeypatch.setattr("app.modules.apppush.get_runtime_setting", lambda _key: SERVER)
    monkeypatch.setattr(MoviePilotServerHelper, "get_user_uid", lambda: UID)
    calls = []

    class Transport:
        def __init__(self, **options):
            self.key = options["headers"]["Authorization"].removeprefix("Bearer ")

        def request(self, method, url, **options):
            stored = SystemConfig.get_by_key(oper._db, SystemConfigKey.AppPushCredentials.value).value
            assert stored[SERVER]["key"] == self.key
            calls.append((method, url, self.key))
            assert options["allow_redirects"] is False
            if url.endswith("/instances"):
                assert options["json"] == {"user_uid": UID}
                payload = {"instance": "a" * 32, "registered": True}
            else:
                payload = {"devices": [{"id": "bound-web-device", "transport": "webpush"}]}
            return SimpleNamespace(status_code=200, json=lambda: payload)

    try:
        monkeypatch.setattr("app.helper.server.RequestUtils", Transport)
        conf = NotificationConf(name="automatic", type="apppush", enabled=True, config={})
        oper.set(SystemConfigKey.Notifications, [conf.model_dump()])
        module = AppPushModule.__new__(AppPushModule)
        module.init_module()
        result = module.channel_manage("AppPush", "status", owner="a" * 32)
        key = oper.get(SystemConfigKey.AppPushCredentials)[SERVER]["key"]
        assert result["success"] is True and result["data"]["relay_url"] == SERVER + "/push/app/"
        assert key.startswith("mpa_") and len(key) == 47
        assert key not in str(result) and key not in str(conf.model_dump())

        second_module = AppPushModule.__new__(AppPushModule)
        second_module.init_module()
        restored = second_module.channel_manage("AppPush", "status", owner="a" * 32)
        assert restored["success"] is True
        assert restored["data"] == result["data"]
        assert sum(url.endswith("/instances") for _, url, _ in calls) == 1
        assert {request_key for _, _, request_key in calls} == {key}
    finally:
        oper.set(SystemConfigKey.AppPushCredentials, original_creds)
        oper.set(SystemConfigKey.Notifications, original_notifs)


def test_user_oper_injects_immutable_notification_identity():
    """
    个人设置忽略手工注入的 apppush，所有用户目标仅含已启用账号的不可变身份。
    """
    user_oper = UserOper()
    User(
        name="apppush_alice",
        is_active=True,
        settings={"apppush": "spoofed", "telegram_userid": "1001"},
        notification_identity="a" * 32,
    ).create(user_oper._db)
    User(
        name="apppush_inactive",
        is_active=False,
        settings={},
        notification_identity="b" * 32,
    ).create(user_oper._db)
    try:
        alice_settings = user_oper.get_settings("apppush_alice")
        assert alice_settings["apppush"] == "a" * 32
        assert alice_settings["telegram_userid"] == "1001"
        inactive_settings = user_oper.get_settings("apppush_inactive")
        assert "apppush" not in inactive_settings
        broadcast = user_oper.get_notification_broadcast_targets()
        assert "a" * 32 in broadcast.get("apppush", ())
        assert "b" * 32 not in broadcast.get("apppush", ())
    finally:
        User().delete_by_name(user_oper._db, "apppush_alice")
        User().delete_by_name(user_oper._db, "apppush_inactive")


def test_notification_identity_migration_is_stable_and_non_nullable(tmp_path):
    """
    旧用户逐个获得不同身份，重复迁移不改已绑定值，改名和主键复用不会串设备。
    """
    path = Path(__file__).parents[1] / "database/versions/c9d0e1f2a3b4_2_2_23.py"
    spec = importlib.util.spec_from_file_location("notification_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO user (name) VALUES ('alice'), ('bob')"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            first = list(connection.execute(text("SELECT notification_identity FROM user ORDER BY id")).scalars())
            module.upgrade()
        assert len(set(first)) == 2 and all(len(value) == 32 for value in first)
        assert list(connection.execute(text("SELECT notification_identity FROM user ORDER BY id")).scalars()) == first
        assert (
            next(
                column
                for column in inspect(connection).get_columns("user")
                if column["name"] == "notification_identity"
            )["nullable"]
            is False
        )
    engine.dispose()
