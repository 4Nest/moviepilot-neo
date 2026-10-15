"""用真实 TMDB 响应离线复放繁体别名搜索和篇章季号识别。"""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.schemas.types import MediaType
from app.utils.zhconv import convert


TITLE = (
    "[黒ネズミたち] 新网球王子 U-17 WORLD CUP 世界杯决赛成员决定战 / "
    "Tenipuri: U-17 Kesshou Member Ketteisen - 03 (Baha 1920x1080 AVC AAC MP4)"
)
CN_NAME = "新网球王子 U-17 WORLD CUP 世界杯决赛成员决定战"
EN_NAME = "Tenipuri: U-17 Kesshou Member Ketteisen"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/tmdb_tenipuri.json").read_text())


@pytest.fixture
def replay_module(monkeypatch):
    """在 HTTP 边界回放实际响应，缓存使用每条测试独立的内存。"""
    queries = []
    unexpected = []

    def replay(url):
        parts = urlsplit(url)
        query = parse_qs(parts.query).get("query", [None])[0]
        if parts.path == "/3/search/tv" and query in FIXTURE["searches"]:
            queries.append(query)
            payload = FIXTURE["searches"][query]
        elif parts.path == "/3/tv/205493":
            payload = FIXTURE["details"]
        else:
            unexpected.append((parts.path, query))
            raise AssertionError(f"未录制请求：{parts.path}, {query}")
        return {TMDb._RESPONSE_SNAPSHOT_MARKER: True, "headers": {}, "json": deepcopy(payload)}

    def request(_self, _method, url, *_args, **_kwargs):
        return replay(url)

    async def async_request(_self, _method, url, *_args, **_kwargs):
        return replay(url)

    monkeypatch.setattr(TMDb, "request", request)
    monkeypatch.setattr(TMDb, "async_request", async_request)
    module = TheMovieDbModule()
    module.tmdb = TmdbApi(language="zh-CN")
    module.category = SimpleNamespace(get_tv_category=lambda _info: "日番")
    entries = {}

    def cache_key(meta):
        return meta.name, meta.begin_season

    def update(meta, info):
        if info:
            entries[cache_key(meta)] = {
                "id": info["id"], "type": info["media_type"], "title": info["name"]
            }
        elif info is not None:
            entries[cache_key(meta)] = {"id": 0}

    module.cache = SimpleNamespace(
        get=lambda meta: entries.get(cache_key(meta), {}), update=update
    )
    yield module, queries
    module.tmdb.close()
    assert not unexpected


@pytest.mark.parametrize("python_only", [False, True])
def test_original_title_keeps_names_and_episode(monkeypatch, python_only: bool) -> None:
    """Rust 和 Python 均保留完整别名，U-17 不作为季号或集号。"""
    if python_only:
        monkeypatch.setattr("app.core.metainfo.rust_accel.parse_metainfo", lambda *_a, **_kw: None)
    meta = MetaInfo(TITLE, custom_words=["#"])
    assert (meta.cn_name, meta.en_name) == (CN_NAME, EN_NAME)
    assert meta.begin_season is None
    assert meta.begin_episode == 3
    assert meta.resource_pix == "1080p"


def test_search_names_add_traditional_after_existing_names() -> None:
    """原名和英文名保持查询优先级，繁体仅作去重后的回退。"""
    meta = MetaInfo(TITLE, custom_words=["#"])
    assert TheMovieDbModule._prepare_search_names(meta) == [CN_NAME, EN_NAME, convert(CN_NAME, "zh-hant")]
    meta.cn_name = convert(CN_NAME, "zh-hant")
    assert TheMovieDbModule._prepare_search_names(meta) == [meta.cn_name, CN_NAME, EN_NAME]
    meta.cn_name = None
    assert TheMovieDbModule._prepare_search_names(meta) == [EN_NAME]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_original_title_matches_series_season_and_cache(replay_module, asynchronous: bool) -> None:
    """完整发布名经真实搜索响应命中作品和第三季，缓存命中同样补季。"""
    module, queries = replay_module
    for cached in (False, True):
        meta = MetaInfo(TITLE, custom_words=["#"])
        if asynchronous:
            result = asyncio.run(module.async_recognize_media(meta=meta, source="themoviedb"))
        else:
            result = module.recognize_media(meta=meta, source="themoviedb")
        assert result is not None
        assert result.tmdb_id == 205493
        assert result.title == "新网球王子 U-17世界杯篇"
        assert result.season == meta.begin_season == 3
        assert meta.begin_episode == 3
        assert result.recognize_cache_hit is cached
    assert queries and queries[-1] == convert(CN_NAME, "zh-hant")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_explicit_id_and_season_keep_priority(replay_module, asynchronous: bool) -> None:
    """显式编号免搜索，用户指定的特别季零不能被篇章季号覆盖。"""
    module, queries = replay_module
    meta = MetaInfo(TITLE, custom_words=["#"])
    meta.begin_season = 0
    kwargs = dict(meta=meta, tmdbid=205493, mtype=MediaType.TV, cache=False)
    if asynchronous:
        result = asyncio.run(module.async_recognize_media(**kwargs))
    else:
        result = module.recognize_media(**kwargs)
    assert result.tmdb_id == 205493
    assert meta.begin_season == 0
    assert not queries


