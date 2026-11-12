"""验证插件静态资源的浏览器缓存、条件请求与路径边界安全，以及插件评分快照缓存。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from app import schemas
from app.api.endpoints import plugin


@pytest.fixture
def static_files(tmp_path, monkeypatch):
    """在临时目录挂载插件静态文件路由并保留访问校验钩子。"""
    root = tmp_path / "app/plugins/demo/dist"
    root.mkdir(parents=True)
    (root / "remoteEntry.js").write_text("export const version = 1", encoding="utf-8")
    (root / "chunk.js").write_text("export const value = 1", encoding="utf-8")
    manager = Mock()
    manager.get_plugin_ids.return_value = ["Demo"]
    manager.get_plugin_attr.return_value = "1.0.0"
    verify = Mock()
    monkeypatch.setattr(plugin, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugin, "verify_resource_token", verify)
    monkeypatch.setattr(type(plugin.settings), "ROOT_PATH", property(lambda _self: tmp_path))
    app = FastAPI()
    app.include_router(plugin.router, prefix="/plugin")
    return TestClient(app), root, manager, verify


def test_current_version_entry_is_private_and_cacheable(static_files):
    """匹配当前插件版本的入口文件授予长期私有缓存，版本不匹配时每次重新校验。"""
    client, _, manager, _ = static_files
    response = client.get("/plugin/file/demo/dist/remoteEntry.js?v=1.0.0")
    assert response.status_code == 200
    assert response.content == b"export const version = 1"
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["etag"]
    assert response.headers["last-modified"]
    manager.get_plugin_attr.assert_called_once_with("Demo", "plugin_version")

    manager.get_plugin_attr.return_value = "2.0.0"
    assert client.get("/plugin/file/Demo/dist/remoteEntry.js?v=1.0.0").headers["cache-control"] == "private, no-cache"
    assert "immutable" in client.get("/plugin/file/Demo/dist/remoteEntry.js?v=2.0.0").headers["cache-control"]


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
def test_unversioned_chunks_revalidate_with_304(static_files, validator):
    """未带版本号的 chunk 在条件请求命中时仍执行权限校验并返回无响应体 304。"""
    client, _, _, verify = static_files
    url = "/plugin/file/Demo/dist/chunk.js"
    first = client.get(url)
    header = "If-None-Match" if validator == "etag" else "If-Modified-Since"
    response = client.get(url, headers={header: first.headers[validator]})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == "private, no-cache"
    assert verify.call_count == 2

    verify.side_effect = HTTPException(status_code=403, detail="Forbidden")
    assert client.get(url, headers={header: first.headers[validator]}).status_code == 403


def test_changed_file_and_escaped_symlink(static_files, tmp_path):
    """文件更新后返回新内容，越界符号链接与缺失文件保持 403/404。"""
    client, root, _, _ = static_files
    url = "/plugin/file/Demo/dist/chunk.js"
    first = client.get(url)
    (root / "chunk.js").write_text("export const value = 'updated'", encoding="utf-8")
    response = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert response.status_code == 200
    assert response.content == b"export const value = 'updated'"

    outside = tmp_path / "outside.js"
    outside.write_text("private", encoding="utf-8")
    (root / "escaped.js").symlink_to(outside)
    assert client.get("/plugin/file/Demo/dist/escaped.js").status_code == 403
    assert client.get("/plugin/file/Demo/dist/missing.js?v=1.0.0").status_code == 404


def test_plugin_metrics_cache_reuses_snapshot_and_invalidates_on_submit():
    """批量评分与统计结果在五分钟内复用，提交评分成功后立即失效。"""
    async def scenario():
        rating_payload = {
            "plugin_id": "DemoPlugin",
            "average_rating": 4.2,
            "rating_count": 5,
            "user_rating": None,
        }
        updated_payload = {**rating_payload, "average_rating": 4.8, "user_rating": 5.0}
        with (
            patch.object(
                plugin.MoviePilotServerHelper,
                "async_get_plugin_ratings",
                new=AsyncMock(side_effect=[{"DemoPlugin": rating_payload}, {"DemoPlugin": updated_payload}]),
            ) as ratings_mock,
            patch.object(
                plugin.MoviePilotServerHelper,
                "async_get_plugin_statistic",
                new=AsyncMock(return_value={"DemoPlugin": 42}),
            ) as stat_mock,
            patch.object(
                plugin.MoviePilotServerHelper,
                "async_submit_plugin_rating",
                new=AsyncMock(return_value=updated_payload),
            ),
            patch("app.api.endpoints.plugin.SystemConfigOper", return_value=MagicMock(get=lambda _k: ["DemoPlugin"])),
        ):
            r1 = await plugin.plugin_ratings("Other,DemoPlugin", None)
            r2 = await plugin.plugin_ratings("DemoPlugin,Other", None)
            assert r1["DemoPlugin"].average_rating == r2["DemoPlugin"].average_rating == 4.2
            assert ratings_mock.await_count == 1

            assert await plugin.statistic(None) == await plugin.statistic(None) == {"DemoPlugin": 42}
            assert stat_mock.await_count == 1

            await plugin.rate_plugin("DemoPlugin", schemas.PluginRatingRequest(rating=5.0), None)
            r3 = await plugin.plugin_ratings("DemoPlugin,Other", None)
            assert r3["DemoPlugin"].average_rating == 4.8
            assert ratings_mock.await_count == 2

    asyncio.run(scenario())
