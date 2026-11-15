"""NEO 自研后端加固（Emby 认证迁移、图标单飞与魔数校验、熔断半开单飞、原子替换与 Nginx 路由）回归测试。"""

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

from app import schemas
from app.chain import ChainBase
from app.chain.site import SiteChain, _is_auth_failure_reason
from app.chain.user import UserChain
from app.core.config import settings
from app.core.security import get_password_hash
from app.db.models.site import Site
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.user_oper import UserOper
from app.helper.resource import ResourceHelper
from app.helper.site import SiteHealthSentinel
from app.modules.emby import EmbyModule
from app.modules.emby.emby import Emby
from app.schemas import SiteUserData


def test_emby_authenticate_user_extracts_canonical_name_and_id_compatibly(monkeypatch):
    """Emby.authenticate_user 提取规范用户名与不可变 ID，且 Emby.authenticate 保持返回字符串令牌。"""
    emby = object.__new__(Emby)
    emby._host = "http://emby.local/"
    emby._apikey = "emby-api-key"

    monkeypatch.setattr(
        "app.modules.emby.emby.RequestUtils.post_res",
        lambda self, url, data=None, **_kw: SimpleNamespace(
            json=lambda: {
                "AccessToken": "tok-canonical-1",
                "User": {"Id": "emby-uid-9001", "Name": "AliceCanonical"},
            }
        ),
    )

    info = emby.authenticate_user("alicecanonical", "pass123")
    assert info == {
        "token": "tok-canonical-1",
        "username": "AliceCanonical",
        "user_id": "emby-uid-9001",
    }
    assert emby.authenticate("alicecanonical", "pass123") == "tok-canonical-1"

    mod = EmbyModule()
    monkeypatch.setattr(mod, "get_instances", lambda: {"MainEmby": emby})
    creds = schemas.AuthCredentials(
        username="alicecanonical",
        password="pass123",
        grant_type="password",
    )
    out = mod.user_authenticate(creds)
    assert out is not None
    assert out.token == "tok-canonical-1"
    assert out.canonical_username == "AliceCanonical"
    assert out.emby_user_id == "emby-uid-9001"


def test_emby_user_rename_migration_integrity_fallback_and_local_password_auth(monkeypatch):
    """Emby 改名自动迁移用户名与订阅归属、并发建号 IntegrityError 兜底，且非 Emby 本地账号保留密码登录。"""
    for name in ("EmbyOldName", "EmbyNewName", "ConcurrentEmby", "LocalMember"):
        User().delete_by_name(None, name)

    # 1. 首次使用小写输入登录，应按 Emby 返回的规范用户名建号并持久化 emby_user_id
    def fake_emby_auth(self, method: str, **kwargs):
        if method != "user_authenticate":
            return None
        creds: schemas.AuthCredentials = kwargs["credentials"]
        if creds.username.lower() == "embyoldname" and creds.password == "emby-pw":
            creds.channel = "Emby"
            creds.service = "Main"
            creds.token = "tok-1"
            creds.canonical_username = "EmbyOldName"
            creds.emby_user_id = "immutable-uid-42"
            return creds
        if creds.username in {"EmbyNewName", "embynewname"} and creds.password == "emby-pw":
            creds.channel = "Emby"
            creds.service = "Main"
            creds.token = "tok-2"
            creds.canonical_username = "EmbyNewName"
            creds.emby_user_id = "immutable-uid-42"
            return creds
        if creds.username == "ConcurrentEmby" and creds.password == "emby-pw":
            creds.channel = "Emby"
            creds.service = "Main"
            creds.token = "tok-3"
            creds.canonical_username = "ConcurrentEmby"
            creds.emby_user_id = "immutable-uid-99"
            return creds
        return None

    monkeypatch.setattr(UserChain, "run_module", fake_emby_auth)
    chain = UserChain()

    ok, created = chain.user_authenticate(username="embyoldname", password="emby-pw")
    assert ok is True
    assert created.name == "EmbyOldName"
    assert created.settings.get("emby_user_id") == "immutable-uid-42"
    assert created.settings.get("auth_source") == "emby"

    # 为旧用户名写入一条订阅与一条订阅历史，模拟用户在 Emby 服务端改名后再次登录
    sub = Subscribe(name="迁移测试剧", year="2026", type="电视剧", tmdbid=88001, season=1, state="R", username="EmbyOldName")
    sub.create(None)
    sub_his = SubscribeHistory(name="迁移历史剧", year="2025", type="电影", tmdbid=88002, username="EmbyOldName")
    sub_his.create(None)

    try:
        ok2, migrated_user = chain.user_authenticate(username="embynewname", password="emby-pw")
        assert ok2 is True
        assert migrated_user.name == "EmbyNewName"
        assert UserOper().get_by_name("EmbyOldName") is None
        assert len(Subscribe.list_by_username(None, "EmbyNewName")) == 1
        assert Subscribe.list_by_username(None, "EmbyOldName") == []

        # 2. 模拟并发首次建号触发 IntegrityError：add 抛错前另一请求已插入同名用户，应回查成功
        real_add = UserOper.add

        def race_add(self, **kwargs):
            real_add(self, **kwargs)
            raise IntegrityError("INSERT INTO user", {}, Exception("UNIQUE constraint failed: user.name"))

        monkeypatch.setattr(UserOper, "add", race_add)
        ok_race, race_user = chain.user_authenticate(username="ConcurrentEmby", password="emby-pw")
        assert ok_race is True
        assert race_user.name == "ConcurrentEmby"
        assert race_user.settings.get("emby_user_id") == "immutable-uid-99"
        monkeypatch.setattr(UserOper, "add", real_add)

        # 3. 已存在且非 Emby 来源的本地有密码普通账号保留本地密码认证能力
        UserOper().add(
            name="LocalMember",
            is_active=True,
            is_superuser=False,
            hashed_password=get_password_hash("LocalSecret#123"),
            settings={"nickname": "本地成员"},
        )
        ok_local, local_user = chain.user_authenticate(
            username="LocalMember", password="LocalSecret#123"
        )
        assert ok_local is True
        assert local_user.name == "LocalMember"
    finally:
        for item in Subscribe.list_by_username(None, "EmbyNewName") + Subscribe.list_by_username(None, "EmbyOldName"):
            Subscribe.delete(None, item.id)
        for name in ("EmbyOldName", "EmbyNewName", "ConcurrentEmby", "LocalMember"):
            User().delete_by_name(None, name)


