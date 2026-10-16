"""订阅响应聚合版本更新时间的兼容回归。"""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import schemas
from app.api.endpoints import subscribe as subscribe_api
from app.db.models.subscribe import Subscribe


_OLD_TIME = "2026-10-13 18:00:00"
_NEW_TIME = "2026-10-16 18:00:00"


def _build_subscribe(**overrides) -> Subscribe:
    values = {
        "id": 1, "name": "美人余", "type": "电视剧", "season": 1,
        "total_episode": 28, "state": "R", "date": _NEW_TIME,
        "version_mode": "all",
        "version_rules": [{"id": "main", "name": "默认版本", "settings": {}}],
        "version_progress": {"main": {"last_update": _NEW_TIME}},
    }
    values.update(overrides)
    return Subscribe(**values)


@pytest.mark.parametrize("parent_time", [None, _OLD_TIME])
def test_response_uses_latest_version_time_without_changing_database(parent_time: str | None) -> None:
    """新建和迁移订阅均显示最新版本时间，序列化不能修改数据库原始事实。"""
    parent = _build_subscribe(last_update=parent_time)
    original = deepcopy(parent.version_progress)

    response = schemas.Subscribe.model_validate(parent)

    assert response.last_update == _NEW_TIME
    assert response.model_dump()["last_update"] == _NEW_TIME
    assert parent.last_update == parent_time
    assert parent.version_progress == original
    assert "last_update" not in response.to_public_write_payload()
    assert "version_progress" not in response.to_public_write_payload()


@pytest.mark.parametrize("parent_time, version_time, expected", [
    (_NEW_TIME, _OLD_TIME, _NEW_TIME),
    (_OLD_TIME, None, _OLD_TIME),
    (_OLD_TIME, "", _OLD_TIME),
    (_OLD_TIME, "not-a-date", _OLD_TIME),
    (_OLD_TIME, "2026-99-99 18:00:00", _OLD_TIME),
    (None, "not-a-date", None),
    (None, None, None),
    ("not-a-date", _NEW_TIME, _NEW_TIME),
])
def test_response_time_handles_missing_and_invalid_snapshots(
        parent_time: str | None, version_time: str | None, expected: str | None,
) -> None:
    """保留较新的父时间；缺失或损坏的版本时间不能覆盖有效记录，也不借用创建时间。"""
    parent = _build_subscribe(
        last_update=parent_time, version_progress={"main": {"last_update": version_time}},
        decision_summary={"result": "no_match", "updated_at": _NEW_TIME},
    )
    assert schemas.Subscribe.model_validate(parent).last_update == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_latest_time_includes_existing_versions_even_when_disabled(enabled: bool) -> None:
    """多个版本取最新记录，停用已有版本不能抹去它的历史下载时间。"""
    parent = _build_subscribe(version_progress={
        "main": {"last_update": _OLD_TIME}, "second": {"last_update": _NEW_TIME},
    })
    parent.version_rules.append({"id": "second", "name": "另一版本", "enabled": enabled, "settings": {}})
    assert schemas.Subscribe.model_validate(parent).last_update == _NEW_TIME


@pytest.mark.parametrize("rules", [None, [], [{"id": "main", "name": "默认版本", "settings": {}}]])
def test_response_ignores_deleted_version_snapshots(rules: list | None) -> None:
    """原版单订阅保持父时间；不存在的版本残留快照不参与聚合。"""
    parent = _build_subscribe(
        version_rules=rules, last_update=_OLD_TIME,
        version_progress={"deleted": {"last_update": _NEW_TIME}},
    )
    assert schemas.Subscribe.model_validate(parent).last_update == _OLD_TIME


@pytest.mark.parametrize("path", ["/", "/list", "/1", "/media/tmdb:1", "/history/电视剧"])
def test_read_endpoints_return_latest_version_time(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """列表、详情、媒体查询和历史接口经过真实响应序列化后统一返回最新时间。"""
    parent = _build_subscribe(last_update=_OLD_TIME)
    monkeypatch.setattr(subscribe_api.Subscribe, "async_list", AsyncMock(return_value=[parent]))
    monkeypatch.setattr(subscribe_api.Subscribe, "async_get", AsyncMock(return_value=parent))
    monkeypatch.setattr(subscribe_api, "list_subscribes_by_media_key", AsyncMock(return_value=[parent]))
    monkeypatch.setattr(subscribe_api.SubscribeHistory, "async_list_by_type", AsyncMock(return_value=[parent]))
    app = FastAPI()
    app.include_router(subscribe_api.router, prefix="/api/v1/subscribe")
    app.dependency_overrides[subscribe_api.get_async_db] = lambda: None
    app.dependency_overrides[subscribe_api.get_current_admin_async] = lambda: None
    app.dependency_overrides[subscribe_api.verify_apitoken] = lambda: "test"

    with TestClient(app) as client:
        response = client.get(f"/api/v1/subscribe{path}")

    assert response.status_code == 200
    data = response.json()
    item = data[0] if isinstance(data, list) else data
    assert item["last_update"] == _NEW_TIME
    assert parent.last_update == _OLD_TIME
