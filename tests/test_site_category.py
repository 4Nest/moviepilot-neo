"""站点分类混合配置与文本标识的离线回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import app.api.endpoints.site as site_endpoint
from app.schemas.site import SiteCategory


def test_site_category_accepts_text_ids():
    """站点分类响应模型应保留文本标识。"""
    assert SiteCategory(id="电影720p").id == "电影720p"
    assert SiteCategory(id=401).id == 401


def test_site_category_ignores_search_settings_and_invalid_items(monkeypatch):
    """分类接口仅合并字典列表项，搜索参数及非法项不得进入响应。"""
    monkeypatch.setattr(
        site_endpoint.Site,
        "async_get",
        AsyncMock(return_value=SimpleNamespace(domain="example.test")),
    )
    helper = SimpleNamespace(async_get_indexer=AsyncMock(return_value={
        "category": {
            "param": "cat[]",
            "movie": [{"id": "电影720p", "cat": "Movies"}, "invalid"],
            "tv": [{"id": "电影720p", "cat": "Movies"}, {"id": 402, "cat": "TV"}],
        }
    }))
    monkeypatch.setattr(site_endpoint, "SitesHelper", Mock(return_value=helper))

    result = asyncio.run(site_endpoint.site_category(1, db=object(), _=None))

    assert result == [
        {"id": "电影720p", "cat": "Movies"},
        {"id": 402, "cat": "TV"},
    ]