def test_site_icon_single_flight_and_strict_magic_validation(monkeypatch):
    """ensure_site_icon 并发单飞只抓一次且共享结果，__parse_favicon 拒绝非 200 与 JSON/文本假图标。"""
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    SiteChain.clear_site_icon_cooldown()

    # 1. 验证 __parse_favicon 拒绝 403 PNG、200 JSON、200 纯文本，仅接受 200 合法图像
    responses = {
        "https://icon-test.org/": SimpleNamespace(
            status_code=200,
            text='<html><head><link rel="icon" href="/json-err.ico"></head></html>',
            content=b"<html></html>",
            headers={"Content-Type": "text/html"},
        ),
        "https://icon-test.org/json-err.ico": SimpleNamespace(
            status_code=200,
            text='{"error":"not found"}',
            content=b'{"error":"not found"}',
            headers={"Content-Type": "image/x-icon"},
        ),
        "https://icon-test.org/favicon.ico": SimpleNamespace(
            status_code=403,
            text="",
            content=b"\x89PNG\r\n\x1a\nFORBIDDEN",
            headers={"Content-Type": "image/png"},
        ),
    }
    monkeypatch.setattr(
        "app.chain.site.RequestUtils.get_res",
        lambda self, url, **_kw: responses.get(url),
    )
    icon_url, icon_b64 = SiteChain._SiteChain__parse_favicon(
        url="https://icon-test.org/", cookie="", ua="Mozilla/5.0"
    )
    assert icon_b64 is None
    assert icon_url == "https://icon-test.org/json-err.ico"

    # 改为 200 合法 WEBP 魔数后应成功提取 Base64
    responses["https://icon-test.org/favicon.ico"] = SimpleNamespace(
        status_code=200,
        text="",
        content=b"RIFF\x10\x00\x00\x00WEBPVP8 ",
        headers={"Content-Type": "image/webp"},
    )
    ok_url, ok_b64 = SiteChain._SiteChain__parse_favicon(
        url="https://icon-test.org/", cookie="", ua="Mozilla/5.0"
    )
    assert ok_url == "https://icon-test.org/favicon.ico"
    assert ok_b64 is not None

    # 2. 验证 ensure_site_icon 并发 Single-flight：5 个线程并发读取缺失图标只执行 1 次抓取并全部拿到 Base64
    db_icon = {"record": None}
    fetch_calls = {"n": 0}
    enter_barrier = threading.Event()
    release_fetch = threading.Event()

    oper = Mock()
    oper.get_icon_by_domain.side_effect = lambda _d: db_icon["record"]
    monkeypatch.setattr("app.chain.site.SiteOper", lambda: oper)

    def slow_cache_icon(self, domain: str):
        fetch_calls["n"] += 1
        enter_barrier.set()
        release_fetch.wait(timeout=3.0)
        db_icon["record"] = SimpleNamespace(
            url=f"https://{domain}/favicon.ico",
            base64="data:image/png;base64,AAA",
        )
        return "data:image/png;base64,AAA"

    monkeypatch.setattr(SiteChain, "cache_site_icon_by_domain", slow_cache_icon)

    results = [None] * 5

    def _worker(idx: int):
        if idx > 0:
            enter_barrier.wait(timeout=3.0)
        results[idx] = SiteChain().ensure_site_icon("singleflight.example")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
    threads[0].start()
    assert enter_barrier.wait(timeout=3.0)
    for t in threads[1:]:
        t.start()
    time.sleep(0.05)
    release_fetch.set()
    for t in threads:
        t.join(timeout=3.0)

    assert fetch_calls["n"] == 1
    assert results == ["data:image/png;base64,AAA"] * 5