@pytest.mark.parametrize("title,aliases,expected", [
    (CN_NAME, [convert(CN_NAME, "zh-hant")], True),
    (convert(CN_NAME, "zh-hant"), [CN_NAME], True),
    (CN_NAME + "后篇", [convert(CN_NAME, "zh-hant")], False),
    (EN_NAME, ["Shin Tennis no Ouji-sama: U-17 WORLD CUP"], False),
    ("", [""], False),
])
def test_alias_match_remains_exact(title: str, aliases: list, expected: bool) -> None:
    """简繁转换不放宽为子串匹配，也不把简称猜成其他译名。"""
    assert TmdbApi._TmdbApi__compare_names(title, aliases) is expected


@pytest.mark.parametrize("scenario", [
    "explicit_season", "season_range", "group", "no_episode", "no_alias",
    "ambiguous", "short_name", "generic_name", "series_name", "specials", "movie",
    "episode_out_of_range", "conflicting_year",
])
def test_season_inference_rejects_insufficient_evidence(scenario: str) -> None:
    """无完整别名、季名歧义、通用短名和自定义编号均不能推断季号。"""
    meta = MetaInfo(TITLE, custom_words=["#"])
    info = deepcopy(FIXTURE["details"])
    info["media_type"] = MediaType.TV
    info["names"] = [convert(CN_NAME, "zh-hant")]
    group = None
    if scenario == "explicit_season":
        meta.begin_season = 1
    elif scenario == "season_range":
        meta.end_season = 4
    elif scenario == "group":
        group = "custom-group"
    elif scenario == "no_episode":
        meta.begin_episode = None
    elif scenario == "no_alias":
        info["names"] = ["另一部作品 决赛成员决定战"]
    elif scenario == "ambiguous":
        info["seasons"].append({**info["seasons"][-1], "season_number": 4})
    elif scenario == "short_name":
        info["seasons"][-1]["name"] = "战"
    elif scenario == "generic_name":
        meta.cn_name = info["names"][0] = "某部作品 SEASON3"
        info["seasons"][-1]["name"] = "Season 3"
    elif scenario == "series_name":
        info["name"] = CN_NAME
    elif scenario == "specials":
        info["seasons"][-1]["season_number"] = 0
    elif scenario == "movie":
        info["media_type"] = MediaType.MOVIE
    elif scenario == "episode_out_of_range":
        meta.begin_episode = 99
    elif scenario == "conflicting_year":
        meta.year = "2024"
    original_season = meta.begin_season
    media = MediaInfo(tmdb_info=info)
    TheMovieDbModule._fill_title_season_info(media, meta, group)
    assert meta.begin_season == original_season
    assert media.season is None


def test_existing_match_skips_extra_queries(replay_module) -> None:
    """已能识别的名称不增加繁体网络查询。"""
    module, _queries = replay_module
    module._search_by_name = Mock(return_value={
        **deepcopy(FIXTURE["details"]), "media_type": MediaType.TV
    })
    meta = MetaInfo(TITLE, custom_words=["#"])
    result = module.recognize_media(meta=meta, source="themoviedb", cache=False)
    assert result.tmdb_id == 205493
    assert module._search_by_name.call_count == 1
