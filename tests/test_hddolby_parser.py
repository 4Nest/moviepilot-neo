# -*- coding: utf-8 -*-
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app import schemas
from app.api.endpoints import site as site_endpoint
from app.chain import ChainBase
from app.chain.site import SiteChain
from app.db.models.site import Site
from app.db import site_oper as site_oper_module
from app.helper import site as catalog
from app.helper.rss import RssHelper
from app.modules.indexer.parser.hddolby import HDDolbySiteUserInfo
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.modules.indexer.spider.hddolby import HddolbySpider


USER_DATA_JSON = """
{
  "status": 0,
  "data": [{
    "id": "41088",
    "username": "g0m3e",
    "added": "2023-12-18 13:58:19",
    "class": "5",
    "uploaded": "1000",
    "downloaded": "500",
    "seedbonus": "100.0",
    "unread_messages": "1"
  }]
}
"""


def _build_parser(
    site_cookie: str = "",
    url: str = "https://www.uhddolby.com/",
) -> HDDolbySiteUserInfo:
    return HDDolbySiteUserInfo(
        site_name="高清杜比",
        url=url,
        site_cookie=site_cookie,
        apikey="test-api-key",
        token=None,
    )


def test_hddolby_parse_user_base_info_reads_unread_count():
    parser = _build_parser()
    parser._parse_site_page("")
    parser._parse_user_base_info(USER_DATA_JSON)

    assert parser._base_url == "https://api.uhddolby.com"
    assert parser.userid == "41088"
    assert parser.username == "g0m3e"
    assert parser.message_unread == 1


def test_hddolby_skips_message_body_without_cookie():
    parser = _build_parser(site_cookie="")
    parser.message_unread = 1

    parser._pase_unread_msgs()

    assert parser.message_unread_contents == []


def test_hddolby_reads_message_body_via_nexus_when_cookie_present(monkeypatch):
    parser = _build_parser(site_cookie="c_secure_uid=1")
    parser.message_unread = 1

    def fake_pase_unread_msgs(self):
        self.message_unread_contents = [
            ("测试标题", "2026-07-21 10:00:00", "测试正文"),
        ]

    monkeypatch.setattr(NexusPhpSiteUserInfo, "_pase_unread_msgs", fake_pase_unread_msgs)

    parser._pase_unread_msgs()

    assert len(parser.message_unread_contents) == 1
    head, date, content = parser.message_unread_contents[0]
    assert head == "测试标题"
    assert date == "2026-07-21 10:00:00"
    assert content == "测试正文"


def test_uhddolby_directory_inherits_template_and_supports_spider(monkeypatch):
    """uhddolby.com 应出现在站点补充目录中，并复用 hddolby 模板与 API 爬虫。"""
    upstream_template = {
        "id": "hddolby",
        "name": "高清杜比",
        "domain": "https://www.hddolby.com/",
        "category": {"movie": [{"id": 401, "desc": "Movies"}]},
        "subtitles": {"search": "subtitles.php"},
        "cookie": "legacy-cookie-must-not-leak",
    }
    resource = Mock(auth_level=2)
    resource.get_indexsites.return_value = {}
    resource.get_indexer.side_effect = (
        lambda d: upstream_template if d == "hddolby.com" else {}
    )
    resource.async_get_indexer = AsyncMock(
        side_effect=lambda d: upstream_template if d == "hddolby.com" else {}
    )
    resource.get_indexers.return_value = []
    resource.async_get_indexers = AsyncMock(return_value=[])

    saved = Site(
        id=18,
        name="高清杜比",
        domain="uhddolby.com",
        url="https://www.uhddolby.com/",
        apikey="dolby-key",
        cookie="uhd-cookie",
        is_active=True,
        pri=1,
    )
    oper = Mock()
    oper.get_by_domain.return_value = saved
    oper.async_get_by_domain = AsyncMock(return_value=saved)
    oper.list_active.return_value = [saved]
    oper.async_list_active = AsyncMock(return_value=[saved])

    monkeypatch.setattr(catalog, "ResourceSitesHelper", lambda: resource)
    monkeypatch.setattr(catalog, "SiteOper", lambda: oper)

    helper = catalog.SitesHelper()
    assert helper.get_indexsites()["uhddolby.com"]["url"] == "https://www.uhddolby.com/"

    entry = helper.get_indexer("https://www.uhddolby.com/")
    assert entry["parser"] == "HDDolby"
    assert entry["schema"] == "HDDolby"
    assert entry["domain"] == "https://www.uhddolby.com/"
    assert entry["category"] == {"movie": [{"id": 401, "desc": "Movies"}]}
    assert entry["subtitles"] == {"search": "subtitles.php"}
    assert entry["cookie"] == "uhd-cookie"
    assert "alias_of" not in entry
    assert asyncio.run(helper.async_get_indexer("uhddolby.com")) == entry
    assert helper.get_indexers() == [entry]
    assert asyncio.run(helper.async_get_indexers()) == [entry]

    spider = HddolbySpider(entry)
    assert spider._searchurl == "https://api.uhddolby.com/api/v1/torrent/search"
    assert spider._pageurl == "https://www.uhddolby.com/details.php?id=%s&hit=1"