def test_sentinel_half_open_single_flight_and_cloudflare_gateway_non_auth(monkeypatch):
    """熔断冷却到期仅放行单个半开试探请求，且 Cloudflare 522/524 网关超时不误判为单次鉴权失效告警。"""
    now = {"ts": 1000.0}
    monkeypatch.setattr("app.helper.site.time.time", lambda: now["ts"])

    for _ in range(3):
        SiteHealthSentinel.record_failure("probe.example.com", "502 Bad Gateway")
    assert SiteHealthSentinel.is_circuit_open("probe.example.com")[0] is True

    # 冷却到期后：首个并发请求放行试探，第二个并发跟风请求被拦截
    now["ts"] = 2805.0
    assert SiteHealthSentinel.is_circuit_open("probe.example.com") == (False, None)
    blocked_second, until_str = SiteHealthSentinel.is_circuit_open("probe.example.com")
    assert blocked_second is True
    assert until_str is not None

    # 试探成功后彻底清空状态
    SiteHealthSentinel.record_success("probe.example.com")
    assert SiteHealthSentinel.is_circuit_open("probe.example.com") == (False, None)

    # 验证 Cloudflare 522/524 抖动不触发单次鉴权告警，而 403/Cookie/Turnstile 仍立即告警
    assert _is_auth_failure_reason("错误：522 Connection timed out (Cloudflare)") is False
    assert _is_auth_failure_reason("Cloudflare 524 A timeout occurred") is False
    assert _is_auth_failure_reason("502 Bad Gateway") is False
    assert _is_auth_failure_reason("403 Forbidden - Cloudflare Turnstile challenge") is True
    assert _is_auth_failure_reason("Cookie 已失效，请重新登录") is True

    chain = SiteChain()
    notifications = []
    monkeypatch.setattr(chain, "post_message", notifications.append)
    monkeypatch.setattr(
        chain,
        "run_module",
        lambda *_a, **_kw: SiteUserData(err_msg="Cloudflare 522 Connection timed out"),
    )
    site = {"domain": "cf522.example.com", "name": "CF网关站", "public": False, "schema": "NexusPhp", "note": None}
    chain.refresh_userdata(site)
    assert notifications == []


