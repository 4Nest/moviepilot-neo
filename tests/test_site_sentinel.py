import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from app.api.endpoints.site import read_statistic_by_domain, read_statistics
from app.chain.search import SearchChain
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings
from app.db.models.sitestatistic import SiteStatistic
from app.db.site_oper import SiteOper
from app.helper.rule import RuleHelper
from app.helper.site import SiteHealthSentinel, SitesHelper
from app.modules import indexer as indexer_module
from app.modules.indexer import IndexerModule
from app.schemas import SearchDiagnosticSite, SiteUserData


@pytest.fixture(autouse=True)
def reset_site_health_sentinel():
    """每个用例清理进程级熔断状态，避免测试相互污染。"""
    SiteHealthSentinel.reset_for_tests()
    yield
    SiteHealthSentinel.reset_for_tests()


def test_sentinel_opens_after_three_failures_and_recovers_from_half_open(monkeypatch):
    """连续三次失败应熔断，冷却后只允许半开试探并由成功彻底恢复。"""
    monkeypatch.setattr("app.helper.site.time.time", lambda: 1000.0)

    for _ in range(2):
        SiteHealthSentinel.record_failure("https://WWW.Example.com/path", "Cookie 失效")
    assert SiteHealthSentinel.is_circuit_open("example.com") == (False, None)

    SiteHealthSentinel.record_failure("example.com", "403")
    circuit_open, circuit_until = SiteHealthSentinel.is_circuit_open("example.com")
    assert circuit_open is True
    assert circuit_until is not None
    assert SiteHealthSentinel.get_status("example.com") == {
        "consecutive_fails": 3,
        "circuit_open": True,
        "circuit_until": circuit_until,
        "last_error": "403",
    }

    monkeypatch.setattr("app.helper.site.time.time", lambda: 2801.0)
    assert SiteHealthSentinel.is_circuit_open("example.com") == (False, None)
    half_open = SiteHealthSentinel.get_status("example.com")
    assert half_open["consecutive_fails"] == 2
    assert half_open["circuit_open"] is False
    assert half_open["circuit_until"] is None

    SiteHealthSentinel.record_success("https://example.com")
    assert SiteHealthSentinel.get_status("example.com") == {
        "consecutive_fails": 0,
        "circuit_open": False,
        "circuit_until": None,
        "last_error": None,
    }


def test_sentinel_half_open_failure_reopens_circuit(monkeypatch):
    """半开试探再次失败时应立即重开完整冷却窗口。"""
    monkeypatch.setattr("app.helper.site.time.time", lambda: 1000.0)
    for _ in range(3):
        SiteHealthSentinel.record_failure("example.com")

    monkeypatch.setattr("app.helper.site.time.time", lambda: 2801.0)
    assert SiteHealthSentinel.is_circuit_open("example.com") == (False, None)
    SiteHealthSentinel.record_failure("example.com", "连接超时")
    assert SiteHealthSentinel.is_circuit_open("example.com")[0] is True
    assert SiteHealthSentinel.get_status("example.com")["consecutive_fails"] == 3


def test_site_oper_updates_health_without_polluting_time_notes():
    """同步和异步统计入口都应更新哨兵，note 只能保留数值耗时。"""
    domain = "oper.example.com"
    oper = SiteOper()
    oper.fail(domain)
    oper.fail(domain)
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 2

    oper.success(domain, seconds=2)
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 0
    statistic = SiteStatistic.get_by_domain(oper._db, domain)
    assert statistic.note
    assert all(type(value) is int for value in statistic.note.values())

    async def exercise_async_operations():
        """覆盖异步失败与成功入口。"""
        await oper.async_fail(domain)
        await oper.async_fail(domain)
        await oper.async_success(domain, seconds=3)

    asyncio.run(exercise_async_operations())
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 0
    statistic = SiteStatistic.get_by_domain(oper._db, domain)
    assert all(type(value) is int for value in statistic.note.values())
    assert set(statistic.note) != {"circuit_open", "last_error"}


def test_site_test_counts_one_failure_and_sends_deduplicated_alert(monkeypatch):
    """连通性测试失败不得重复计数，第三次失败应产生一条清晰告警。"""
    domain = "test.example.com"
    site_info = SimpleNamespace(domain=domain, name="测试站点")
    monkeypatch.setattr(SiteOper, "get_by_domain", lambda self, value: site_info)
    monkeypatch.setattr(
        SiteChain,
        "_SiteChain__test",
        staticmethod(lambda _site_info: (False, "连接超时")),
    )
    chain = SiteChain()
    notifications = []
    monkeypatch.setattr(chain, "post_message", notifications.append)

    for _ in range(3):
        assert chain.test(f"https://{domain}")[0] is False

    status = SiteHealthSentinel.get_status(domain)
    assert status["consecutive_fails"] == 3
    assert status["circuit_open"] is True
    assert status["last_error"] == "连接超时"
    assert len(notifications) == 1
    assert notifications[0].title == "【站点健康预警】测试站点"
    assert "熔断" in notifications[0].text
    assert notifications[0].link == settings.MP_DOMAIN("#/site")

    chain.reset_site_health(SimpleNamespace(event_data={"domain": domain}))
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 0
    assert SiteHealthSentinel.get_status(domain)["last_error"] is None


