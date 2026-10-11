"""订阅进度重算与后台下载并发时的数据完整性回归测试。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, RLock, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import schemas
from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain, build_subscribe_version_view
from tests.test_subscribe_version_progress import _build_subscribe, _media_info, _missing


@pytest.fixture
def concurrent_progress(monkeypatch):
    """隔离订阅状态，并让每次数据库读取都返回独立快照。"""
    chain = object.__new__(SubscribeChain)
    chain._rlock = RLock()
    parent = _build_subscribe()
    saved = {"subscribe": deepcopy(parent)}
    oper = Mock()
    oper.get.side_effect = lambda _sid: deepcopy(saved["subscribe"])

    def update(_sid, fields):
        for field, value in fields.items():
            setattr(saved["subscribe"], field, deepcopy(value))

    oper.update.side_effect = update
    monkeypatch.setattr(subscribe_module, "SubscribeOper", lambda: oper)
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=_media_info()))
    downloader = Mock()
    downloader.get_no_exists_info.side_effect = lambda **_kwargs: (False, _missing(range(26, 38)))
    monkeypatch.setattr(subscribe_module, "DownloadChain", lambda: downloader)
    return SimpleNamespace(chain=chain, parent=parent, saved=saved, oper=oper)


def test_recompute_waits_for_download_and_reads_latest_snapshot(concurrent_progress):
    """重算须等下载释放锁后重读，保留等待期间新增的第 27 集记录。"""
    state = concurrent_progress
    attempted = Event()
    download_lock = state.chain._rlock

    def acquire(**kwargs):
        attempted.set()
        return download_lock.acquire(**kwargs)

    state.chain._rlock = Mock(acquire=acquire, release=download_lock.release)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with download_lock:
            future = executor.submit(state.chain.refresh_subscribe_progress, state.parent)
            assert attempted.wait(5)
            state.oper.get.assert_not_called()
            state.chain.recognize_media.assert_not_called()
            state.saved["subscribe"].version_progress["adweb"]["note"] = [26, 27]
            state.saved["subscribe"].version_progress["adweb"]["lack_episode"] = 10
            state.saved["subscribe"].version_progress["adweb"]["episode_priority"] = {"26": 80, "27": 100}
        summary = future.result(timeout=5)

    assert summary["updated"] is True
    assert state.saved["subscribe"].version_progress["adweb"]["note"] == [26, 27]
    assert state.saved["subscribe"].version_progress["adweb"]["lack_episode"] == 10
    assert state.saved["subscribe"].version_progress["adweb"]["episode_priority"] == {"26": 80, "27": 100}
    assert state.parent.version_progress == state.saved["subscribe"].version_progress
    state.oper.get.assert_called_once_with(state.parent.id)


def test_download_cannot_write_during_recompute(concurrent_progress, monkeypatch):
    """媒体识别和持久化期间都持有订阅锁，下载只在重算完成后写入。"""
    state = concurrent_progress
    recognition_started = Event()
    resume_recognition = Event()
    download_attempted = Event()
    download_finished = Event()
    immediate_acquisitions = []

    def recognize(**_kwargs):
        recognition_started.set()
        assert resume_recognition.wait(5)
        return _media_info()

    def download():
        acquired = state.chain._rlock.acquire(blocking=False)
        immediate_acquisitions.append(acquired)
        if acquired:
            state.chain._rlock.release()
        download_attempted.set()
        with state.chain._rlock:
            state.saved["subscribe"].version_progress["adweb"]["note"] = [26, 27]
        download_finished.set()

    monkeypatch.setattr(state.chain, "recognize_media", recognize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recompute = executor.submit(state.chain.refresh_subscribe_progress, state.parent)
        try:
            assert recognition_started.wait(5)
            pending_download = executor.submit(download)
            assert download_attempted.wait(5)
            assert immediate_acquisitions == [False]
            assert not download_finished.is_set()
        finally:
            resume_recognition.set()
        assert recompute.result(timeout=5)["updated"] is True
        pending_download.result(timeout=5)

    assert state.saved["subscribe"].version_progress["adweb"]["note"] == [26, 27]


def test_recompute_skips_deleted_subscribe(concurrent_progress):
    """入口获取后订阅已被删除时，重算不得使用旧快照写库。"""
    state = concurrent_progress
    state.saved["subscribe"] = None

    summary = state.chain.refresh_subscribe_progress(state.parent)

    assert summary["reason"] == "subscribe_not_found"
    assert summary["updated"] is False
    state.oper.update.assert_not_called()
    state.chain.recognize_media.assert_not_called()


def test_recompute_skips_removed_version(concurrent_progress):
    """版本在排队期间被删除时，不得重新写入已删除的版本进度。"""
    state = concurrent_progress
    view = build_subscribe_version_view(state.parent, state.parent.version_rules[0])
    state.saved["subscribe"].version_rules = []

    summary = state.chain.refresh_subscribe_progress(view)

    assert summary["reason"] == "version_not_found"
    assert summary["updated"] is False
    state.oper.update.assert_not_called()


def test_recompute_lock_timeout_does_not_read_or_write(concurrent_progress):
    """获取订阅锁失败时直接返回，不无锁继续重算或释放他人的锁。"""
    state = concurrent_progress
    state.chain._rlock = Mock()
    state.chain._rlock.acquire.return_value = False

    summary = state.chain.refresh_subscribe_progress(state.parent)

    assert summary["reason"] == "lock_timeout"
    assert summary["updated"] is False
    state.oper.get.assert_not_called()
    state.oper.update.assert_not_called()
    state.chain._rlock.release.assert_not_called()


@pytest.mark.parametrize("recognize_success", [False, True])
def test_backfill_keeps_written_facts_when_reloading(concurrent_progress, monkeypatch, recognize_success):
    """补写入口先保存事实，重算重读后仍保留这些事实，识别失败也不撤回。"""
    state = concurrent_progress
    parent = _build_subscribe(version_rules=[], version_progress={}, note=[26])
    state.saved["subscribe"] = deepcopy(parent)
    if not recognize_success:
        monkeypatch.setattr(state.chain, "recognize_media", Mock(return_value=None))

    summary = state.chain.backfill_existing_episodes(parent, [27])

    assert summary["accepted"] == [27]
    assert summary["updated"] is True
    assert state.saved["subscribe"].note == [26, 27]
    assert summary["progress"]["updated"] is recognize_success
    if recognize_success:
        assert state.saved["subscribe"].lack_episode == 10


@pytest.mark.parametrize("direct_version", [False, True])
def test_recompute_growing_total_preserves_new_library_episodes(concurrent_progress, direct_version):
    """重算发现总数从 25 增至 37 时，库内第 26 集不能被旧目标范围截断。"""
    state = concurrent_progress
    parent = _build_subscribe(total_episode=25, lack_episode=25, last_update="2026-10-01 10:00:00")
    parent.version_rules[0]["settings"].update({"total_episode": 25, "manual_total_episode": 0})
    state.saved["subscribe"] = deepcopy(parent)
    downloader = subscribe_module.DownloadChain()
    downloader.get_no_exists_info.side_effect = lambda **_kwargs: (False, _missing(range(27, 38)))
    target = build_subscribe_version_view(parent, parent.version_rules[0]) if direct_version else parent

    summary = state.chain.refresh_subscribe_progress(target, scene="manual_recompute")

    assert summary["updated"] is True
    assert "total_episode" in summary["fields"]
    assert parent.total_episode == state.saved["subscribe"].total_episode == 37
    assert parent.version_progress["adweb"]["known_episodes"] == list(range(1, 27))
    assert parent.version_progress["adweb"]["lack_episode"] == 11
    assert parent.version_progress["adweb"]["note"] == [26]
    assert schemas.Subscribe.model_validate(parent).completed_episode == 26
    assert state.saved["subscribe"].lack_episode == 25
    assert state.saved["subscribe"].last_update == "2026-10-01 10:00:00"
    next_view = build_subscribe_version_view(state.saved["subscribe"], parent.version_rules[0])
    assert next_view.total_episode == 37
    assert state.chain.compute_lack_episode(next_view) == 11
    if direct_version:
        assert state.chain.compute_lack_episode(target) == 11
    assert all(set(call.args[1]) <= {"total_episode", "version_progress"}
               for call in state.oper.update.call_args_list)


@pytest.mark.parametrize("failure", ["recognize", "resolve_missing"])
def test_recompute_failed_query_keeps_original_total(concurrent_progress, monkeypatch, failure):
    """新总集数只有查库成功后才生效，失败时完整保留总数与上次版本快照。"""
    state = concurrent_progress
    parent = _build_subscribe(total_episode=25, lack_episode=25)
    parent.version_rules[0]["settings"]["total_episode"] = 25
    state.saved["subscribe"] = deepcopy(parent)
    progress = deepcopy(parent.version_progress)
    if failure == "recognize":
        monkeypatch.setattr(state.chain, "recognize_media", Mock(return_value=None))
    else:
        monkeypatch.setattr(state.chain, "resolve_subscribe_missing", Mock(return_value=(False, {})))

    summary = state.chain.refresh_subscribe_progress(parent, scene="manual_recompute")

    assert summary["updated"] is False
    assert parent.total_episode == state.saved["subscribe"].total_episode == 25
    assert parent.version_progress == state.saved["subscribe"].version_progress == progress
    state.oper.update.assert_not_called()


def test_recompute_manual_version_does_not_change_parent_total(concurrent_progress):
    """手动指定 25 集的版本保持独立范围，不能改写父订阅的总集数。"""
    state = concurrent_progress
    parent = _build_subscribe(total_episode=30)
    parent.version_rules[0]["settings"].update({"total_episode": 25, "manual_total_episode": 1})
    state.saved["subscribe"] = deepcopy(parent)

    summary = state.chain.refresh_subscribe_progress(parent, scene="manual_recompute")

    assert summary["updated"] is True
    assert "total_episode" not in summary["fields"]
    assert parent.total_episode == state.saved["subscribe"].total_episode == 30
    assert parent.version_progress["adweb"]["known_episodes"] == list(range(1, 26))
    assert all("total_episode" not in call.args[1] for call in state.oper.update.call_args_list)
    assert build_subscribe_version_view(parent, parent.version_rules[0]).total_episode == 25


def test_recompute_endpoint_runs_blocking_chain_outside_event_loop(monkeypatch):
    """等待订阅锁与同步媒体查询在工作线程执行，不能阻塞异步接口事件循环。"""
    from app.api.endpoints import subscribe as endpoint

    parent = _build_subscribe()
    main_thread = get_ident()
    worker_threads = []

    def refresh(subscribe, *, scene):
        assert subscribe is parent
        assert scene == "manual_recompute"
        worker_threads.append(get_ident())
        return {"reason": "updated", "updated": True, "fields": ["version_progress"]}

    monkeypatch.setattr(endpoint.Subscribe, "async_get", AsyncMock(return_value=parent))
    monkeypatch.setattr(endpoint, "SubscribeChain", lambda: SimpleNamespace(refresh_subscribe_progress=refresh))

    response = asyncio.run(endpoint.recompute_subscribe_progress(parent.id, db=None, current_user=None))

    assert response.success is True
    assert worker_threads and worker_threads[0] != main_thread


@pytest.mark.parametrize("reason", [
    "lock_timeout", "subscribe_not_found", "version_not_found", "recognize_failed", "resolve_missing_failed",
])
def test_recompute_endpoint_reports_failures(monkeypatch, reason):
    """锁超时、订阅删除或查库失败均不能返回重算成功。"""
    from app.api.endpoints import subscribe as endpoint

    parent = _build_subscribe()
    chain = Mock()
    chain.refresh_subscribe_progress.return_value = {"reason": reason, "updated": False, "fields": []}
    monkeypatch.setattr(endpoint.Subscribe, "async_get", AsyncMock(return_value=parent))
    monkeypatch.setattr(endpoint, "SubscribeChain", lambda: chain)

    response = asyncio.run(endpoint.recompute_subscribe_progress(parent.id, db=None, current_user=None))

    assert response.success is False
    assert response.data["reason"] == reason
