"""RSS 工作流和站点订阅的描述传递回归测试。"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain import torrents as torrents_module
from app.chain.torrents import TorrentsChain
from app.core.context import MediaInfo, TorrentInfo
from app.helper.message import TemplateContextBuilder, TemplateHelper
from app.schemas import ActionContext
from app.workflow.actions import fetch_rss as rss_module
from app.workflow.actions.fetch_rss import FetchRssAction


@pytest.fixture
def rss_items():
    """构造名称和描述已分开的 RSS 条目。"""
    return [{
        "title": "Beyond Time's Gaze S01E43 2025 2160p WEB-DL H265 AAC-ADWeb",
        "description": "国创连载 光阴之外 第43集 [玄机科技出品]",
        "enclosure": "https://example.com/example.torrent",
        "link": "https://example.com/details",
        "size": 1024,
        "pubdate": datetime(2026, 5, 19, 8, 30, 0),
    }]


@pytest.mark.parametrize("match_media", [False, True])
def test_execute_preserves_rss_description_for_recognition_and_notification(monkeypatch, rss_items, match_media):
    """工作流使用 core 种子对象并传递描述，识别和通知均可读取副标题。"""
    rss = Mock()
    rss.parse.return_value = rss_items
    media = Mock()
    media.recognize_by_meta.return_value = MediaInfo(title="光阴之外", year="2025")
    monkeypatch.setattr(rss_module, "RssHelper", lambda: rss)
    monkeypatch.setattr(rss_module, "MediaChain", lambda: media)
    monkeypatch.setattr(rss_module.global_vars, "is_workflow_stopped", lambda _: False)
    context = FetchRssAction("fetch-rss").execute(
        workflow_id=1, params={"url": "https://example.com/rss.xml", "match_media": match_media},
        context=ActionContext(),
    )
    torrent_info = context.torrents[0].torrent_info
    assert isinstance(torrent_info, TorrentInfo)
    assert torrent_info.category is None
    assert callable(torrent_info.to_dict)
    assert torrent_info.pubdate == "2026-05-19 08:30:00"
    assert torrent_info.description == rss_items[0]["description"]
    assert context.torrents[0].meta_info.subtitle == rss_items[0]["description"]
    if match_media:
        assert media.recognize_by_meta.call_args.args[0].subtitle == rss_items[0]["description"]
    template_context = TemplateContextBuilder().build(torrentinfo=torrent_info)
    rendered = TemplateHelper.render_with_context(
        "名称：{{ torrent_title }}\n描述：{{ description }}", template_context,
    )
    assert rendered == f"名称：{rss_items[0]['title']}\n描述：{rss_items[0]['description']}"


def test_site_rss_preserves_description(monkeypatch, rss_items):
    """站点 RSS 刷新与工作流应一致地保留独立描述。"""
    rss = Mock()
    rss.parse.return_value = rss_items
    site = {"id": 1, "name": "测试站点", "rss": "https://example.com/rss.xml"}
    monkeypatch.setattr(torrents_module, "RssHelper", lambda: rss)
    monkeypatch.setattr(torrents_module, "SitesHelper", lambda: SimpleNamespace(get_indexer=lambda _: site))
    torrents = object.__new__(TorrentsChain).rss("example.com")
    assert len(torrents) == 1
    assert torrents[0].title == "Beyond Time's Gaze S01E43 2025 2160p WEB-DL H265 AAC-ADWeb"
    assert torrents[0].description == "国创连载 光阴之外 第43集 [玄机科技出品]"
    assert torrents[0].site_name == "测试站点"
