"""
站点 UA/请求头字段裁剪、非法请求头诊断以及多级域名保留的回归测试。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app import schemas
from app.api.endpoints import site as site_api
from app.chain import site as site_chain_module
from app.chain.site import SiteChain
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.utils import http as http_module
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.site import find_invalid_http_header_names, is_valid_http_header_value


def test_http_header_value_validation_rejects_whitespace_controls_and_non_latin1() -> None:
    """HTTP 请求头纯规则校验应拒绝首尾空白、控制字符及非 Latin-1 文本，并仅返回出错字段名。"""
    assert is_valid_http_header_value("") is True
    assert is_valid_http_header_value("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") is True
    assert is_valid_http_header_value(" Mozilla/5.0") is False
    assert is_valid_http_header_value("Mozilla/5.0 ") is False
    assert is_valid_http_header_value("Mozilla/5.0\nBad") is False
    assert is_valid_http_header_value("Mozilla/5.0\x00Bad") is False
    assert is_valid_http_header_value("Mozilla/5.0 中文") is False

    assert find_invalid_http_header_names(None) == []
    assert find_invalid_http_header_names({}) == []
    assert find_invalid_http_header_names(
        {
            "User-Agent": " Mozilla/5.0",
            "Accept": "text/html",
            "X-Empty": None,
            "X-Bad-Type": 123,
        }
    ) == ["User-Agent", "X-Bad-Type"]


def test_request_utils_trims_user_agent_for_sync_and_async_clients() -> None:
    """同步与异步客户端构造时应裁剪 UA 首尾空白，兼容历史脏数据。"""
    sync_client = RequestUtils(ua=" Mozilla/5.0 Edg/158.0.0.0 ")
    async_client = AsyncRequestUtils(ua=" Mozilla/5.0 Edg/158.0.0.0 ")

    assert sync_client._headers["User-Agent"] == "Mozilla/5.0 Edg/158.0.0.0"
    assert async_client._headers["User-Agent"] == "Mozilla/5.0 Edg/158.0.0.0"


def test_request_utils_logs_invalid_header_field_without_leaking_values(monkeypatch) -> None:
    """请求头触发 InvalidHeader 时必须记录异常类型和出错字段，且不泄露凭据值或查询参数。"""
    warnings: list[str] = []
    monkeypatch.setattr(http_module.logger, "warning", warnings.append)

    session = requests.Session()
    result = RequestUtils(
        session=session,
        headers={
            "User-Agent": " Mozilla/5.0 Edg/158.0.0.0",
            "Authorization": "Bearer super-secret\nvalue",
        },
    ).get_res("https://site.example/index.php?passkey=secret-passkey")

    assert result is None
    assert len(warnings) == 1
    assert "InvalidHeader" in warnings[0]
    assert "User-Agent" in warnings[0]
    assert "Authorization" in warnings[0]
    assert "https://site.example" in warnings[0]
    assert "super-secret" not in warnings[0]
    assert "secret-passkey" not in warnings[0]


def test_request_utils_invalid_header_raises_without_duplicate_warning(monkeypatch) -> None:
    """调用方显式要求抛出异常时应传播 InvalidHeader，不额外重复告警。"""
    warnings: list[str] = []
    monkeypatch.setattr(http_module.logger, "warning", warnings.append)

    session = requests.Session()
    with pytest.raises(requests.exceptions.InvalidHeader):
        RequestUtils(
            session=session,
            headers={"User-Agent": " Mozilla/5.0"},
        ).get_res("https://site.example/", raise_exception=True)

    assert warnings == []


def test_site_connectivity_trims_legacy_whitespace_ua_and_falls_back_when_blank(monkeypatch) -> None:
    """历史配置中带首尾空格或全空白的站点 UA 在连通性测试时应自动裁剪或回退默认 UA。"""
    captured_uas: list[str] = []

    class _FakeRequestUtils:
        def __init__(self, **kwargs):
            captured_uas.append(kwargs.get("ua"))

        def get_res(self, url: str):
            return SimpleNamespace(status_code=200, text="ok", reason="OK")

    monkeypatch.setattr(site_chain_module, "RequestUtils", _FakeRequestUtils)

    padded_site = SimpleNamespace(
        name="空格UA站",
        url="https://padded.test/",
        cookie="",
        ua=" Mozilla/5.0 Edg/158.0.0.0 ",
        render=0,
        public=1,
        proxy=0,
        timeout=30,
    )
    blank_site = SimpleNamespace(
        name="空白UA站",
        url="https://blank.test/",
        cookie="",
        ua="   ",
        render=0,
        public=1,
        proxy=0,
        timeout=30,
    )

    assert SiteChain._SiteChain__test(padded_site)[0] is True
    assert SiteChain._SiteChain__test(blank_site)[0] is True
    assert captured_uas == ["Mozilla/5.0 Edg/158.0.0.0", settings.USER_AGENT]


@pytest.mark.parametrize("bad_ua", ["Mozilla/5.0\nInjected", "Mozilla/5.0 中文"])
def test_site_connectivity_reports_invalid_ua_instead_of_unreachable(
    monkeypatch: pytest.MonkeyPatch, bad_ua: str,
) -> None:
    """UA 含非法字符时应明确提示配置错误并告警，而不是误报无法打开网站。"""
    warnings: list[str] = []
    monkeypatch.setattr(site_chain_module.logger, "warning", warnings.append)
    called = False

    class _UnexpectedRequestUtils:
        def __init__(self, **_kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(site_chain_module, "RequestUtils", _UnexpectedRequestUtils)
    site = SimpleNamespace(
        name="非法UA站",
        url="https://bad-ua.test/",
        cookie="",
        ua=bad_ua,
        render=0,
        public=1,
        proxy=0,
        timeout=30,
    )

    result = SiteChain._SiteChain__test(site)

    assert result == (False, "站点浏览器标识(UA)包含非法字符，请检查配置！")
    assert called is False
    assert len(warnings) == 1
    assert "User-Agent" in warnings[0]


def test_site_oper_update_cookie_trims_whitespace(monkeypatch) -> None:
    """SiteOper.update_cookie 应裁剪 Cookie 首尾空白。"""
    updated_payload = {}

    class _FakeSite:
        def update(self, _db, payload):
            updated_payload.update(payload)

    monkeypatch.setattr("app.db.site_oper.Site.get_by_domain", lambda _db, _domain: _FakeSite())
    oper = SiteOper(db=Mock())

    success, _msg = oper.update_cookie("demo.example", "  sid=1; uid=2 \n")

    assert success is True
    assert updated_payload == {"cookie": "sid=1; uid=2"}


def test_site_parser_trims_ua_and_reports_invalid_header(monkeypatch) -> None:
    """站点解析器应裁剪历史 UA 首尾空格；若仍含非法字符，应明确告警、保留错误说明并禁止回退。"""
    trimmed_parser = NexusPhpSiteUserInfo(
        site_name="憨憨",
        url="https://hhan.test/",
        site_cookie="sid=1",
        apikey=None,
        token=None,
        ua=" Mozilla/5.0 Edg/158.0.0.0 ",
    )
    assert trimmed_parser._ua == "Mozilla/5.0 Edg/158.0.0.0"

    assert IndexerModule._site_user_agent({"ua": " Mozilla/5.0 ", "parser": ""}) == "Mozilla/5.0"

    warnings: list[str] = []
    monkeypatch.setattr("app.modules.indexer.parser.logger.warning", warnings.append)
    monkeypatch.setattr(IndexerModule, "_site_schemas", [NexusPhpSiteUserInfo])
    module = object.__new__(IndexerModule)
    result = module.refresh_userdata(
        {
            "name": "憨憨",
            "url": "https://hhan.test/",
            "cookie": "sid=1",
            "ua": "Mozilla/5.0\nBadUA",
            "schema": SiteSchema.NexusPhp.value,
            "public": False,
        }
    )

    assert result is not None
    assert result.userid is None
    assert result.err_msg == "请求头包含非法字符（User-Agent），请检查站点 UA 或认证配置"
    assert any("InvalidHeader" in warning and "User-Agent" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_site_api_trims_fields_and_preserves_multilevel_domains(monkeypatch) -> None:
    """站点新增/更新接口应裁剪请求头/地址字段首尾空白，并保持 u2.dmhy.org 与 dmhy.org 独立域名。"""
    created_payloads: list[dict] = []
    updated_payloads: list[dict] = []

    sites_helper = SimpleNamespace(
        auth_level=2,
        async_get_indexer=AsyncMock(return_value={"name": "测试站点", "public": False}),
    )
    monkeypatch.setattr(site_api, "SitesHelper", lambda: sites_helper)
    monkeypatch.setattr(site_api.eventmanager, "async_send_event", AsyncMock())

    class _FakeSiteModel:
        def __init__(self, **kwargs):
            self._payload = dict(kwargs)

        def create(self, _db):
            created_payloads.append(self._payload)

        @staticmethod
        async def async_get_by_domain(_db, _domain):
            return None

        @staticmethod
        async def async_get(_db, _sid):
            return SimpleNamespace(
                id=_sid,
                domain="old.example",
                url="https://old.example/",
                cookie="",
                apikey="",
                token="",
                ua="",
                proxy=0,
                render=0,
                is_active=True,
                async_update=AsyncMock(side_effect=lambda _d, p: updated_payloads.append(dict(p))),
            )

    monkeypatch.setattr(site_api, "Site", _FakeSiteModel)
    monkeypatch.setattr(
        site_api,
        "SiteOper",
        lambda _db: SimpleNamespace(async_migrate_domain=AsyncMock()),
    )

    res_u2 = await site_api.add_site(
        db=Mock(),
        site_in=schemas.Site(
            url=" https://u2.dmhy.org/torrents.php ",
            ua=" Mozilla/5.0 Edg/158.0.0.0 ",
            cookie=" sid=1; uid=2 ",
            rss=" https://u2.dmhy.org/rss ",
            apikey=" secret-key ",
            token=" secret-token ",
            filter=" 1080p ",
        ),
        _=Mock(),
    )
    res_dmhy = await site_api.add_site(
        db=Mock(),
        site_in=schemas.Site(url=" https://share.dmhy.org/ "),
        _=Mock(),
    )
    assert res_u2.success is True
    assert res_dmhy.success is True

    assert [p["domain"] for p in created_payloads] == ["u2.dmhy.org", "dmhy.org"]
    assert created_payloads[0]["url"] == "https://u2.dmhy.org/"
    assert created_payloads[0]["ua"] == "Mozilla/5.0 Edg/158.0.0.0"
    assert created_payloads[0]["cookie"] == "sid=1; uid=2"
    assert created_payloads[0]["rss"] == "https://u2.dmhy.org/rss"
    assert created_payloads[0]["apikey"] == "secret-key"
    assert created_payloads[0]["token"] == "secret-token"
    assert created_payloads[0]["filter"] == " 1080p "

    upd_u2 = await site_api.update_site(
        db=Mock(),
        site_in=schemas.Site(
            id=1,
            name="U2",
            url=" https://u2.dmhy.org/ ",
            ua=" Mozilla/5.0 ",
            cookie=" sid=9 ",
        ),
        _=Mock(),
    )
    upd_dmhy = await site_api.update_site(
        db=Mock(),
        site_in=schemas.Site(
            id=2,
            name="动漫花园",
            url=" https://share.dmhy.org/ ",
        ),
        _=Mock(),
    )
    assert upd_u2.success is True
    assert upd_dmhy.success is True
    assert [p["domain"] for p in updated_payloads] == ["u2.dmhy.org", "dmhy.org"]
    assert updated_payloads[0]["ua"] == "Mozilla/5.0"
    assert updated_payloads[0]["cookie"] == "sid=9"
