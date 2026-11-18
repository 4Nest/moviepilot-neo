"""订阅与工作流分享撤回、细粒度管理权限及载荷脱敏回归测试。"""

import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.cache import MemoryBackend
from app.core.config import settings
from app.db.user_oper import get_current_active_user_async, get_current_user_async
from app.helper.server import MoviePilotServerHelper


@pytest.fixture(autouse=True)
def isolate_share_state(monkeypatch):
    """隔离共享缓存、开关和安装身份，外部请求由各用例拦截。"""
    monkeypatch.setattr(MemoryBackend, "_region_caches", {})
    monkeypatch.setattr(settings, "SUBSCRIBE_STATISTIC_SHARE", True)
    monkeypatch.setattr(settings, "WORKFLOW_STATISTIC_SHARE", True)
    monkeypatch.setattr(MoviePilotServerHelper, "get_user_uid", lambda: "installed-owner")
    monkeypatch.setattr(MoviePilotServerHelper, "_github_user", None)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("share_uid", [None, "", "  ", "installed-owner"])
def test_cancel_share_uses_owner_identity_even_after_sharing_disabled(
    monkeypatch, asynchronous, enabled, share_uid
):
    """关闭后续数据共享不应阻止撤回本人订阅分享，且本人撤回无需请求远端权限接口。"""
    monkeypatch.setattr(settings, "SUBSCRIBE_STATISTIC_SHARE", enabled)
    response = Mock(status_code=200)
    response.json.return_value = {"success": True}
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    method = "_async_delete" if asynchronous else "_delete"
    monkeypatch.setattr(MoviePilotServerHelper, method, request)
    perm_check = AsyncMock() if asynchronous else Mock()
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_can_manage_subscribe_share" if asynchronous else "can_manage_subscribe_share",
        perm_check,
        raising=False,
    )

    with warnings.catch_warnings(record=True):
        if asynchronous:
            result = asyncio.run(MoviePilotServerHelper.async_share_delete(6201, share_uid=share_uid))
        else:
            result = MoviePilotServerHelper.share_delete(6201, share_uid=share_uid)

    assert result == (True, "")
    assert request.call_args.args[0].endswith("/subscribe/share/6201")
    assert request.call_args.kwargs["params"] == {"share_uid": "installed-owner"}
    perm_check.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("share_uid", [None, "installed-owner"])
