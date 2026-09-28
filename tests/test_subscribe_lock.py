# -*- coding: utf-8 -*-
"""订阅搜索与 RSS 匹配的锁协作。"""
import threading
from unittest.mock import patch

import tests.test_subscribe_chain as subscribe_chain_test

# 以模块属性访问，避免把 TestCase 类导入本模块全局而被 pytest 重复收集
SUBSCRIBE_CHAIN_MODULE = subscribe_chain_test.SUBSCRIBE_CHAIN_MODULE
SubscribeChain = subscribe_chain_test.SubscribeChain


def _build_subscribe(**overrides):
    """构造订阅中状态、创建已超过 1 分钟的剧集订阅。"""
    fields = {"state": "R", "date": None, "best_version": 0, "sites": [], "keyword": None}
    fields.update(overrides)
    return subscribe_chain_test.SubscribeChainTest()._build_subscribe(**fields)


def _lock_free_in_other_thread() -> bool:
    """在另一个线程尝试获取订阅锁，返回是否成功。"""
    result = []

    def _try():
        acquired = SubscribeChain._rlock.acquire(timeout=1)
        result.append(acquired)
        if acquired:
            SubscribeChain._rlock.release()

    worker = threading.Thread(target=_try)
    worker.start()
    worker.join()
    return result[0]


def _run_search(subscribe, latest):
    """
    执行一次定时订阅搜索（state=R 触发随机休眠）。
    休眠期间检查锁是否可被其他线程获取；休眠后数据库返回 latest。
    返回 (休眠期间锁是否空闲, 被识别的订阅列表)。
    """
    lock_free_during_sleep = []
    recognized = []

    class _SubscribeOper:
        def list(self, *args, **kwargs):
            return [subscribe]

        def get(self, *args, **kwargs):
            return latest

        def update(self, *args, **kwargs):
            return None

    def _sleep(_seconds):
        lock_free_during_sleep.append(_lock_free_in_other_thread())

    def _recognize_media(**kwargs):
        recognized.append(kwargs["meta"])
        return None

    chain = SubscribeChain()
    chain.recognize_media = _recognize_media

    with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), \
            patch.object(SUBSCRIBE_CHAIN_MODULE.random, "randint", lambda *_args: 2), \
            patch.object(SUBSCRIBE_CHAIN_MODULE.time, "sleep", _sleep), \
            patch.object(SUBSCRIBE_CHAIN_MODULE, "build_subscribe_meta", lambda sub: sub):
        chain.search(state="R")
    return lock_free_during_sleep, recognized


def test_search_releases_lock_while_sleeping():
    """随机休眠期间释放订阅锁，RSS 匹配无需排队等待整轮搜索结束。"""
    subscribe = _build_subscribe()

    lock_free_during_sleep, recognized = _run_search(subscribe, latest=subscribe)

    assert lock_free_during_sleep and all(lock_free_during_sleep)
    assert recognized == [subscribe]
    # 搜索结束后锁已完全释放
    assert _lock_free_in_other_thread()


def test_search_skips_subscribe_removed_while_sleeping():
    """休眠期间订阅被 RSS 匹配完成删除时，醒来后不再搜索。"""
    lock_free_during_sleep, recognized = _run_search(_build_subscribe(), latest=None)

    assert lock_free_during_sleep
    assert recognized == []
    assert _lock_free_in_other_thread()


def test_search_skips_subscribe_paused_while_sleeping():
    """休眠期间订阅被暂停时，醒来后不再搜索。"""
    _, recognized = _run_search(_build_subscribe(), latest=_build_subscribe(state="S"))

    assert recognized == []


def test_match_skips_run_when_lock_times_out():
    """拿不到订阅锁时跳过本次匹配，而不是无锁并发处理订阅。"""
    listed = []

    class _SubscribeOper:
        def list(self, *args, **kwargs):
            listed.append(True)
            return []

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold_lock():
        with SubscribeChain._rlock:
            holder_ready.set()
            release_holder.wait(5)

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    holder_ready.wait(5)
    try:
        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), \
                patch.object(SubscribeChain, "_LOCK_TIMOUT", 0.05):
            SubscribeChain().match({"a.example": []})
    finally:
        release_holder.set()
        holder.join()

    assert listed == []
    assert _lock_free_in_other_thread()
