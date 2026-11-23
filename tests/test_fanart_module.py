"""
Fanart 模块查询、缓存、TV 回退 TMDB ID 与缺图告警去重测试。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.context import MediaInfo
from app.modules.fanart import FanartModule
from app.schemas.types import MediaType


def test_fanart_missing_warning_deduplicated_across_sync_async_and_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一媒体缺图时，在缓存有效期内（同步/异步/并发）仅输出一次 warning 告警。"""

    class _EmptyResponse:
        def json(self) -> dict:
            return {"status": "error", "error message": "Not found"}

    class _FakeRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, _url: str, **_kwargs) -> _EmptyResponse:
            return _EmptyResponse()

    class _FakeAsyncRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        async def get_json(self, _url: str) -> dict:
            return {"status": "error", "error message": "Not found"}

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _FakeRequestUtils)
    monkeypatch.setattr("app.modules.fanart.AsyncRequestUtils", _FakeAsyncRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "dedup-key")

    module = FanartModule()
    module.clear_cache()
    mediainfo = MediaInfo(type=MediaType.MOVIE, tmdb_id=7531001, title="缺图电影", year="2026")

    try:
        with patch("app.modules.fanart.logger.warning") as warning_log:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _i: module.obtain_images(mediainfo), range(8)))
            assert all(item is None for item in results)
            assert asyncio.run(module.async_obtain_images(mediainfo)) is None
            assert module.metadata_img(mediainfo) is None
            assert warning_log.call_count == 1
    finally:
        module.clear_cache()