def test_cancel_workflow_share_allows_owner_even_after_sharing_disabled(
    monkeypatch, asynchronous, enabled, share_uid
):
    """关闭工作流数据共享开关后仍允许撤回本人已有工作流分享。"""
    monkeypatch.setattr(settings, "WORKFLOW_STATISTIC_SHARE", enabled)
    response = Mock(status_code=200)
    response.json.return_value = {"code": 0}
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    method = "_async_delete" if asynchronous else "_delete"
    monkeypatch.setattr(MoviePilotServerHelper, method, request)

    if asynchronous:
        result = asyncio.run(
            MoviePilotServerHelper.async_workflow_share_delete_by_id(3101, share_uid=share_uid)
        )
    else:
        result = MoviePilotServerHelper.workflow_share_delete_by_id(3101, share_uid=share_uid)

    assert result == (True, "")
    assert request.call_args.args[0].endswith("/workflow/share/3101")
    assert request.call_args.kwargs["params"] == {"share_uid": "installed-owner"}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "permissions",
    [
        {"is_admin": True},
        {"is_admin": False, "subscribe_share_manage": True, "workflow_share_manage": True},
    ],
)
def test_admin_can_delete_other_user_subscribe_and_workflow_share(
    monkeypatch, asynchronous, permissions
):
    """中心端管理员传入他人 share_uid 时可将目标 share_uid 转发至远端删除订阅与工作流分享。"""
    monkeypatch.setattr(settings, "WORKFLOW_STATISTIC_SHARE", False)
    response = Mock(status_code=200)
    response.json.return_value = {"code": 0}
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "_async_delete" if asynchronous else "_delete",
        request,
    )
    if asynchronous:
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "async_get_user_permissions",
            AsyncMock(return_value=permissions),
        )
        sub_res = asyncio.run(
            MoviePilotServerHelper.async_share_delete(6201, share_uid="other-user-uid")
        )
        wf_res = asyncio.run(
            MoviePilotServerHelper.async_workflow_share_delete_by_id(3101, share_uid="legacy-uid")
        )
    else:
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "get_user_permissions",
            Mock(return_value=permissions),
        )
        sub_res = MoviePilotServerHelper.share_delete(6201, share_uid="other-user-uid")
        wf_res = MoviePilotServerHelper.workflow_share_delete_by_id(3101, share_uid="legacy-uid")

    assert sub_res == (True, "")
    assert wf_res == (True, "")
    assert request.call_args_list[0].args[0].endswith("/subscribe/share/6201")
    assert request.call_args_list[0].kwargs["params"] == {"share_uid": "other-user-uid"}
    assert request.call_args_list[1].args[0].endswith("/workflow/share/3101")
    assert request.call_args_list[1].kwargs["params"] == {"share_uid": "legacy-uid"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_non_admin_cannot_delete_other_user_subscribe_or_workflow_share(monkeypatch, asynchronous):
    """无对应中心端管理权限时传入他人 share_uid 必须被本地拦截，不得向远端发请求。"""
    request = AsyncMock() if asynchronous else Mock()
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "_async_delete" if asynchronous else "_delete",
        request,
    )

    if asynchronous:
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "async_get_user_permissions",
            AsyncMock(return_value={"is_admin": False, "workflow_share_manage": True}),
        )
        sub_res = asyncio.run(
            MoviePilotServerHelper.async_share_delete(6201, share_uid="other-user-uid")
        )
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "async_get_user_permissions",
            AsyncMock(return_value={"is_admin": False, "subscribe_share_manage": True}),
        )
        wf_res = asyncio.run(
            MoviePilotServerHelper.async_workflow_share_delete_by_id(3101, share_uid="other-user-uid")
        )
    else:
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "get_user_permissions",
            Mock(return_value={"is_admin": False, "workflow_share_manage": True}),
        )
        sub_res = MoviePilotServerHelper.share_delete(6201, share_uid="other-user-uid")
        monkeypatch.setattr(
            MoviePilotServerHelper,
            "get_user_permissions",
            Mock(return_value={"is_admin": False, "subscribe_share_manage": True}),
        )
        wf_res = MoviePilotServerHelper.workflow_share_delete_by_id(3101, share_uid="other-user-uid")

    assert sub_res == (False, "无权限删除他人分享")
    assert wf_res == (False, "无权限删除他人分享")
    request.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sub_share_whitelist_strips_private_and_runtime_fields(monkeypatch, asynchronous):
    """订阅分享上报载荷只允许包含白名单公共字段，剥离目录、站点、下载器、用户名等私有字段。"""
    raw_subscribe_dict = {
        "id": 88,
        "name": "葬送的芙莉莲",
        "year": "2023",
        "type": "电视剧",
        "keyword": "Frieren",
        "tmdbid": 209867,
        "imdbid": "tt22248376",
        "tvdbid": 424536,
        "doubanid": "36093329",
        "bangumiid": 400602,
        "anilistid": 154587,
        "mediaid": "tmdb:209867",
        "media_source": "themoviedb",
        "media_id": "209867",
        "season": 1,
        "poster": "https://img.example/poster.jpg",
        "backdrop": "https://img.example/backdrop.jpg",
        "vote": 9.5,
        "description": "简介",
        "filter": "1080p",
        "include": "简日",
        "exclude": "720p",
        "quality": "BluRay",
        "resolution": "1080p",
        "effect": "DV",
        "total_episode": 28,
        "start_episode": 1,
        "custom_words": "Frieren",
        "media_category": "动漫",
        "filter_groups": ["动画组"],
        "episode_group": "eg-1",
        # 以下为私有或运行状态字段，严禁外发
        "username": "private-admin",
        "sites": [1, 3, 5],
        "downloader": "qb-private",
        "save_path": "/mnt/nas/private/anime",
        "note": [1, 2, 3],
        "lack_episode": 25,
        "state": "R",
        "last_update": "2026-11-17 12:00:00",
        "date": "2026-11-16 12:00:00",
        "best_version": 1,
        "best_version_full": 0,
        "current_priority": 90,
        "episode_priority": {"1": 100},
        "version_rules": [{"id": "v1", "name": "BDRip"}],
        "version_progress": {"v1": {"lack_episode": 10}},
        "decision_summary": {"result": "matched"},
        "version_mode": "all",
        "search_imdbid": 1,
        "skip_library_check": 1,
        "manual_total_episode": 0,
    }
    fake_subscribe = Mock()
    fake_subscribe.to_dict.return_value = dict(raw_subscribe_dict)

    response = Mock(status_code=200)
    response.json.return_value = {"code": 0}

    if asynchronous:
        fake_oper = Mock()
        fake_oper.async_get = AsyncMock(return_value=fake_subscribe)
        monkeypatch.setattr("app.helper.server.SubscribeOper", lambda: fake_oper)
        share_req = AsyncMock(return_value=response)
        monkeypatch.setattr(MoviePilotServerHelper, "async_subscribe_share", share_req)
        result = asyncio.run(
            MoviePilotServerHelper.async_sub_share(88, "芙莉莲精校", "附识别词", "tester")
        )
    else:
        fake_oper = Mock()
        fake_oper.get.return_value = fake_subscribe
        monkeypatch.setattr("app.helper.server.SubscribeOper", lambda: fake_oper)
        share_req = Mock(return_value=response)
        monkeypatch.setattr(MoviePilotServerHelper, "subscribe_share", share_req)
        result = MoviePilotServerHelper.sub_share(88, "芙莉莲精校", "附识别词", "tester")

    assert result == (True, "")
    sent_payload = share_req.call_args.args[0]
    for field in MoviePilotServerHelper._SUBSCRIBE_SHARE_FIELDS:
        assert sent_payload[field] == raw_subscribe_dict[field]
    assert sent_payload["share_title"] == "芙莉莲精校"
    assert sent_payload["share_comment"] == "附识别词"
    assert sent_payload["share_user"] == "tester"
    assert sent_payload["share_uid"] == "installed-owner"
    for forbidden in (
        "id",
        "imdbid",
        "tvdbid",
        "username",
        "sites",
        "downloader",
        "save_path",
        "note",
        "lack_episode",
        "state",
        "last_update",
        "date",
        "version_rules",
        "version_progress",
        "decision_summary",
        "version_mode",
        "best_version",
        "current_priority",
        "episode_priority",
    ):
        assert forbidden not in sent_payload


