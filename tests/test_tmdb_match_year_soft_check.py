"""
TMDB 剧集回退匹配的年份软校验回归测试（#7506）。
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules import themoviedb as tmdb_module
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.schemas.types import MediaType


_QUERY = "Example Show"
_META_YEAR = "2018"

# 搜索列表里的两部同名作品：前作标题正好等于查询名，正片原名是日文。
_PREQUEL_SEARCH = {
    "id": 42509,
    "name": "Example Show",
    "original_name": "Example Show",
    "first_air_date": "2011-04-01",
    "media_type": "tv",
}
_SEQUEL_SEARCH = {
    "id": 78102,
    "name": "サンプル番組0",
    "original_name": "サンプル番組0",
    "first_air_date": "2018-01-05",
    "media_type": "tv",
}


def _details(sequel_aliases: list) -> dict:
    """
    按 get_info 的真实返回结构构造两部作品的详情。
    """
    return {
        42509: {
            "id": 42509,
            "name": "Example Show",
            "original_name": "Example Show",
            "first_air_date": "2011-04-01",
            "media_type": "tv",
            "names": ["Example Show"],
            "genres": [{"id": 18}],
            "seasons": [{"season_number": 1, "air_date": "2011-04-01"}],
        },
        78102: {
            "id": 78102,
            "name": "サンプル番組0",
            "original_name": "サンプル番組0",
            "first_air_date": "2018-01-05",
            "media_type": "tv",
            "names": list(sequel_aliases),
            "genres": [{"id": 18}],
            "seasons": [{"season_number": 1, "air_date": "2018-01-05"}],
        },
    }


def _search_tv_shows() -> Mock:
    """
    模拟 TMDB 电视剧搜索：不带年份返回两部作品，带年份只返回该年作品。
    """
    def _side_effect(term: str, release_year: Optional[str] = None) -> list:
        assert term == _QUERY
        if release_year is None:
            return deepcopy([_PREQUEL_SEARCH, _SEQUEL_SEARCH])
        return deepcopy([_SEQUEL_SEARCH]) if release_year == _META_YEAR else []

    return Mock(side_effect=_side_effect)


async def _async_search_tv_shows(term: str, release_year: Optional[str] = None) -> list:
    """
    同步搜索替身的异步版本。
    """
    assert term == _QUERY
    if release_year is None:
        return deepcopy([_PREQUEL_SEARCH, _SEQUEL_SEARCH])
    return deepcopy([_SEQUEL_SEARCH]) if release_year == _META_YEAR else []


def _api(sequel_aliases: list) -> TmdbApi:
    """
    构造不初始化真实 TMDB 客户端的最小 API 实例。
    """
    details = _details(sequel_aliases)
    api = object.__new__(TmdbApi)
    api.tmdb = SimpleNamespace(language="en")
    api.search = SimpleNamespace(
        total_results=2,
        movies=Mock(),
        async_movies=AsyncMock(),
        tv_shows=_search_tv_shows(),
        async_tv_shows=AsyncMock(side_effect=_async_search_tv_shows),
        multi=Mock(),
        async_multi=AsyncMock(),
        collections=Mock(),
        async_collections=AsyncMock(),
    )
    api.movie = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    api.tv = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    api.get_info = Mock(side_effect=lambda mtype, tmdbid: deepcopy(details[tmdbid]))
    api.async_get_info = AsyncMock(side_effect=lambda mtype, tmdbid: deepcopy(details[tmdbid]))
    return api


def _meta(year: Optional[str] = _META_YEAR) -> SimpleNamespace:
    """
    构造测试场景的解析元数据：电视剧、指定年份、第 1 季。
    """
    return SimpleNamespace(
        type=MediaType.TV,
        year=year,
        begin_season=1,
        name=_QUERY,
        cn_name=None,
        en_name=_QUERY,
    )


def _module(api: TmdbApi) -> TheMovieDbModule:
    """
    构造只挂载给定 TMDB 客户端的模块实例，不初始化真实缓存与客户端。
    """
    module = object.__new__(TheMovieDbModule)
    module.tmdb = api
    return module


@pytest.mark.parametrize("asynchronous", [False, True])
def test_soft_year_prefers_year_matching_alias_over_mismatched_direct(asynchronous: bool) -> None:
    """
    回退搜索带软年份时，年份相符的别名候选应胜过年份不符的标题直达候选。
    """
    api = _api(["Example Show 0", "Example Show"])
    if asynchronous:
        result = asyncio.run(api.async_match(_QUERY, MediaType.TV, soft_year=_META_YEAR))
    else:
        result = api.match(_QUERY, MediaType.TV, soft_year=_META_YEAR)
    assert result is not None
    assert result["id"] == 78102


@pytest.mark.parametrize("asynchronous", [False, True])
def test_soft_year_keeps_mismatched_match_when_no_year_match_exists(asynchronous: bool) -> None:
    """
    没有任何年份相符的候选时，回退仍应返回年份不符的标题直达候选，不丢失召回。
    """
    api = _api(["Example Show 0"])
    if asynchronous:
        result = asyncio.run(api.async_match(_QUERY, MediaType.TV, soft_year=_META_YEAR))
    else:
        result = api.match(_QUERY, MediaType.TV, soft_year=_META_YEAR)
    assert result is not None
    assert result["id"] == 42509


def test_no_soft_year_keeps_first_title_match() -> None:
    """
    不传软年份时保持原有行为：标题直达候选直接胜出，不额外查详情。
    """
    api = _api(["Example Show 0", "Example Show"])
    result = api.match(_QUERY, MediaType.TV)
    assert result is not None
    assert result["id"] == 42509
    api.get_info.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_by_name_warns_on_year_mismatch(asynchronous: bool, monkeypatch) -> None:
    """
    年份不符的候选最终胜出时，识别日志必须显式警告元数据年份与结果年份。
    """
    warnings: list[str] = []
    monkeypatch.setattr(tmdb_module.logger, "warning", warnings.append)
    api = _api(["Example Show 0"])
    module = _module(api)
    if asynchronous:
        result = asyncio.run(module._async_search_by_name(_QUERY, _meta(), []))
    else:
        result = module._search_by_name(_QUERY, _meta(), [])
    assert result is not None
    assert result["id"] == 42509
    assert any("2018" in msg and "2011" in msg and "年份不符" in msg for msg in warnings)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_by_name_quiet_when_year_matches(asynchronous: bool, monkeypatch) -> None:
    """
    年份相符的候选胜出时不应产生年份不符警告（对照用例）。
    """
    warnings: list[str] = []
    monkeypatch.setattr(tmdb_module.logger, "warning", warnings.append)
    api = _api(["Example Show 0", "Example Show"])
    module = _module(api)
    if asynchronous:
        result = asyncio.run(module._async_search_by_name(_QUERY, _meta(), []))
    else:
        result = module._search_by_name(_QUERY, _meta(), [])
    assert result is not None
    assert result["id"] == 78102
    assert not any("年份不符" in msg for msg in warnings)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_by_name_yearless_fallback_passes_none_soft_year(asynchronous: bool, monkeypatch) -> None:
    """
    元数据无年份时，剧集回退不传软年份且不产生年份不符警告。
    """
    warnings: list[str] = []
    monkeypatch.setattr(tmdb_module.logger, "warning", warnings.append)
    api = _api(["Example Show 0"])
    module = _module(api)
    if asynchronous:
        result = asyncio.run(module._async_search_by_name(_QUERY, _meta(year=None), []))
    else:
        result = module._search_by_name(_QUERY, _meta(year=None), [])
    assert result is not None
    assert result["id"] == 42509
    assert not any("年份不符" in msg for msg in warnings)