def test_fanart_missing_warning_resets_on_key_change_cache_clear_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更换 API Key、清理缓存或恢复获取到图片后，再次缺图应重新告警一次。"""

    class _ErrorFanartResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    responses = iter(
        [
            {"status": "error"},
            {"status": "error"},
            {"status": "error"},
        ]
    )

    class _SequencedRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, _url: str, **_kwargs) -> _ErrorFanartResponse:
            return _ErrorFanartResponse(next(responses))

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _SequencedRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "reset-key-a")

    module = FanartModule()
    module.clear_cache()
    mediainfo = MediaInfo(type=MediaType.MOVIE, tmdb_id=7531003, title="测试电影", year="2026")

    try:
        with patch("app.modules.fanart.logger.warning") as warning_log:
            assert module.obtain_images(mediainfo) is None
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 1

            monkeypatch.setattr(settings, "FANART_API_KEY", "reset-key-b")
            assert module.obtain_images(mediainfo) is None
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 2

            module.clear_cache()
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 3

            valid = module._FanartModule__extract_images(
                mediainfo,
                {"movieposter": [{"url": "https://artwork.invalid/poster.jpg", "lang": "zh"}]},
            )
            assert valid == {"poster.jpg": "https://artwork.invalid/poster.jpg"}
            assert module._FanartModule__extract_images(mediainfo, {}) is None
            assert warning_log.call_count == 4
    finally:
        module.clear_cache()


def test_fanart_tv_falls_back_to_tmdb_id_with_explicit_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """电视剧缺失 TVDB 标识时应回退使用 TMDB 标识并携带 source=tmdb，同时保留 TVDB 优先顺序。"""
    requested_urls: list[str] = []
    async_requested_urls: list[str] = []

    class _PayloadResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _RoutingRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, url: str, **_kwargs) -> _PayloadResponse:
            requested_urls.append(url)
            if "/v3/tv/95842?" in url and "source=tmdb" in url:
                return _PayloadResponse(
                    {
                        "hdtvlogo": [
                            {"url": "https://artwork.invalid/joy-logo.png", "lang": "zh", "likes": "5"}
                        ],
                        "seasonposter": [
                            {"url": "https://artwork.invalid/joy-s1.jpg", "season": "1"}
                        ],
                    }
                )
            if "/v3/tv/81189?" in url and "source=tmdb" not in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/bb-tvdb-poster.jpg", "lang": "en", "likes": "3"}
                        ]
                    }
                )
            return _PayloadResponse({})

    class _RoutingAsyncRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        async def get_json(self, url: str) -> dict:
            async_requested_urls.append(url)
            if "/v3/tv/1396?" in url and "source=tmdb" in url:
                return {
                    "hdtvlogo": [
                        {"url": "https://artwork.invalid/bb-tmdb-logo.png", "lang": "en", "likes": "9"}
                    ]
                }
            return {}

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _RoutingRequestUtils)
    monkeypatch.setattr("app.modules.fanart.AsyncRequestUtils", _RoutingAsyncRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "tv-fallback-key")

    module = FanartModule()
    module.clear_cache()

    tmdb_only_tv = MediaInfo(type=MediaType.TV, tmdb_id=95842, title="庆余年", year="2019")
    async_tmdb_only_tv = MediaInfo(type=MediaType.TV, tmdb_id=1396, title="绝命毒师", year="2008")
    both_ids_tv = MediaInfo(
        type=MediaType.TV, tvdb_id=81189, tmdb_id=1396, title="绝命毒师", year="2008"
    )

    try:
        assert module.obtain_images(tmdb_only_tv) is tmdb_only_tv
        assert tmdb_only_tv.logo_path == "https://artwork.invalid/joy-logo.png"
        assert module.metadata_img(tmdb_only_tv, season=1) == {
            "season01-poster.jpg": "https://artwork.invalid/joy-s1.jpg"
        }

        assert module.obtain_images(both_ids_tv) is both_ids_tv
        assert both_ids_tv.poster_path == "https://artwork.invalid/bb-tvdb-poster.jpg"

        assert asyncio.run(module.async_obtain_images(async_tmdb_only_tv)) is async_tmdb_only_tv
        assert async_tmdb_only_tv.logo_path == "https://artwork.invalid/bb-tmdb-logo.png"

        assert requested_urls == [
            "https://webservice.fanart.tv/v3/tv/95842?api_key=tv-fallback-key&source=tmdb",
            "https://webservice.fanart.tv/v3/tv/81189?api_key=tv-fallback-key",
        ]
        assert async_requested_urls == [
            "https://webservice.fanart.tv/v3/tv/1396?api_key=tv-fallback-key&source=tmdb",
        ]
    finally:
        module.clear_cache()


def test_fanart_tv_same_numeric_id_isolates_tvdb_and_tmdb_cache_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同号 TVDB 与 TMDB 剧集必须隔离请求缓存和缺图告警，避免跨命名空间串图或吞告警。"""
    requested_urls: list[str] = []

    class _PayloadResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _CollisionRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, url: str, **_kwargs) -> _PayloadResponse:
            requested_urls.append(url)
            if "/v3/tv/82856?" in url and "source=tmdb" in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/mandalorian.jpg", "lang": "en", "likes": "8"}
                        ]
                    }
                )
            if "/v3/tv/82856?" in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/ninja-nonsense.jpg", "lang": "en", "likes": "2"}
                        ]
                    }
                )
            return _PayloadResponse({})

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _CollisionRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "collision-key")

    module = FanartModule()
    module.clear_cache()

    tvdb_show = MediaInfo(type=MediaType.TV, tvdb_id=82856, title="Ninja Nonsense", year="2004")
    tmdb_show = MediaInfo(type=MediaType.TV, tmdb_id=82856, title="The Mandalorian", year="2019")
    empty_tvdb_show = MediaInfo(type=MediaType.TV, tvdb_id=70327, title="同号缺图剧", year="2026")
    empty_tmdb_show = MediaInfo(type=MediaType.TV, tmdb_id=70327, title="同号缺图剧", year="2026")

    try:
        assert module.obtain_images(tvdb_show) is tvdb_show
        assert module.obtain_images(tmdb_show) is tmdb_show
        assert tvdb_show.poster_path == "https://artwork.invalid/ninja-nonsense.jpg"
        assert tmdb_show.poster_path == "https://artwork.invalid/mandalorian.jpg"

        with patch("app.modules.fanart.logger.warning") as warning_log:
            assert module.obtain_images(empty_tvdb_show) is None
            assert module.obtain_images(empty_tvdb_show) is None
            assert module.obtain_images(empty_tmdb_show) is None
            assert module.obtain_images(empty_tmdb_show) is None
            assert module._FanartModule__extract_images(empty_tvdb_show, {}) is None
            assert module._FanartModule__extract_images(empty_tmdb_show, {}) is None
            assert warning_log.call_count == 2

        assert requested_urls == [
            "https://webservice.fanart.tv/v3/tv/82856?api_key=collision-key",
            "https://webservice.fanart.tv/v3/tv/82856?api_key=collision-key&source=tmdb",
            "https://webservice.fanart.tv/v3/tv/70327?api_key=collision-key",
            "https://webservice.fanart.tv/v3/tv/70327?api_key=collision-key&source=tmdb",
        ]
    finally:
        module.clear_cache()


def test_fanart_non_tv_with_only_tmdb_id_does_not_fallback_to_tv_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非电影且非电视剧类型（如合集）仅有 TMDB 标识时，不得误查 TV 端点。"""
    requested_urls: list[str] = []

    class _UnexpectedRequestUtils:
        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, url: str, **_kwargs) -> None:
            requested_urls.append(url)
            return None

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _UnexpectedRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "collection-key")

    module = FanartModule()
    module.clear_cache()
    collection = MediaInfo(type=MediaType.COLLECTION, tmdb_id=10, title="测试系列")

    try:
        with patch("app.modules.fanart.logger.info") as info_log:
            assert module.obtain_images(collection) is None

        assert requested_urls == []
        info_log.assert_called_once()
    finally:
        module.clear_cache()