def test_user_permissions_short_ttl_cache_and_isolation(monkeypatch):
    """中心端权限查询应命中进程内短缓存（含空字典），网络失败不缓存且支持主动清理。"""
    ok_resp = Mock(status_code=200)
    ok_resp.json.return_value = {"is_admin": False, "subscribe_share_manage": True}
    fail_resp = Mock(status_code=502)

    monkeypatch.setattr(MoviePilotServerHelper, "get_github_user", lambda: "octocat")
    monkeypatch.setattr(
        MoviePilotServerHelper, "async_get_github_user", AsyncMock(return_value="octocat")
    )
    sync_query = Mock(side_effect=[fail_resp, ok_resp])
    async_query = AsyncMock(return_value=ok_resp)
    monkeypatch.setattr(MoviePilotServerHelper, "user_permissions", sync_query)
    monkeypatch.setattr(MoviePilotServerHelper, "async_user_permissions", async_query)

    # 首次 502 不应被缓存
    assert MoviePilotServerHelper.get_user_permissions() == {}
    # 第二次请求成功并写入短缓存
    assert MoviePilotServerHelper.can_manage_subscribe_share() is True
    assert MoviePilotServerHelper.can_manage_workflow_share() is False
    assert sync_query.call_count == 2

    # 异步接口共享同一缓存区域，不再发起远端请求
    assert asyncio.run(MoviePilotServerHelper.async_can_manage_subscribe_share()) is True
    assert asyncio.run(MoviePilotServerHelper.async_can_manage_workflow_share()) is False
    async_query.assert_not_awaited()

    # 清理缓存后重新请求远端
    MoviePilotServerHelper.clear_user_permissions_cache()
    assert asyncio.run(MoviePilotServerHelper.async_can_manage_subscribe_share()) is True
    async_query.assert_awaited_once_with("octocat")


