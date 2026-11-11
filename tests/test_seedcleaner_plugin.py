"""SeedCleaner 插件规则引擎与执行流程测试。"""
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_seedcleaner_pkg = pytest.importorskip(
    "app.plugins.seedcleaner",
    reason="SeedCleaner 属于独立插件仓库，未安装时跳过插件集成测试",
)
_seedcleaner_engine = pytest.importorskip(
    "app.plugins.seedcleaner.engine",
    reason="SeedCleaner 属于独立插件仓库，未安装时跳过插件集成测试",
)
SeedCleaner = _seedcleaner_pkg.SeedCleaner
CleanEngine = _seedcleaner_engine.CleanEngine
CleanRule = _seedcleaner_engine.CleanRule
NormalizedTorrent = _seedcleaner_engine.NormalizedTorrent


def make_torrent(**overrides: Any) -> NormalizedTorrent:
    """构造默认安全且已完成的种子快照。"""
    values: Dict[str, Any] = {
        "downloader": "QB-主",
        "hash": "abc123",
        "name": "Example.Movie.2025.2160p.WEB-DL-HDArea",
        "save_path": "/downloads/movies",
        "size": 20 * 1024**3,
        "ratio": 2.5,
        "seeding_seconds": 100 * 3600,
        "added_seconds": 120 * 3600,
        "upspeed": 1024,
        "dlspeed": 0,
        "progress": 100.0,
        "state": "seeding",
        "category": "movies",
        "tags": ["已整理"],
        "tracker": "https://tracker.example.com/announce",
        "tracker_msg": "working",
        "is_transferred": True,
    }
    values.update(overrides)
    return NormalizedTorrent(**values)


def make_rule(**overrides: Any) -> CleanRule:
    """构造默认启用且允许删除的清理规则。"""
    values: Dict[str, Any] = {
        "id": "rule-1",
        "name": "测试规则",
        "enabled": True,
        "min_ratio": 2.0,
    }
    values.update(overrides)
    return CleanRule.from_dict(values)


def test_regex_include_and_exclude_conditions() -> None:
    """名称、路径、Tracker、分类和标签正则应同时支持包含与排除。"""
    rule = make_rule(
        min_ratio=None,
        name_regex=r"example\.movie",
        exclude_name_regex=r"-(HDArea|EPiC|WiKi|FRDS)$",
        save_path_regex=r"/downloads/",
        exclude_save_path_regex=r"/protected/",
        tracker_regex=r"example\.com",
        exclude_tracker_regex=r"banned",
        category_regex=r"^movies$",
        tag_regex=r"整理",
        exclude_tag_regex=r"保种",
    )
    engine = CleanEngine({})
    matched, _ = engine.match_rule(make_torrent(), rule)
    assert not matched

    matched, reasons = engine.match_rule(
        make_torrent(name="Example.Movie.2025.2160p.WEB-DL-NTb"), rule
    )
    assert matched
    assert any("名称" in reason for reason in reasons)


def test_invalid_rule_regex_is_reported_and_rule_is_skipped() -> None:
    """非法规则正则不得导致巡检崩溃或误删。"""
    engine = CleanEngine({})
    rule = make_rule(name_regex="[")
    matched, _ = engine.match_rule(make_torrent(), rule)
    assert not matched
    plan = engine.plan({"QB-主": {"torrents": [make_torrent()], "free_space": 10**12}}, [rule])
    assert not plan.candidates
    assert plan.errors
    assert "非法正则" in plan.errors[0]


def test_invalid_global_exclude_regex_fails_closed() -> None:
    """全局排除表达式非法时应保守停止清理。"""
    engine = CleanEngine({"global_exclude_regex": "["})
    plan = engine.plan(
        {"QB-主": {"torrents": [make_torrent()], "free_space": 10**12}},
        [make_rule()],
    )
    assert not plan.candidates
    assert "全局排除" in plan.errors[0]


@pytest.mark.parametrize(
    ("metric_logic", "ratio", "seeding_seconds", "expected"),
    [
        ("all", 3.0, 100 * 3600, True),
        ("all", 3.0, 10 * 3600, False),
        ("any", 3.0, 10 * 3600, True),
        ("any", 1.0, 100 * 3600, True),
        ("any", 1.0, 10 * 3600, False),
    ],
)
def test_metric_logic_supports_and_or(
    metric_logic: str, ratio: float, seeding_seconds: int, expected: bool
) -> None:
    """数值达标条件应支持 AND 与 OR 两种组合。"""
    rule = make_rule(
        metric_logic=metric_logic,
        min_ratio=2.0,
        min_seeding_hours=72,
    )
    matched, _ = CleanEngine({}).match_rule(
        make_torrent(ratio=ratio, seeding_seconds=seeding_seconds), rule
    )
    assert matched is expected