def test_uhddolby_special_site_test_and_rss_map(monkeypatch):
    """uhddolby.com 应注册连接测试器与 RSS 自动获取规则。"""
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    chain = SiteChain()
    assert "uhddolby.com" in chain.special_site_test
    assert chain.special_site_test["uhddolby.com"] == chain.special_site_test["hddolby.com"]
    assert RssHelper.rss_link_conf["uhddolby.com"] == RssHelper.rss_link_conf["hddolby.com"]


def test_update_site_migrates_domain_records(monkeypatch):
    """修改站点域名为 www.uhddolby.com 时，应同步迁移图标、统计与历史用户数据。"""
    existing_site = SimpleNamespace(
        id=18,
        domain="hddolby.com",
        url="https://www.hddolby.com/",
        async_update=AsyncMock(),
    )
    old_icon = SimpleNamespace(id=1, domain="hddolby.com", async_update=AsyncMock())
    old_stat = SimpleNamespace(id=2, domain="hddolby.com", async_update=AsyncMock())
    old_user_day1 = SimpleNamespace(
        id=10, domain="hddolby.com", updated_day="2026-10-23", async_update=AsyncMock()
    )
    old_user_day2 = SimpleNamespace(
        id=11, domain="hddolby.com", updated_day="2026-10-24", async_update=AsyncMock()
    )
    new_user_day2 = SimpleNamespace(
        id=12, domain="uhddolby.com", updated_day="2026-10-24", async_update=AsyncMock()
    )

    monkeypatch.setattr(site_endpoint.Site, "async_get", AsyncMock(return_value=existing_site))
    monkeypatch.setattr(site_endpoint.eventmanager, "async_send_event", AsyncMock())
    monkeypatch.setattr(
        site_oper_module.SiteIcon,
        "async_get_by_domain",
        AsyncMock(side_effect=lambda _db, d: old_icon if d == "hddolby.com" else None),
    )
    monkeypatch.setattr(
        site_oper_module.SiteStatistic,
        "async_get_by_domain",
        AsyncMock(side_effect=lambda _db, d: old_stat if d == "hddolby.com" else None),
    )
    monkeypatch.setattr(
        site_oper_module.SiteUserData,
        "async_get_by_domain",
        AsyncMock(
            side_effect=lambda _db, d: [old_user_day1, old_user_day2]
            if d == "hddolby.com"
            else [new_user_day2]
        ),
    )
    deleted_userdata_ids = []
    monkeypatch.setattr(
        site_oper_module.SiteUserData,
        "async_delete",
        AsyncMock(side_effect=lambda _db, rid: deleted_userdata_ids.append(rid)),
    )

    form = schemas.Site(id=18, name="高清杜比", url="https://www.uhddolby.com/")
    resp = asyncio.run(site_endpoint.update_site(db=Mock(), site_in=form, _=Mock()))

    assert resp.success is True
    existing_site.async_update.assert_awaited_once()
    updated_payload = existing_site.async_update.await_args.args[1]
    assert updated_payload["domain"] == "uhddolby.com"
    assert updated_payload["url"] == "https://www.uhddolby.com/"
    old_icon.async_update.assert_awaited_once_with(
        old_icon.async_update.await_args.args[0], {"domain": "uhddolby.com"}
    )
    old_stat.async_update.assert_awaited_once_with(
        old_stat.async_update.await_args.args[0], {"domain": "uhddolby.com"}
    )
    old_user_day1.async_update.assert_awaited_once_with(
        old_user_day1.async_update.await_args.args[0], {"domain": "uhddolby.com"}
    )
    assert deleted_userdata_ids == [11]