@pytest.mark.parametrize(
    "is_superuser, remote_permissions, expected_sub, expected_wf, expect_called",
    [
        (True, {"is_admin": True}, True, True, True),
        (
            True,
            {"is_admin": False, "subscribe_share_manage": True, "workflow_share_manage": False},
            True,
            False,
            True,
        ),
        (
            True,
            {"is_admin": False, "subscribe_share_manage": False, "workflow_share_manage": True},
            False,
            True,
            True,
        ),
        (True, {}, False, False, True),
        (False, {"is_admin": True}, False, False, False),
    ],
)
def test_user_global_setting_returns_granular_share_manage_flags(
    monkeypatch, is_superuser, remote_permissions, expected_sub, expected_wf, expect_called
):
    """/api/v1/system/global/user 按细粒度权限分别返回订阅分享与工作流分享管理标识。"""
    from app.api.endpoints import system as system_endpoint

    perm_mock = AsyncMock(return_value=remote_permissions)
    monkeypatch.setattr(MoviePilotServerHelper, "async_get_user_permissions", perm_mock)

    app = FastAPI()
    app.include_router(system_endpoint.router, prefix="/api/v1/system")
    user = SimpleNamespace(name="user", is_active=True, is_superuser=is_superuser)
    app.dependency_overrides[get_current_active_user_async] = lambda: user

    with TestClient(app) as client:
        resp = client.get("/api/v1/system/global/user")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["USER_UNIQUE_ID"] == "installed-owner"
    assert body["data"]["SUBSCRIBE_SHARE_MANAGE"] is expected_sub
    assert body["data"]["WORKFLOW_SHARE_MANAGE"] is expected_wf
    assert perm_mock.await_count == (1 if expect_called else 0)


def test_share_delete_endpoints_enforce_admin_and_forward_share_uid(monkeypatch):
    """非超管调用删除分享路由必须返回 403；超管调用时正确透传 share_uid。"""
    from app.api.endpoints import subscribe as subscribe_endpoint
    from app.api.endpoints import workflow as workflow_endpoint

    sub_delete_mock = AsyncMock(return_value=(True, ""))
    wf_delete_mock = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(MoviePilotServerHelper, "async_share_delete", sub_delete_mock)
    monkeypatch.setattr(
        MoviePilotServerHelper, "async_workflow_share_delete_by_id", wf_delete_mock
    )

    app = FastAPI()
    app.include_router(subscribe_endpoint.router, prefix="/api/v1/subscribe")
    app.include_router(workflow_endpoint.router, prefix="/api/v1/workflow")

    # 1) 普通非超管用户访问被 get_current_admin_async 拦截返回 403
    normal_user = SimpleNamespace(name="member", is_active=True, is_superuser=False)
    app.dependency_overrides[get_current_user_async] = lambda: normal_user
    with TestClient(app) as client:
        sub_forbidden = client.delete("/api/v1/subscribe/share/6201?share_uid=target-uid")
        wf_forbidden = client.delete("/api/v1/workflow/share/3101?share_uid=target-uid")

    assert sub_forbidden.status_code == 403
    assert wf_forbidden.status_code == 403
    sub_delete_mock.assert_not_awaited()
    wf_delete_mock.assert_not_awaited()

    # 2) canonical 超管用户访问放行并透传 share_uid
    admin_user = SimpleNamespace(name=settings.SUPERUSER, is_active=True, is_superuser=True)
    app.dependency_overrides[get_current_user_async] = lambda: admin_user
    with TestClient(app) as client:
        sub_ok = client.delete("/api/v1/subscribe/share/6201?share_uid=target-sub-uid")
        wf_ok = client.delete("/api/v1/workflow/share/3101?share_uid=target-wf-uid")

    assert sub_ok.status_code == 200
    assert sub_ok.json()["success"] is True
    assert wf_ok.status_code == 200
    assert wf_ok.json()["success"] is True
    sub_delete_mock.assert_awaited_once_with(share_id=6201, share_uid="target-sub-uid")
    wf_delete_mock.assert_awaited_once_with(share_id=3101, share_uid="target-wf-uid")