def test_transferred_lock_and_protected_tags() -> None:
    """默认仅删除已整理种子，并始终保留全局保护标签。"""
    engine = CleanEngine({"protect_tags": ["保种", "辅种", "勿删", "keep"]})
    rule = make_rule()
    assert not engine.match_rule(make_torrent(is_transferred=False, tags=[]), rule)[0]
    assert not engine.match_rule(make_torrent(tags=["KEEP", "已整理"]), rule)[0]

    unsafe_rule = make_rule(require_transferred=False)
    assert engine.match_rule(make_torrent(is_transferred=False, tags=[]), unsafe_rule)[0]


def test_incomplete_torrent_requires_explicit_opt_in() -> None:
    """未完成任务默认受保护，红种规则可显式允许清理。"""
    torrent = make_torrent(progress=72.0, state="error", is_transferred=False, tags=[])
    assert not CleanEngine({}).match_rule(torrent, make_rule(require_transferred=False))[0]
    red_rule = make_rule(
        require_transferred=False,
        allow_incomplete=True,
        states=["error"],
        min_ratio=None,
        tracker_msg_regex="unregistered|不存在",
    )
    torrent.tracker_msg = "Unregistered torrent"
    assert CleanEngine({}).match_rule(torrent, red_rule)[0]


def test_disk_guard_stops_at_target_and_sorts_candidates() -> None:
    """低水位淘汰应按策略排序，并在预计恢复目标空间后停止。"""
    engine = CleanEngine({})
    torrents = [
        make_torrent(hash="new", name="new", size=30 * 1024**3, seeding_seconds=20 * 3600),
        make_torrent(hash="old", name="old", size=20 * 1024**3, seeding_seconds=90 * 3600),
        make_torrent(hash="mid", name="mid", size=25 * 1024**3, seeding_seconds=50 * 3600),
    ]
    guard = {
        "enabled": True,
        "free_space_threshold_gb": 50,
        "target_free_space_gb": 80,
        "min_protect_hours": 24,
        "sort_by": "oldest_seeding",
    }
    selected = engine.select_disk_guard(torrents, 40 * 1024**3, guard)
    assert [item.hash for item in selected] == ["old", "mid"]
    plan = engine.plan(
        {"QB-主": {"torrents": torrents, "free_space": 40 * 1024**3}},
        [],
        disk_guard=guard,
    )
    assert [item.hash for item in plan.candidates] == ["old", "mid"]
    assert all(item.rule_id == "__disk_guard__" for item in plan.candidates)


def test_disk_guard_respects_safety_and_protect_duration() -> None:
    """应急删种不得绕过完成、入库、保护标签和最低做种时长。"""
    engine = CleanEngine({})
    protected = make_torrent(hash="protected", tags=["保种"])
    young = make_torrent(hash="young", seeding_seconds=2 * 3600)
    eligible = make_torrent(hash="eligible")
    selected = engine.select_disk_guard(
        [protected, young, eligible],
        1 * 1024**3,
        {
            "enabled": True,
            "free_space_threshold_gb": 50,
            "target_free_space_gb": 60,
            "min_protect_hours": 24,
        },
    )
    assert [item.hash for item in selected] == ["eligible"]


def test_rule_order_limit_and_dry_run() -> None:
    """规则应按顺序匹配、执行单轮上限，干跑不得调用删除器。"""
    first = make_rule(id="first", name="首轮", max_delete_per_run=1, name_regex="Movie")
    second = make_rule(id="second", name="次轮", min_ratio=None, name_regex="Movie")
    torrents = [
        make_torrent(hash="one", name="Movie.One-NTb"),
        make_torrent(hash="two", name="Movie.Two-NTb"),
    ]
    engine = CleanEngine({})
    plan = engine.plan({"QB-主": {"torrents": torrents, "free_space": 10**12}}, [first, second])
    assert [item.hash for item in plan.candidates] == ["one", "two"]
    assert plan.candidates[0].rule_id == "first"
    assert plan.candidates[1].rule_id == "second"
    calls: List[Any] = []
    result = engine.execute(plan, lambda *args: calls.append(args) or True, dry_run=True)
    assert result["deleted"] == []
    assert calls == []


def test_execute_groups_real_deletions_and_reports_failures() -> None:
    """真删应传递源文件删除选项，并保留每个种子的执行结果。"""
    rule = make_rule(delete_files=False)
    plan = CleanEngine({}).plan(
        {"QB-主": {"torrents": [make_torrent()], "free_space": 10**12}}, [rule]
    )
    calls: List[Any] = []

    def delete(downloader: str, torrent_hash: str, delete_files: bool) -> bool:
        """记录伪删除调用。"""
        calls.append((downloader, torrent_hash, delete_files))
        return True

    result = CleanEngine({}).execute(plan, delete)
    assert calls == [("QB-主", "abc123", False)]
    assert result["deleted"][0]["hash"] == "abc123"
    assert not result["failed"]