def test_resource_helper_atomic_binary_replace(monkeypatch, tmp_path):
    """ResourceHelper 更新二进制 .so 时应先写同目录 .tmp 再通过 os.replace 原子替换。"""
    from app.utils.system import SystemUtils

    helper = object.__new__(ResourceHelper)
    helper._base_dir = tmp_path
    monkeypatch.setattr(settings, "PROXY_HOST", "", raising=False)
    monkeypatch.setattr(settings, "GITHUB_PROXY", "", raising=False)

    target_dir = tmp_path / "app" / "helper"
    target_dir.mkdir(parents=True, exist_ok=True)
    so_name = "sites.cpython-312-darwin.so"
    existing_so = target_dir / so_name
    existing_so.write_bytes(b"OLD_BINARY")

    monkeypatch.setattr(helper, "_sync_neo_sites", lambda _meta: False)
    monkeypatch.setattr(helper, "_get_needed_files", lambda: [so_name])
    monkeypatch.setattr(
        "app.helper.resource.SitesHelper",
        lambda: SimpleNamespace(auth_version="1.0.0", indexer_version="1.0.0"),
    )
    restarted = []
    monkeypatch.setattr("app.helper.resource.SystemHelper.restart", lambda: restarted.append(True))

    manifest_json = __import__("json").dumps(
        {
            "version": "9.9.9",
            "resources": {
                so_name: {
                    "type": "sites",
                    "platform": SystemUtils.platform(),
                    "target": "app/helper",
                    "version": "9.9.9",
                }
            },
        }
    )

    def fake_get_res(self, url, **_kw):
        if url.endswith("package.v2.json"):
            return SimpleNamespace(ok=True, status_code=200, text=manifest_json)
        if url == helper._files_api:
            return SimpleNamespace(
                ok=True,
                status_code=200,
                json=lambda: [{"name": so_name, "download_url": f"https://dl.example/{so_name}"}],
            )
        return SimpleNamespace(ok=True, status_code=200, content=b"NEW_ATOMIC_BINARY")

    monkeypatch.setattr("app.helper.resource.RequestUtils.get_res", fake_get_res)

    replace_calls = []
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        src_path, dst_path = Path(src), Path(dst)
        assert src_path.parent == dst_path.parent
        assert src_path.name.endswith(".tmp")
        assert dst_path.read_bytes() == b"OLD_BINARY"
        replace_calls.append((src_path.name, dst_path.name))
        return real_replace(src, dst)

    monkeypatch.setattr("app.helper.resource.os.replace", spy_replace)
    monkeypatch.setattr(settings, "AUTO_UPDATE_RESOURCE", True)

    helper.check(force=True)
    assert len(replace_calls) == 1
    assert existing_so.read_bytes() == b"NEW_ATOMIC_BINARY"
    assert list(target_dir.glob("*.tmp")) == []
    assert restarted == [True]


def test_nginx_common_and_template_prefix_locations_and_syntax():
    """校验 nginx.common.conf 使用 ^~ 保护后端与静态目录路由、保留嵌套 SSE，并通过 nginx -t 校验。"""
    repo_root = Path(settings.ROOT_PATH)
    common_conf = (repo_root / "docker" / "nginx.common.conf").read_text("utf-8")
    template_conf = (repo_root / "docker" / "nginx.template.conf").read_text("utf-8")
    entrypoint_sh = (repo_root / "docker" / "entrypoint.sh").read_text("utf-8")

    assert "location ^~ /api {" in common_conf
    assert "location ^~ /api/v1/site/icon/ {" in common_conf
    assert "location ^~ /assets {" in common_conf
    assert "location ~ ^/api/v1/(system/(message|progress/|logging)|search/.*/stream$|message/agent/stream$)" in common_conf
    assert "client_max_body_size ${NGINX_CLIENT_MAX_BODY_SIZE};" in template_conf
    assert '["NGINX_CLIENT_MAX_BODY_SIZE"]="50m"' in entrypoint_sh

    nginx_bin = shutil.which("nginx") or ("/opt/homebrew/bin/nginx" if Path("/opt/homebrew/bin/nginx").exists() else None)
    if nginx_bin:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "cache").mkdir()
            (td_path / "common.conf").write_text(common_conf, "utf-8")
            rendered = (
                template_conf.replace("user moviepilot;", "")
                .replace("/var/run/nginx.pid", str(td_path / "nginx.pid"))
                .replace("/tmp levels=1:2", f"{td_path}/cache levels=1:2")
                .replace("include /etc/nginx/mime.types;", "types { text/html html; }")
                .replace("include common.conf;", f"include {td_path}/common.conf;")
                .replace("${NGINX_CLIENT_MAX_BODY_SIZE}", "50m")
                .replace("${NGINX_PORT}", "18080")
                .replace("${PORT}", "18081")
                .replace("${HTTPS_SERVER_CONF}", "")
            )
            conf_file = td_path / "nginx.conf"
            conf_file.write_text(rendered, "utf-8")
            proc = subprocess.run(
                [nginx_bin, "-t", "-c", str(conf_file), "-e", str(td_path / "error.log")],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
