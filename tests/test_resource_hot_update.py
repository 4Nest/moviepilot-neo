"""NEO 站点资源包热更新、SHA-256 完整性保护及零发版动态 API 站点适配测试。"""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from app.chain import ChainBase
from app.chain.site import SiteChain
from app.core.config import settings
from app.db.models.site import Site
from app.helper import site as catalog
from app.helper.resource import ResourceHelper
from app.helper.rss import RssHelper
from app.helper.site import SiteResourceRegistry
from app.modules.indexer import IndexerModule


class _BytesResp:
    """离线二进制/JSON HTTP 响应替身。"""

    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.reason = "OK" if status_code == 200 else "Error"
        self.ok = status_code < 400
        self.text = content.decode("utf-8", errors="replace")

    def __bool__(self) -> bool:
        return self.status_code < 400


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_resource_helper_hot_loads_new_custom_api_site_without_restart(
    monkeypatch,
    tmp_path: Path,
):
    """无需新增 SiteSchema 枚举或重启服务，即可通过资源包热更上线全新自定义 API 站点。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))

    spider_py = '''from typing import List, Optional, Tuple


class FuturePTSpider:
    """热更新动态测试 Spider"""

    _size = 50

    def __init__(self, indexer: dict):
        self._indexer = indexer

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> int:
        """返回单页容量"""
        return cls._size

    def search(self, keyword: Optional[str] = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """同步搜索"""
        return False, [{
            "title": f"Future.Movie.2026.{keyword or 'browse'}",
            "description": "热更站点测试",
            "enclosure": "https://futurept.example/dl/1",
            "page_url": "https://futurept.example/details/1",
            "size": 1073741824,
            "seeders": 9,
            "peers": 1,
            "downloadvolumefactor": 0.0,
            "uploadvolumefactor": 1.0,
        }]

    async def async_search(self, keyword: Optional[str] = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """异步搜索"""
        return self.search(keyword=keyword, page=page)

    def search_subtitles(self, keyword: Optional[str] = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """同步字幕搜索"""
        return False, [{
            "title": "Future.Movie.chs",
            "enclosure": "https://futurept.example/sub/1",
            "language": "简体中文",
            "size": 4096,
        }]
'''.encode("utf-8")

    parser_py = '''from typing import Optional, Tuple
from app.modules.indexer.parser import SiteParserBase


class FuturePTSiteUserInfo(SiteParserBase):
    """热更新动态用户解析器（直接使用字符串 schema，不依赖宿主枚举）"""

    schema = "FuturePT"
    request_mode = "apikey"

    def _parse_site_page(self, html_text: str) -> None:
        self._user_basic_page = None
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None

    def _parse_logged_in(self, html_text: str) -> bool:
        return True

    def _parse_user_base_info(self, html_text: str) -> None:
        self.userid = "888"
        self.username = "future_tester"
        self.user_level = "VIP"
        self.upload = 2048
        self.download = 1024
        self.ratio = 2.0
        self.bonus = 999.5

    def _parse_user_traffic_info(self, html_text: str) -> None:
        pass

    def _parse_user_detail_info(self, html_text: str) -> None:
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        return None, None, None
'''.encode("utf-8")

    test_py = '''from typing import Tuple


def test_site(site) -> Tuple[bool, str]:
    """热更连通性测试函数"""
    if site.apikey == "valid-future-key":
        return True, "热更连接成功"
    return False, "热更密钥无效"
'''.encode("utf-8")

    neo_sites_bytes = json.dumps(
        {
            "version": "1.2.0",
            "supplements": {
                "futurept.example": {
                    "id": "futurept",
                    "name": "未来视界",
                    "url": "https://futurept.example/",
                    "domain": "https://futurept.example/",
                    "public": False,
                    "parser": "FuturePT",
                    "schema": "FuturePT",
                    "language": "zh",
                    "subtitles": True,
                }
            },
            "apikey_rss_templates": {
                "futurept.example": "rss/feed?key={apikey}"
            },
            "adapters": {
                "FuturePT": {
                    "spider_module": "adapters/spider/futurept.py",
                    "spider_class": "FuturePTSpider",
                    "parser_module": "adapters/parser/futurept.py",
                    "parser_class": "FuturePTSiteUserInfo",
                    "test_module": "adapters/test/futurept.py",
                    "test_func": "test_site",
                    "domains": ["futurept.example"],
                }
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")

    package_payload = {
        "version": "161",
        "resources": {},
        "neo_sites": {
            "version": "1.2.0",
            "files": {
                "resources.v2/neo.sites.json": {
                    "target": "neo.sites.json",
                    "sha256": _sha256(neo_sites_bytes),
                },
                "resources.v2/adapters/spider/futurept.py": {
                    "target": "adapters/spider/futurept.py",
                    "sha256": _sha256(spider_py),
                },
                "resources.v2/adapters/parser/futurept.py": {
                    "target": "adapters/parser/futurept.py",
                    "sha256": _sha256(parser_py),
                },
                "resources.v2/adapters/test/futurept.py": {
                    "target": "adapters/test/futurept.py",
                    "sha256": _sha256(test_py),
                },
            },
        },
    }

    remote_files = {
        "https://raw.githubusercontent.com/4Nest/MoviePilot-Resources/main/package.v2.json": json.dumps(
            package_payload
        ).encode("utf-8"),
        "https://raw.githubusercontent.com/4Nest/MoviePilot-Resources/main/resources.v2/neo.sites.json": neo_sites_bytes,
        "https://raw.githubusercontent.com/4Nest/MoviePilot-Resources/main/resources.v2/adapters/spider/futurept.py": spider_py,
        "https://raw.githubusercontent.com/4Nest/MoviePilot-Resources/main/resources.v2/adapters/parser/futurept.py": parser_py,
        "https://raw.githubusercontent.com/4Nest/MoviePilot-Resources/main/resources.v2/adapters/test/futurept.py": test_py,
    }

    restart_called = []
    monkeypatch.setattr("app.helper.resource.SystemHelper.restart", lambda: restart_called.append(True))
    monkeypatch.setattr(
        "app.helper.resource.RequestUtils.get_res",
        lambda self, url, **_kw: _BytesResp(remote_files[url]),
    )

    ResourceHelper.check_and_update()
    assert restart_called == []
    assert SiteResourceRegistry.current_version() == "1.2.0"

    # 1. 目录与索引配置生效
    resource_mock = Mock(auth_level=2)
    resource_mock.get_indexsites.return_value = {}
    resource_mock.get_indexer.return_value = {}
    resource_mock.get_indexers.return_value = []
    saved_site = Site(
        id=77,
        name="未来视界",
        domain="futurept.example",
        url="https://futurept.example/",
        apikey="valid-future-key",
        is_active=True,
        pri=1,
    )
    oper_mock = Mock()
    oper_mock.get_by_domain.return_value = saved_site
    oper_mock.list_active.return_value = [saved_site]
    monkeypatch.setattr(catalog, "ResourceSitesHelper", lambda: resource_mock)
    monkeypatch.setattr(catalog, "SiteOper", lambda: oper_mock)

    helper = catalog.SitesHelper()
    assert "futurept.example" in helper.get_indexsites()
    runtime_site = helper.get_indexer("futurept.example")
    assert runtime_site["parser"] == "FuturePT"
    assert runtime_site["schema"] == "FuturePT"

    # 2. 动态 Spider 同步/异步搜索与字幕搜索生效
    monkeypatch.setattr("app.modules.indexer.SitesHelper", lambda: Mock(check=lambda _d: (False, "")))
    monkeypatch.setattr("app.modules.indexer.SiteOper.success", lambda self, **_kw: None)
    monkeypatch.setattr("app.modules.indexer.SiteOper.async_success", AsyncMock(return_value=None))

    im = IndexerModule()
    im.init_module()
    assert im.get_search_page_size(runtime_site) == 50
    torrents = im.search_torrents(site=runtime_site, keyword="Dune")
    assert len(torrents) == 1
    assert torrents[0].title == "Future.Movie.2026.Dune"
    async_torrents = asyncio.run(im.async_search_torrents(site=runtime_site, keyword="Matrix"))
    assert async_torrents[0].title == "Future.Movie.2026.Matrix"
    subs = im.search_subtitles(site=runtime_site, keyword="Dune")
    assert len(subs) == 1 and subs[0].language == "简体中文"

    # 3. 动态字符串 schema 用户数据解析器生效
    userdata = im.refresh_userdata(runtime_site)
    assert userdata is not None
    assert userdata.username == "future_tester"
    assert userdata.bonus == 999.5

    # 4. 动态连通性测活与 API Key RSS 模板生效
    monkeypatch.setattr(ChainBase, "__init__", lambda self: None)
    monkeypatch.setattr("app.chain.site.SiteOper", lambda: oper_mock)
    ok, msg = SiteChain().test("https://futurept.example/")
    assert (ok, msg) == (True, "热更连接成功")

    rss_url, rss_err = RssHelper().get_rss_link(
        "https://futurept.example/",
        cookie="",
        ua="",
        apikey="valid-future-key",
    )
    assert rss_err == ""
    assert rss_url == "https://futurept.example/rss/feed?key=valid-future-key"


def test_resource_helper_rejects_tampered_sha256_and_path_traversal(
    monkeypatch,
    tmp_path: Path,
):
    """当远端文件字节被篡改或包含路径穿越时，必须拒绝落盘与拒绝加载。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))

    good_bytes = json.dumps({"version": "1.0.1", "supplements": {}}).encode("utf-8")
    tampered_bytes = json.dumps({"version": "1.0.1", "supplements": {"evil.com": {}}}).encode("utf-8")

    pkg = {
        "version": "161",
        "resources": {},
        "neo_sites": {
            "version": "1.0.1",
            "files": {
                "resources.v2/neo.sites.json": {
                    "target": "neo.sites.json",
                    "sha256": _sha256(good_bytes),
                }
            },
        },
    }
    monkeypatch.setattr(
        "app.helper.resource.RequestUtils.get_res",
        lambda self, url, **_kw: _BytesResp(
            json.dumps(pkg).encode("utf-8") if url.endswith("package.v2.json") else tampered_bytes
        ),
    )

    ResourceHelper.check_and_update()
    assert not SiteResourceRegistry.manifest_path().exists()
    assert SiteResourceRegistry.current_version() is None

    # 路径穿越检查
    assert SiteResourceRegistry._safe_resolve("../escape.py") is None
    assert SiteResourceRegistry._safe_resolve("/etc/passwd") is None


def test_stale_cached_manifest_does_not_downgrade_builtin_sites(
    monkeypatch,
    tmp_path: Path,
):
    """磁盘遗留低于内置基准版本的旧热更缓存时，不得把新版内置站点协议降级回旧配置。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    res_dir = SiteResourceRegistry.resources_dir()
    res_dir.mkdir(parents=True, exist_ok=True)

    stale_sites = json.dumps(
        {
            "version": "1.0.0",
            "supplements": {
                "hdwing.org": {
                    "id": "hdwing",
                    "name": "HDWinG",
                    "url": "https://hdwing.org/",
                    "domain": "https://hdwing.org/",
                    "public": False,
                    "parser": "NexusPhp",
                    "schema": "NexusPhp",
                }
            },
        }
    ).encode("utf-8")
    SiteResourceRegistry.sites_json_path().write_bytes(stale_sites)
    SiteResourceRegistry.manifest_path().write_text(
        json.dumps({"version": "1.0.0", "files": {"neo.sites.json": _sha256(stale_sites)}}),
        encoding="utf-8",
    )
    SiteResourceRegistry.reload()

    assert SiteResourceRegistry.current_version() is None
    assert SiteResourceRegistry.get_supplements()["hdwing.org"]["parser"] == "HDWing"
