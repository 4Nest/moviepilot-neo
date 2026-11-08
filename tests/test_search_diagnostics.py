import asyncio
from collections import Counter
from types import SimpleNamespace

from app.api.endpoints import search as search_endpoint
from app.chain import search as search_module
from app.chain.search import SearchChain
from app.core.config import ConfigModel
from app.core.context import MediaInfo, TorrentInfo
from app.helper.rule import RuleHelper
from app.helper.torrent import TorrentHelper
from app.modules import indexer as indexer_module
from app.modules.indexer import IndexerModule
from app.schemas import (
    FilterRuleGroup,
    SearchDiagnosticSite,
    SearchDiagnostics,
    capture_site_search_observation,
)
from app.schemas.types import MediaType, SystemConfigKey


def _make_chain() -> SearchChain:
    """构造不初始化外部模块的搜索链，并隔离缓存与搜索参数。"""

    async def async_noop(*_args, **_kwargs):
        """异步空操作桩。"""

    chain = object.__new__(SearchChain)
    chain.save_last_search_params = lambda **_kwargs: None
    chain.async_save_last_search_params = async_noop
    chain.save_cache = lambda _cache, _filename: None
    chain.async_save_cache = async_noop
    return chain

def _patch_default_rule_groups(monkeypatch, rule_groups=None) -> None:
    """固定标题搜索使用的默认规则组，避免依赖数据库配置。"""

    oper = SimpleNamespace(
        get=lambda key: (rule_groups or []) if key == SystemConfigKey.SearchFilterRuleGroups else None
    )
    monkeypatch.setattr(search_module, "SystemConfigOper", lambda: oper)