@pytest.mark.parametrize("status, payload, message", [
    (200, {"success": False, "message": "没有删除权限"}, "没有删除权限"),
    (200, {"success": False, "detail": "分享不属于当前用户"}, "分享不属于当前用户"),
    (200, {"success": False}, "操作失败"),
    (403, {"detail": "分享不属于当前用户"}, "分享不属于当前用户"),
])
def test_cancel_share_preserves_business_failure_in_api_response(monkeypatch, status, payload, message):
    """HTTP 200 内的业务失败不能变成取消成功，错误原因应原样返回前端。"""
    from app.api.endpoints.subscribe import subscribe_share_delete

    response = Mock(status_code=status)
    response.json.return_value = payload
    monkeypatch.setattr(MoviePilotServerHelper, "_async_delete", AsyncMock(return_value=response))
    clear_cache = Mock()
    monkeypatch.setattr(MoviePilotServerHelper, "_clear_subscribe_share_cache", clear_cache)

    result = asyncio.run(subscribe_share_delete(6201, _=None))

    assert result.success is False
    assert result.message == message
    clear_cache.assert_not_called()


def test_cancel_share_refreshes_cached_list_without_unawaited_coroutines(monkeypatch):
    """取消成功后刷新列表必须重新查询服务端，且不能留下未执行的清缓存协程。"""
    before = Mock(status_code=200)
    before.json.return_value = [{"id": 6201, "share_title": "已取消的分享"}]
    after = Mock(status_code=200)
    after.json.return_value = []
    deleted = Mock(status_code=200)
    deleted.json.return_value = {"success": True}
    query = AsyncMock(side_effect=[before, after])
    monkeypatch.setattr(MoviePilotServerHelper, "async_subscribe_shares", query)
    monkeypatch.setattr(MoviePilotServerHelper, "_async_delete", AsyncMock(return_value=deleted))

    async def cancel_and_refresh():
        """复放先读列表、取消分享、刷新页面的完整顺序。"""
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == before.json()
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == before.json()
        assert query.await_count == 1
        assert await MoviePilotServerHelper.async_share_delete(6201) == (True, "")
        assert await MoviePilotServerHelper.async_get_subscribe_shares() == []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        asyncio.run(cancel_and_refresh())

    assert query.await_count == 2
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.parametrize(
    "payload",
    [{"success": True}, {"code": 0, "message": "success"}, {"code": "0"}, {"message": "删除成功"}, None],
)
def test_server_response_preserves_legacy_success_contract(payload):
    """保留公共服务及旧服务端的成功响应契约。"""
    response = Mock(status_code=200)
    if payload is None:
        response.json.side_effect = ValueError("旧版纯文本响应")
    else:
        response.json.return_value = payload
    clear_cache = Mock()

    assert MoviePilotServerHelper._handle_response(response, clear_cache) == (True, "")
    clear_cache.assert_called_once_with()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"code": 1, "message": "分享不存在或无权限"}, "分享不存在或无权限"),
        ({"code": 2, "message": "昵称已分享"}, "昵称已分享"),
        ({"code": 1}, "操作失败"),
    ],
)
def test_server_code_failure_is_not_treated_as_success(payload, message):
    """公共服务以 HTTP 200 返回非零 code 时仍应保留失败结果。"""
    response = Mock(status_code=200)
    response.json.return_value = payload
    clear_cache = Mock()

    assert MoviePilotServerHelper._handle_response(response, clear_cache) == (False, message)
    clear_cache.assert_not_called()