def test_site_test_success_recovers_sentinel(monkeypatch):
    """手动连通性测试成功应清除失败、熔断和告警状态。"""
    domain = "recover.example.com"
    SiteHealthSentinel.record_failure(domain, "旧异常")
    monkeypatch.setattr(
        SiteOper,
        "get_by_domain",
        lambda self, value: SimpleNamespace(domain=domain, name="恢复站点"),
    )
    monkeypatch.setattr(
        SiteChain,
        "_SiteChain__test",
        staticmethod(lambda _site_info: (True, "连接成功")),
    )
    monkeypatch.setattr(SiteOper, "success", lambda self, domain, seconds=None: None)

    assert SiteChain().test(domain) == (True, "连接成功")
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 0
    assert SiteHealthSentinel.get_status(domain)["last_error"] is None


@pytest.mark.parametrize(
    "userdata, expected_reason",
    [
        (None, "未获取到站点用户数据"),
        (SiteUserData(err_msg="APIKEY 已失效"), "APIKEY 已失效"),
    ],
)
def test_refresh_userdata_records_specific_failures(monkeypatch, userdata, expected_reason):
    """用户数据刷新无结果或返回鉴权错误时应记录真实原因并告警。"""
    chain = SiteChain()
    monkeypatch.setattr(chain, "run_module", lambda *_args, **_kwargs: userdata)
    notifications = []
    monkeypatch.setattr(chain, "post_message", notifications.append)
    site = {
        "domain": "userdata.example.com",
        "name": "用户数据站点",
        "public": False,
        "schema": "NexusPhp",
    }

    assert chain.refresh_userdata(site) is None
    status = SiteHealthSentinel.get_status("userdata.example.com")
    assert status["consecutive_fails"] == 1
    assert status["last_error"] == expected_reason
    assert len(notifications) == 1 if userdata is not None else len(notifications) == 0


def test_successful_userdata_refresh_recovers_sentinel(monkeypatch):
    """用户数据刷新成功可作为半开试探并恢复站点。"""
    domain = "userdata-recover.example.com"
    SiteHealthSentinel.record_failure(domain, "旧异常")
    chain = SiteChain()
    monkeypatch.setattr(chain, "run_module", lambda *_args, **_kwargs: SiteUserData(domain=domain))
    monkeypatch.setattr(SiteOper, "update_userdata", lambda self, **_kwargs: None)

    result = chain.refresh_userdata({
        "domain": domain,
        "name": "恢复站点",
        "public": False,
        "schema": "NexusPhp",
    })
    assert SiteHealthSentinel.get_status(domain)["consecutive_fails"] == 0


def test_indexer_circuit_check_precedes_rate_limit_and_reports_outcome(monkeypatch):
    """搜索调度遇熔断时不得进入流控或真实请求，并报告 circuit_open。"""
    domain = "search.example.com"
    for _ in range(3):
        SiteHealthSentinel.record_failure(domain)
    monkeypatch.setattr(
        SitesHelper,
        "check",
        lambda *_args, **_kwargs: pytest.fail("熔断站点不应继续执行流控检查"),
        raising=False,
    )
    reports = []
    monkeypatch.setattr(
        indexer_module,
        "report_site_search_outcome",
        lambda **kwargs: reports.append(kwargs),
    )

    allowed = IndexerModule._IndexerModule__search_check(
        {"domain": f"https://{domain}", "name": "搜索站点", "language": "zh"}
    )
    assert allowed is False
    assert reports == [{"attempted": False, "outcome": "circuit_open"}]


def test_torrents_refresh_skips_circuit_open_site(monkeypatch):
    """订阅刷新应跳过熔断站点，避免继续执行 RSS 请求。"""
    domain = "rss.example.com"
    for _ in range(3):
        SiteHealthSentinel.record_failure(domain)
    monkeypatch.setattr(
        SitesHelper,
        "get_indexers",
        lambda self: [{"id": 1, "domain": domain, "name": "订阅站点"}],
    )
    chain = TorrentsChain()
    monkeypatch.setattr(chain, "get_torrents", lambda: {})
    monkeypatch.setattr(chain, "save_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chain,
        "rss",
        lambda *_args, **_kwargs: pytest.fail("熔断站点不应执行 RSS 抓取"),
    )

    assert chain.refresh(stype="rss", sites=[1]) == {}