def test_qbittorrent_and_transmission_normalization() -> None:
    """两类下载器字段应归一化为相同快照模型。"""
    engine = CleanEngine({})
    qb = engine.normalize_torrent(
        {
            "hash": "QBHASH",
            "name": "QB",
            "save_path": "/qb",
            "size": 1024,
            "ratio": 1.2,
            "seeding_time": 7200,
            "added_on": 1_000,
            "upspeed": 10,
            "dlspeed": 20,
            "progress": 1,
            "state": "stalledUP",
            "category": "tv",
            "tags": "已整理, 保种",
            "tracker": "https://tracker.qb/announce",
        },
        "QB-主",
        now=1_000,
    )
    tr = engine.normalize_torrent(
        SimpleNamespace(
            hashString="TRHASH",
            name="TR",
            downloadDir="/tr",
            totalSize=2048,
            uploadRatio=0.8,
            doneDate=500,
            addedDate=100,
            rateUpload=30,
            rateDownload=40,
            percentDone=1,
            status="seed_pending",
            labels=["已整理"],
            trackerStats=[
                SimpleNamespace(host="https://tracker.tr/announce", lastAnnounceResult="Success")
            ],
            errorString="",
        ),
        "TR-备",
        now=1_000,
    )
    assert qb.hash == "qbhash"
    assert qb.progress == 100
    assert qb.state == "stalled"
    assert qb.is_transferred
    assert tr.hash == "trhash"
    assert tr.seeding_seconds == 500
    assert tr.added_seconds == 900
    assert tr.state == "paused"
    assert tr.tracker == "https://tracker.tr/announce"


class FakeDownloader:
    """提供插件集成测试所需的伪下载器。"""

    def __init__(self, torrents: List[dict]) -> None:
        """保存伪种子和删除调用。"""
        self.torrents = torrents
        self.delete_calls: List[Any] = []

    def get_torrents(self) -> tuple:
        """返回无错误的伪种子列表。"""
        return self.torrents, False

    def get_transfer_info(self) -> dict:
        """返回充足的伪磁盘空间。"""
        return {"free_space": 500 * 1024**3}

    def delete_torrents(self, delete_file: bool, ids: str) -> bool:
        """记录伪删除并成功返回。"""
        self.delete_calls.append((delete_file, ids))
        return True


def make_plugin(monkeypatch: pytest.MonkeyPatch, downloader: FakeDownloader) -> SeedCleaner:
    """构造隔离持久化与通知的插件实例。"""
    monkeypatch.setattr(
        "app.plugins.seedcleaner.DownloaderHelper.get_services",
        lambda self: {
            "QB-主": SimpleNamespace(name="QB-主", type="qbittorrent", instance=downloader)
        },
    )
    plugin = SeedCleaner()
    monkeypatch.setattr(plugin, "update_config", lambda config: True)
    saved: Dict[str, Any] = {"history": []}
    monkeypatch.setattr(plugin, "get_data", lambda key=None: saved.get(key))
    monkeypatch.setattr(plugin, "save_data", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(plugin, "post_message", lambda **kwargs: None)
    plugin.init_plugin(
        {
            "enabled": True,
            "notify": True,
            "rules": [
                {
                    "id": "safe",
                    "name": "安全达标",
                    "enabled": True,
                    "min_ratio": 2,
                }
            ],
        }
    )
    return plugin


def test_plugin_dry_run_and_real_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件级干跑应只预览，立即执行才删除并保存历史。"""
    downloader = FakeDownloader(
        [
            {
                "hash": "ABC",
                "name": "Movie-NTb",
                "save_path": "/downloads",
                "size": 1024**3,
                "ratio": 3,
                "seeding_time": 100000,
                "progress": 1,
                "state": "uploading",
                "tags": "已整理",
                "tracker": "tracker.example",
            }
        ]
    )
    plugin = make_plugin(monkeypatch, downloader)
    preview = plugin.run_cleanup(dry_run=True)
    assert preview["success"]
    assert preview["dry_run"]
    assert len(preview["candidates"]) == 1
    assert downloader.delete_calls == []

    result = plugin.run_cleanup()
    assert result["success"]
    assert len(result["deleted"]) == 1
    assert downloader.delete_calls == [(True, "abc")]
    history = plugin.api_history()
    assert history.success
    assert len(history.data) == 1
    cleared = plugin.api_clear_history()
    assert cleared.success
    assert plugin.api_history().data == []


def test_plugin_zero_free_space_triggers_disk_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载器明确返回零空间时必须触发保盘，而非误判为空间未知。"""
    downloader = FakeDownloader(
        [
            {
                "hash": "FULL",
                "name": "Movie-NTb",
                "save_path": "/downloads",
                "size": 2 * 1024**3,
                "ratio": 3,
                "seeding_time": 100 * 3600,
                "progress": 1,
                "state": "uploading",
                "tags": "已整理",
                "tracker": "tracker.example",
            }
        ]
    )
    plugin = make_plugin(monkeypatch, downloader)
    monkeypatch.setattr(downloader, "get_transfer_info", lambda: {"free_space": 0})
    plugin.init_plugin(
        {
            "enabled": True,
            "rules": [],
            "disk_guard": {
                "enabled": True,
                "free_space_threshold_gb": 50,
                "target_free_space_gb": 1,
                "min_protect_hours": 24,
            },
        }
    )
    result = plugin.run_cleanup(dry_run=True)
    assert result["success"]
    assert [item["hash"] for item in result["candidates"]] == ["full"]
    assert downloader.delete_calls == []