def test_explain_rule_groups_reports_effective_mismatch_and_missing(monkeypatch):
    """规则说明应区分有效、媒体范围不符和配置缺失，并保留选择顺序。"""

    helper = RuleHelper()
    groups = [
        FilterRuleGroup(name="all"),
        FilterRuleGroup(name="movies", media_type=MediaType.MOVIE.value),
        FilterRuleGroup(name="anime", media_type=MediaType.MOVIE.value, category="动画"),
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    media = MediaInfo(title="测试剧集", type=MediaType.TV, category="剧集")

    result = helper.explain_rule_groups(
        media=media,
        group_names=["all", "movies", "anime", "missing", "all"],
    )

    assert [rule.name for rule in result] == ["all", "movies", "anime", "missing"]
    assert [rule.status for rule in result] == ["effective", "skipped", "skipped", "skipped"]
    assert [rule.reason for rule in result] == [
        None,
        "rule_group_not_applicable",
        "rule_group_not_applicable",
        "rule_group_missing",
    ]
    assert result[2].categories == ["动画"]


def test_parse_result_counts_each_rejection_stage(monkeypatch):
    """匹配管线应分别记录缺标题、季集、身份和重复资源的排除数量。"""

    chain = _make_chain()
    media = MediaInfo(title="Demo", names=["Demo"], type=MediaType.TV, tmdb_id=1)
    torrents = [
        TorrentInfo(site=1, site_name="站点", title=None, description=""),
        TorrentInfo(site=1, site_name="站点", title="Scope", description=""),
        TorrentInfo(site=1, site_name="站点", title="Other", description=""),
        TorrentInfo(site=1, site_name="站点", title="Demo A", description="same"),
        TorrentInfo(site=1, site_name="站点", title="Demo A", description="same"),
    ]
    monkeypatch.setattr(
        TorrentHelper,
        "match_season_episodes",
        staticmethod(lambda torrent, meta, season_episodes: torrent.title != "Scope"),
    )
    monkeypatch.setattr(
        TorrentHelper,
        "match_torrent",
        staticmethod(lambda mediainfo, torrent_meta, torrent: torrent.title.startswith("Demo")),
    )
    counts = Counter()
    rejection_details = []
    rejected_items = []

    contexts = chain._SearchChain__parse_result(
        torrents=torrents,
        mediainfo=media,
        season_episodes={1: []},
        rule_groups=[],
        diagnostics=counts,
        rejection_details=rejection_details,
        rejected_items=rejected_items,
    )

    assert len(contexts) == 1
    assert counts == Counter({
        "title_missing": 1,
        "scope_mismatch": 1,
        "identity_mismatch": 1,
        "duplicate": 1,
    })
    assert [
        (item["context"]["torrent_info"]["title"], [reason["code"] for reason in item["reasons"]])
        for item in rejected_items
    ] == [
        (None, ["title_missing"]),
        ("Scope", ["scope_mismatch"]),
        ("Other", ["identity_mismatch"]),
        ("Demo A", ["duplicate"]),
    ]
    assert all(
        "torrent" not in reason
        for item in rejected_items
        for reason in item["reasons"]
    )


def test_parse_result_collects_filter_rejections_before_early_return(monkeypatch):
    """过滤规则清空候选时仍应保留过滤参数与规则排除项，并从原因中剥离内部 torrent。"""

    chain = _make_chain()
    media = MediaInfo(title="Demo", names=["Demo"], type=MediaType.MOVIE, tmdb_id=1)
    param_drop = TorrentInfo(site=1, site_name="站点", title="Demo 720p", description="")
    rule_drop = TorrentInfo(site=1, site_name="站点", title="Demo 1080p REMUX", description="")
    counts = Counter()
    rejection_details = []
    rejected_items = []

    def filter_torrents(**kwargs):
        """模拟规则组过滤掉剩余候选并附带内部种子引用。"""
        for torrent in kwargs["torrent_list"]:
            kwargs["rejection_details"].append({
                "stage": "filter_rules",
                "code": "negated_rule_matched",
                "rule_group": "default",
                "rule": "REMUX",
                "torrent": torrent,
            })
        return []

    chain.filter_torrents = filter_torrents
    monkeypatch.setattr(
        TorrentHelper,
        "match_torrent",
        staticmethod(lambda mediainfo, torrent_meta, torrent: True),
    )

    contexts = chain._SearchChain__parse_result(
        torrents=[param_drop, rule_drop],
        mediainfo=media,
        rule_groups=["default"],
        filter_params={"include": "1080p"},
        diagnostics=counts,
        rejection_details=rejection_details,
        rejected_items=rejected_items,
    )

    assert contexts == []
    assert counts == Counter({"filter_params": 1, "filter_rules": 1})
    assert [
        (item["context"]["torrent_info"]["title"], item["reasons"])
        for item in rejected_items
    ] == [
        ("Demo 720p", [{
            "code": "param_include_missed",
            "count": 1,
            "rule_group": None,
            "rule": None,
            "expected": "1080p",
            "actual": None,
        }]),
        ("Demo 1080p REMUX", [{
            "code": "negated_rule_matched",
            "count": 1,
            "rule_group": "default",
            "rule": "REMUX",
            "expected": None,
            "actual": None,
        }]),
    ]


def test_title_stream_done_contains_complete_diagnostics(monkeypatch):
    """标题搜索完成事件应包含候选、保留、规则拒绝和站点状态。"""

    chain = _make_chain()
    keep = TorrentInfo(site=1, site_name="站点", title="Demo 1080p", description="")
    drop = TorrentInfo(site=1, site_name="站点", title="Demo REMUX", description="")

    async def search_stream(**_kwargs):
        """模拟一个站点返回两条候选。"""

        yield {
            "type": "append",
            "items": [keep, drop],
            "site": "站点",
            "site_id": 1,
        }

    chain._SearchChain__async_search_all_sites_stream = search_stream

    cache = {}

    async def save_cache(value, filename):
        """记录标题搜索流写入的本地缓存。"""
        cache[filename] = value

    async def load_cache(filename):
        """读取标题搜索流写入的本地缓存。"""
        return cache.get(filename)

    chain.async_save_cache = save_cache
    chain.async_load_cache = load_cache

    def filter_torrents(**kwargs):
        """模拟排除一条资源并报告未满足的包含条件。"""
        kwargs["rejection_details"].append({
            "stage": "filter_rules",
            "code": "include_missed",
            "rule_group": "default",
            "rule": "1080P",
            "expected": "1080p",
            "torrent": drop,
        })
        return [keep]

    chain.filter_torrents = filter_torrents
    _patch_default_rule_groups(monkeypatch, ["default"])
    monkeypatch.setattr(
        search_module.RuleHelper,
        "explain_rule_groups",
        lambda self, media, group_names: [],
    )

    events = asyncio.run(_collect(chain.async_search_by_title_stream(title="Demo", cache_local=True)))
    diagnostics = events[-1]["diagnostics"]
    rejected_items = events[-1]["rejected_items"]

    assert events[-1]["type"] == "done"
    assert diagnostics["version"] == 1
    assert diagnostics["scope"] == "search"
    assert diagnostics["stage_counts"] == {"candidates": 2, "retained": 1}
    assert diagnostics["rejection_reasons"] == [{
        "code": "filter_rules",
        "count": 1,
        "details": [{
            "code": "include_missed",
            "count": 1,
            "rule_group": "default",
            "rule": "1080P",
            "expected": "1080p",
            "actual": None,
        }],
    }]
    assert len(rejected_items) == 1
    assert rejected_items[0]["context"]["torrent_info"]["title"] == "Demo REMUX"
    assert rejected_items[0]["reasons"] == [{
        "code": "include_missed",
        "count": 1,
        "rule_group": "default",
        "rule": "1080P",
        "expected": "1080p",
        "actual": None,
    }]
    assert asyncio.run(chain.async_last_rejected_search_results()) == rejected_items
    assert diagnostics["searched_sites"][0]["site_name"] == "站点"
    assert diagnostics["rule_groups"] == []
    assert diagnostics["warnings"] == []

def test_precise_stream_done_distinguishes_cooldown_and_error(monkeypatch):
    """精确搜索应分别保留冷却与失败站点，并生成对应来源警告。"""

    chain = _make_chain()
    media = MediaInfo(title="Demo", names=["Demo"], type=MediaType.MOVIE, tmdb_id=1)

    async def search_stream(**_kwargs):
        """模拟冷却站点和请求失败站点。"""

        yield {
            "type": "append",
            "items": [],
            "site_diagnostic": SearchDiagnosticSite(
                site_id=1,
                site_name="冷却站点",
                attempted=False,
                outcome="cooldown",
            ),
        }
        yield {
            "type": "append",
            "items": [],
            "site_diagnostic": SearchDiagnosticSite(
                site_id=2,
                site_name="失败站点",
                attempted=True,
                outcome="error",
            ),
        }

    chain._SearchChain__async_search_all_sites_stream = search_stream
    _patch_default_rule_groups(monkeypatch)

    events = asyncio.run(_collect(chain.async_process_stream(mediainfo=media)))
    diagnostics = events[-1]["diagnostics"]

    assert events[-1]["type"] == "done"
    assert events[-1]["rejected_items"] == []
    assert diagnostics["stage_counts"] == {"candidates": 0, "retained": 0}
    assert [(site["site_name"], site["outcome"], site["attempted"]) for site in diagnostics["searched_sites"]] == [
        ("冷却站点", "cooldown", False),
        ("失败站点", "error", True),
    ]
    assert [warning["code"] for warning in diagnostics["warnings"]] == [
        "provider_cooldown",
        "provider_error",
    ]


def test_indexer_reports_cooldown_and_exception_outcomes(monkeypatch):
    """索引模块应把流控跳过与搜索异常分别报告为冷却和错误。"""

    module = object.__new__(IndexerModule)
    module._IndexerModule__indexer_statistic = lambda **_kwargs: None
    module._IndexerModule__parse_result = lambda **_kwargs: []
    monkeypatch.setattr(
        indexer_module,
        "SitesHelper",
        lambda: SimpleNamespace(check=lambda _domain: (True, "冷却中")),
    )
    with capture_site_search_observation() as cooldown:
        assert module.search_torrents(site={"name": "冷却站点", "domain": "cool.test"}) == []
    assert (cooldown.reported, cooldown.attempted, cooldown.outcome) == (True, False, "cooldown")

    class FailingSpider:
        """模拟搜索入口抛出异常的专站爬虫。"""

        def __init__(self, _site):
            """保存测试站点配置。"""

        @staticmethod
        def matches(_site):
            """匹配当前测试站点。"""
            return True

        def search(self, **_kwargs):
            """模拟请求异常。"""
            raise RuntimeError("request failed")

    monkeypatch.setattr(
        indexer_module,
        "SitesHelper",
        lambda: SimpleNamespace(check=lambda _domain: (False, "")),
    )
    monkeypatch.setattr(indexer_module, "AniBTSpider", FailingSpider)
    with capture_site_search_observation() as error:
        assert module.search_torrents(site={"name": "失败站点", "domain": "error.test"}) == []
    assert (error.reported, error.attempted, error.outcome) == (True, True, "error")


def test_latest_context_returns_diagnostics_and_hides_subtitle_diagnostics(monkeypatch):
    """上次资源搜索上下文应返回诊断与被过滤资源，字幕上下文不得泄漏资源数据。"""

    diagnostics = SearchDiagnostics(search_id="search-1").model_dump(mode="json")
    rejected_results = [{
        "context": {"torrent_info": {"title": "Demo 720p"}},
        "reasons": [{"code": "include_missed", "count": 1}],
    }]

    class FakeSearchChain:
        """提供已缓存的资源结果、被过滤资源和诊断。"""

        result_type = "torrent"

        async def async_last_search_params(self):
            """返回当前结果类型。"""
            return {"keyword": "Demo", "result_type": self.result_type}

        async def async_last_search_results(self):
            """返回空资源结果。"""
            return []

        async def async_last_subtitle_search_results(self):
            """返回空字幕结果。"""
            return []

        async def async_last_search_diagnostics(self):
            """返回最近资源诊断。"""
            return diagnostics

        async def async_last_rejected_search_results(self):
            """返回最近被过滤资源列表。"""
            return rejected_results

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    response = asyncio.run(search_endpoint.search_latest_context(_=None))
    assert response.data["diagnostics"] == diagnostics
    assert response.data["rejected_results"] == rejected_results

    FakeSearchChain.result_type = "subtitle"
    response = asyncio.run(search_endpoint.search_latest_context(_=None))
    assert response.data["diagnostics"] is None
    assert response.data["rejected_results"] == []


def test_id_stream_forwards_and_caches_rejected_items(monkeypatch):
    """精确搜索流应原样转发被过滤资源，并在开启本地缓存时持久化。"""

    chain = _make_chain()
    cache = {}
    media = MediaInfo(title="Demo", names=["Demo"], type=MediaType.MOVIE, tmdb_id=1)
    rejected_items = [{
        "context": {"torrent_info": {"title": "Demo 720p"}},
        "reasons": [{"code": "include_missed", "count": 1}],
    }]
    diagnostics = SearchDiagnostics(search_id="id-stream").model_dump(mode="json")

    async def save_cache(value, filename):
        """记录精确搜索流写入的本地缓存。"""
        cache[filename] = value

    async def load_cache(filename):
        """读取精确搜索流写入的本地缓存。"""
        return cache.get(filename)

    async def recognize_media(**_kwargs):
        """返回已识别媒体信息。"""
        return media

    async def process_stream(**_kwargs):
        """模拟内部精确搜索流输出完成事件。"""
        yield {
            "type": "done",
            "items": [],
            "total_items": 0,
            "contexts": [],
            "diagnostics": diagnostics,
            "rejected_items": rejected_items,
        }

    chain.async_save_cache = save_cache
    chain.async_load_cache = load_cache
    chain.async_recognize_media = recognize_media
    chain.async_process_stream = process_stream

    events = asyncio.run(_collect(chain.async_search_by_id_stream(tmdbid=1, mtype=MediaType.MOVIE, cache_local=True)))

    assert events[-1]["rejected_items"] == rejected_items
    assert "contexts" not in events[-1]
    assert asyncio.run(chain.async_last_rejected_search_results()) == rejected_items


def test_new_torrent_or_subtitle_search_clears_cached_diagnostics():
    """保存新一轮资源或字幕搜索参数时应同步清空旧诊断与被过滤资源缓存。"""

    chain = _make_chain()
    chain.save_last_search_params = SearchChain.save_last_search_params.__get__(chain)
    chain.async_save_last_search_params = SearchChain.async_save_last_search_params.__get__(chain)
    cache = {}
    diagnostics = SearchDiagnostics(search_id="old-search")
    rejected_items = [{"context": {"torrent_info": {"title": "Old"}}, "reasons": []}]

    def save_sync_cache(value, filename):
        """记录同步缓存写入。"""
        cache[filename] = value

    async def save_cache(value, filename):
        """使用内存字典记录缓存写入。"""
        cache[filename] = value

    async def load_cache(filename):
        """从内存字典读取缓存。"""
        return cache.get(filename)

    chain.save_cache = save_sync_cache
    chain.async_save_cache = save_cache
    chain.async_load_cache = load_cache

    asyncio.run(chain.async_save_last_search_diagnostics(diagnostics))
    asyncio.run(chain.async_save_cache(rejected_items, chain._SearchChain__rejected_result_temp_file))
    assert asyncio.run(chain.async_last_search_diagnostics())["search_id"] == "old-search"
    assert asyncio.run(chain.async_last_rejected_search_results()) == rejected_items

    asyncio.run(chain.async_save_last_search_params(keyword="Demo", result_type="torrent"))
    assert asyncio.run(chain.async_last_search_diagnostics()) is None
    assert asyncio.run(chain.async_last_rejected_search_results()) == []

    asyncio.run(chain.async_save_last_search_diagnostics(diagnostics))
    asyncio.run(chain.async_save_cache(rejected_items, chain._SearchChain__rejected_result_temp_file))
    chain.save_last_search_params(keyword="Demo", result_type="subtitle")
    assert asyncio.run(chain.async_last_search_diagnostics()) is None
    assert asyncio.run(chain.async_last_rejected_search_results()) == []


def test_search_resource_pages_defaults_to_three(monkeypatch):
    """搜索页数默认应为 3 页，并在配置异常时回退为 3 页。"""

    assert ConfigModel().SEARCH_RESOURCE_PAGES == 3
    monkeypatch.setattr(search_module.settings, "SEARCH_RESOURCE_PAGES", 3)
    assert SearchChain._build_search_pages(0) == [0, 1, 2]

    monkeypatch.setattr(search_module.settings, "SEARCH_RESOURCE_PAGES", "invalid")
    assert SearchChain._build_search_pages(0) == [0, 1, 2]


async def _collect(stream):

    return [event async for event in stream]