def test_search_diagnostics_expose_circuit_open_warning(monkeypatch):
    """搜索诊断应保留熔断来源并追加剧缩警告。"""
    monkeypatch.setattr(RuleHelper, "explain_rule_groups", lambda self, **_kwargs: [])
    diagnostics = SearchChain._build_search_diagnostics(
        observations=[
            SearchDiagnosticSite(
                site_id=1,
                site_name="熔断站点",
                attempted=False,
                outcome="circuit_open",
            )
        ],
        candidates=0,
        retained=0,
        counts=Counter(),
    )

    assert diagnostics.searched_sites[0].outcome == "circuit_open"
    assert [warning.code for warning in diagnostics.warnings] == ["provider_circuit_open"]


def test_statistic_endpoints_merge_runtime_circuit_status(monkeypatch):
    """单站点与批量统计接口均应合并内存中的实时熔断状态。"""
    domain = "api-example.com"
    for _ in range(3):
        SiteHealthSentinel.record_failure(domain, "权限不足")
    database_statistic = SiteStatistic(
        domain=domain,
        success=4,
        fail=3,
        seconds=2,
        lst_state=1,
        note={"2026-01-01 00:00:00": 2},
    )

    async def fake_get_by_domain(_db, value):
        """返回指定域名的统计记录。"""
        assert value == domain
        return database_statistic

    async def fake_list(_db):
        """返回批量统计记录。"""
        return [database_statistic]

    monkeypatch.setattr(SiteStatistic, "async_get_by_domain", staticmethod(fake_get_by_domain))
    monkeypatch.setattr(SiteStatistic, "async_list", staticmethod(fake_list))

    single = asyncio.run(read_statistic_by_domain(domain, None, None))
    multiple = asyncio.run(read_statistics(None, None))
    for statistic in [single, multiple[0]]:
        assert statistic.domain == domain
        assert statistic.consecutive_fails == 3
        assert statistic.circuit_open is True
        assert statistic.circuit_until is not None
        assert statistic.last_error == "权限不足"
        assert statistic.note == {"2026-01-01 00:00:00": 2}


def test_public_bt_and_schemaless_sites_never_trigger_userdata_failure_or_circuit(monkeypatch):
    """公开 BT 站（如 Nyaa、动漫花园）与无 schema 站点没有个人用户数据，刷新时严禁计失败或触发熔断。"""
    nyaa = {"id": 1, "domain": "https://nyaa.si/", "url": "https://nyaa.si/", "name": "Nyaa", "public": True, "is_active": True}
    dmhy = {"id": 2, "domain": "https://dmhy.org/", "url": "https://dmhy.org/", "name": "动漫花园", "public": 1, "schema": "NexusPhp", "is_active": True}
    mikan = {"id": 3, "domain": "https://mikanani.me/", "url": "https://mikanani.me/", "name": "MiKan", "public": False, "schema": None, "is_active": True}
    private_pt = {"id": 4, "domain": "https://private.example.com/", "url": "https://private.example.com/", "name": "私有站", "public": False, "schema": "NexusPhp", "is_active": True}

    chain = SiteChain()
    notifications = []
    refreshed_domains = []
    monkeypatch.setattr(chain, "post_message", notifications.append)
    monkeypatch.setattr(SiteOper, "update_userdata", lambda self, **_kwargs: None)

    def fake_run_module(method, site=None, **_kw):
        assert method == "refresh_userdata"
        if not site or site.get("public") or not site.get("schema"):
            return None
        refreshed_domains.append(site.get("name"))
        return SiteUserData(domain="private.example.com", username="pt_user")
    monkeypatch.setattr(chain, "run_module", fake_run_module)
    monkeypatch.setattr(SitesHelper, "get_indexers", lambda self: [nyaa, dmhy, mikan, private_pt])
    monkeypatch.setattr(SitesHelper, "get_indexer", lambda self, dom: nyaa if "nyaa" in dom else dmhy)

    # 模拟此前已残留误记状态，公开站刷新后应恢复干净且不调用底层解析
    SiteHealthSentinel.record_failure("nyaa.si", "未获取到站点用户数据")
    for _ in range(5):
        assert chain.refresh_userdata(nyaa) is None
        assert chain.refresh_userdata(dmhy) is None
        assert chain.refresh_userdata(mikan) is None
        chain.cache_site_userdata(SimpleNamespace(event_data={"domain": "nyaa.si"}))

    for dom in ("nyaa.si", "dmhy.org", "mikanani.me"):
        st = SiteHealthSentinel.get_status(dom)
        assert st["consecutive_fails"] == 0
        assert st["circuit_open"] is False
        assert st["last_error"] is None
    assert notifications == []
    assert refreshed_domains == []

    # 批量刷新只应对支持用户数据的私有 PT 站点发起请求
    batch = chain.refresh_userdatas()
    assert list(batch.keys()) == ["私有站"]
    assert refreshed_domains == ["私有站"]
