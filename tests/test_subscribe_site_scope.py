# -*- coding: utf-8 -*-
"""订阅 RSS 匹配的站点范围与种子缓存隔离。"""
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.types import MediaType
import tests.test_subscribe_chain as subscribe_chain_test

# 以模块属性访问，避免把 TestCase 类导入本模块全局而被 pytest 重复收集
SUBSCRIBE_CHAIN_MODULE = subscribe_chain_test.SUBSCRIBE_CHAIN_MODULE
SubscribeChain = subscribe_chain_test.SubscribeChain

# 站点 1 在全局订阅站点中；站点 3 曾被订阅选中，但后来被移出全局订阅站点
_DOMAINS = {1: "a.example", 3: "c.example"}


class _SystemConfigOper:
    """全局订阅站点只包含站点 1。"""

    def get(self, *args, **kwargs):
        return [1]


class _SiteOper:
    """按站点 ID 返回域名。"""

    def get_domains_by_ids(self, ids):
        return [_DOMAINS[i] for i in ids]


class _TorrentHelper:
    def filter_torrent(self, *args, **kwargs):
        return True


def _build_context(site: int, title: str):
    """构造一个已识别到订阅媒体的缓存种子。"""
    return SimpleNamespace(
        media_info=SimpleNamespace(
            clear=lambda: None, douban_id=None, tmdb_id=1, type=MediaType.TV,
            category=None, episode_group=None,
        ),
        media_recognize_fail_count=0,
        match_source="unknown",
        meta_info=SimpleNamespace(begin_season=1, episode_list=[1], org_string=title, season_list=[1]),
        torrent_info=SimpleNamespace(
            description="", pri_order=100, site=site, site_name=_DOMAINS[site], title=title,
        ),
    )


def _run_match(subscribe, torrents):
    """执行一次 match，返回提交下载的上下文列表。"""
    mediainfo = SimpleNamespace(
        clear=lambda: None, douban_id=None, title_year="Test Show (2026)", tmdb_id=1, type=MediaType.TV,
    )
    downloads = []

    class _SubscribeOper:
        def list(self, *args, **kwargs):
            return [subscribe]

        def get(self, *args, **kwargs):
            return subscribe

    def _download(self, **kwargs):
        downloads.extend(kwargs["contexts"])
        return kwargs["contexts"], {}

    chain = SubscribeChain()
    chain.recognize_media = lambda **kwargs: mediainfo
    chain.check_and_handle_existing_media = lambda **kwargs: (False, {})
    chain.get_params = lambda *_args, **_kwargs: {}
    chain.filter_torrents = lambda **kwargs: kwargs["torrent_list"]
    chain.finish_subscribe_or_not = lambda **_kwargs: None

    with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), \
            patch.object(SUBSCRIBE_CHAIN_MODULE, "SystemConfigOper", _SystemConfigOper), \
            patch.object(SUBSCRIBE_CHAIN_MODULE, "SiteOper", _SiteOper), \
            patch.object(SUBSCRIBE_CHAIN_MODULE, "TorrentHelper", _TorrentHelper), \
            patch.object(SubscribeChain, "_SubscribeChain__download_best_version_with_full_pack_first", _download):
        chain.match(torrents)
    return downloads


def _build_subscribe(**overrides):
    """构造普通（非洗版）剧集订阅。"""
    fields = {
        "best_version": 0, "filter_groups": [], "keyword": None, "media_category": None,
        "save_path": None, "search_imdbid": False, "username": "", "downloader": None,
    }
    fields.update(overrides)
    return subscribe_chain_test.SubscribeChainTest()._build_subscribe(**fields)


def test_get_sub_sites_falls_back_to_global_sites_when_selection_outside_whitelist():
    """订阅所选站点全部不在全局订阅站点中时，回退为全局订阅站点。"""
    with patch.object(SUBSCRIBE_CHAIN_MODULE, "SystemConfigOper", _SystemConfigOper):
        assert SubscribeChain.get_sub_sites(_build_subscribe(sites=[3])) == [1]
        assert SubscribeChain.get_sub_sites(_build_subscribe(sites=[1, 3])) == [1]


def test_match_uses_effective_sites_for_domain_and_site_filters():
    """
    域名过滤与站点ID过滤必须使用同一组有效站点。
    原实现域名按订阅原始站点 [3] 过滤、站点ID按回退后的 [1] 过滤，两者互斥导致永远匹配不到。
    """
    subscribe = _build_subscribe(sites=[3])
    torrents = {
        "a.example": [_build_context(1, "Test Show S01E01 A")],
        "c.example": [_build_context(3, "Test Show S01E01 C")],
    }

    downloads = _run_match(subscribe, torrents)

    assert [ctx.torrent_info.site for ctx in downloads] == [1]


def test_match_does_not_write_subscribe_attributes_into_cached_media_info():
    """订阅的分类与剧集组只写到本次匹配结果上，不污染共享的种子缓存。"""
    subscribe = _build_subscribe(sites=[1], media_category="动漫", episode_group="g1")
    cached = _build_context(1, "Test Show S01E01 A")

    downloads = _run_match(subscribe, {"a.example": [cached]})

    assert len(downloads) == 1
    assert downloads[0].media_info.category == "动漫"
    assert downloads[0].media_info.episode_group == "g1"
    assert cached.media_info.category is None
    assert cached.media_info.episode_group is None
