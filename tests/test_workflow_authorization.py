import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.endpoints import workflow as workflow_endpoint
from app.core.config import settings
from app.core.security import verify_token
from app.db.user_oper import (
    get_current_admin,
    get_current_admin_async,
    get_current_user,
    get_current_user_async,
)


def _declared_dependencies(func):
    """读取接口函数签名中直接声明的 FastAPI 依赖函数。"""
    dependencies = []
    for parameter in inspect.signature(func).parameters.values():
        default = parameter.default
        dependency = getattr(default, "dependency", None)
        if dependency:
            dependencies.append(dependency)
    return dependencies


def _workflow_routes():
    """返回 Workflow API 当前注册的所有路由。"""
    return [
        route
        for route in workflow_endpoint.router.routes
        if isinstance(route, APIRoute)
    ]


def test_admin_dependency_allows_canonical_admin():
    """admin 依赖放行激活的 canonical admin 账号。"""
    user = SimpleNamespace(
        name=settings.SUPERUSER, is_active=True, is_superuser=True
    )
    assert get_current_admin(current_user=user) is user
    assert asyncio.run(get_current_admin_async(current_user=user)) is user


@pytest.mark.parametrize(
    "user",
    [
        # 非 canonical admin 用户
        SimpleNamespace(name="alice", is_active=True, is_superuser=True),
        # 非超级用户
        SimpleNamespace(name=settings.SUPERUSER, is_active=True, is_superuser=False),
        # 已停用账号
        SimpleNamespace(name=settings.SUPERUSER, is_active=False, is_superuser=True),
    ],
)
def test_admin_dependency_rejects_non_admin(user):
    """admin 依赖拒绝非 canonical admin、未激活或非超级用户。"""
    with pytest.raises(HTTPException) as sync_exc_info:
        get_current_admin(current_user=user)
    assert sync_exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as async_exc_info:
        asyncio.run(get_current_admin_async(current_user=user))
    assert async_exc_info.value.status_code == 403


def test_admin_dependencies_reuse_current_user_resolution():
    """admin 依赖复用当前用户解析，保留不存在用户的拒绝策略。"""
    assert _declared_dependencies(get_current_admin) == [get_current_user]
    assert _declared_dependencies(get_current_admin_async) == [get_current_user_async]


def test_workflow_routes_require_admin_dependency_not_bare_verify_token():
    """Workflow 路由必须使用 admin 依赖，不能直接裸用 verify_token。"""
    routes = _workflow_routes()
    assert routes

    for route in routes:
        dependencies = _declared_dependencies(route.endpoint)
        assert verify_token not in dependencies, route.path
        if inspect.iscoroutinefunction(route.endpoint):
            assert get_current_admin_async in dependencies, route.path
            assert get_current_admin not in dependencies, route.path
        else:
            assert get_current_admin in dependencies, route.path
            assert get_current_admin_async not in dependencies, route.path
