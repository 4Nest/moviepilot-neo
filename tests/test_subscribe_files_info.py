"""订阅文件统计相关测试"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.chain.subscribe import SubscribeChain
from app.modules.filemanager import FileManagerModule
from app.schemas.file import FileItem
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaType


def _build_subscribe(**overrides):
    data = {
        "id": 1,
        "name": "Test Show",
        "year": "2026",
        "type": MediaType.TV.value,
        "season": 1,
        "tmdbid": None,
        "doubanid": None,
        "imdbid": None,
        "tvdbid": None,
        "bangumiid": None,
        "anilistid": None,
        "media_source": None,
        "media_id": None,
        "episode_group": None,
        "start_episode": 1,
        "total_episode": 2,
    }
    data.update(overrides)
    subscribe = SimpleNamespace(**data)
    subscribe.to_dict = lambda: dict(data)
    return subscribe


def _build_mediainfo():
    return SimpleNamespace(
        type=MediaType.TV,
        title="Test Show",
        title_year="Test Show (2026)",
        year="2026",
        tmdb_id=None,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
        source=None,
        media_id=None,
    )


def test_filemanager_media_exists_skips_local_when_server_specified():
    module = FileManagerModule()
    mediainfo = _build_mediainfo()

    with patch.object(module, "media_files", return_value=[SimpleNamespace(path="/media/test.mkv")]) as media_files:
        result = module.media_exists(mediainfo, server="Emby1")

    assert result is None
    media_files.assert_not_called()


def test_subscribe_files_info_merges_multiple_mediaservers():
    subscribe = _build_subscribe(season=1, total_episode=2)
    mediainfo = _build_mediainfo()

    def _media_exists_side_effect(*, mediainfo, server=None, **kwargs):
        if server == "Emby1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={1: [1]},
                server_type="emby",
                server="Emby1",
                itemid="emby-series",
            )
        if server == "Jellyfin1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={1: [1]},
                server_type="jellyfin",
                server="Jellyfin1",
                itemid="jf-series",
            )
        return None

    helper = MagicMock()
    helper.get_services.return_value = {"Emby1": object(), "Jellyfin1": object()}

    mediaserver_chain = MagicMock()
    mediaserver_chain.get_play_url.side_effect = lambda server, item_id: f"https://{server}/item/{item_id}"
    mediaserver_chain.get_season_episode_ids.side_effect = lambda server, item_id, season: {1: f"{item_id}-ep1"}

    chain = SubscribeChain()
    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=mediainfo), \
            patch.object(chain, "media_files", return_value=None), \
            patch.object(chain, "media_exists", side_effect=_media_exists_side_effect), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=mediaserver_chain), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        result = chain.subscribe_files_info(subscribe)

    library = result.episodes[1].library
    servers = {item.server for item in library}
    assert servers == {"Emby1", "Jellyfin1"}
    assert all(str(item.file_path).startswith("https://") for item in library)


def test_subscribe_files_info_uses_season_zero_for_tv():
    subscribe = _build_subscribe(season=0, total_episode=1, start_episode=1)
    mediainfo = _build_mediainfo()
    captured_seasons = []

    def _media_exists_side_effect(*, mediainfo, server=None, **kwargs):
        if server == "Emby1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={0: [1]},
                server_type="emby",
                server="Emby1",
                itemid="emby-special",
            )
        return None

    def _get_season_episode_ids(server, item_id, season):
        captured_seasons.append(season)
        return {1: f"{item_id}-ep1"}

    helper = MagicMock()
    helper.get_services.return_value = {"Emby1": object()}

    mediaserver_chain = MagicMock()
    mediaserver_chain.get_play_url.return_value = "https://emby/item/1"
    mediaserver_chain.get_season_episode_ids.side_effect = _get_season_episode_ids

    chain = SubscribeChain()
    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=mediainfo), \
            patch.object(chain, "media_files", return_value=None), \
            patch.object(chain, "media_exists", side_effect=_media_exists_side_effect), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=mediaserver_chain), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        result = chain.subscribe_files_info(subscribe)

    assert captured_seasons == [0]
    assert len(result.episodes[1].library) == 1
    assert result.episodes[1].library[0].server == "Emby1"


def test_subscribe_files_info_uses_recognition_cache_and_falls_back_when_tmdb_unavailable():
    """
    文件统计优先复用识别缓存；TMDB 分集与媒体识别均失败时仍降级展示订阅、下载和媒体库详情。
    """
    subscribe = _build_subscribe(
        name="断网降级剧",
        year="2026",
        tmdbid=12345,
        media_source="themoviedb",
        media_id="12345",
        season=1,
        start_episode=1,
        total_episode=2,
    )
    history = SimpleNamespace(download_hash="hash-1", torrent_name="Show.S01E01.1080p", torrent_site="测试站点")
    download_file = SimpleNamespace(
        filepath="Show.S01E01.1080p.mkv",
        downloader="qBittorrent",
        fullpath="/downloads/Show.S01E01.1080p.mkv",
    )
    captured_mediainfo = []

    def _media_exists_side_effect(*, mediainfo, server=None, **kwargs):
        captured_mediainfo.append(mediainfo)
        if server == "Emby1":
            return ExistMediaInfo(
                type=MediaType.TV,
                seasons={1: [2]},
                server_type="emby",
                server="Emby1",
                itemid="emby-12345",
            )
        return None

    helper = MagicMock()
    helper.get_services.return_value = {"Emby1": object()}
    mediaserver_chain = MagicMock()
    mediaserver_chain.get_play_url.side_effect = lambda server, item_id: f"https://{server}/item/{item_id}"
    mediaserver_chain.get_season_episode_ids.return_value = {2: "emby-ep2"}
    tmdb_chain = MagicMock()
    tmdb_chain.tmdb_episodes.return_value = None

    chain = SubscribeChain()
    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch("app.chain.subscribe.TmdbChain", return_value=tmdb_chain), \
            patch.object(chain, "recognize_media", return_value=None) as recognize_mock, \
            patch.object(
                chain,
                "media_files",
                return_value=[FileItem(storage="local", type="file", path="/media/Show.S01E01.1080p.mkv")],
            ), \
            patch.object(chain, "media_exists", side_effect=_media_exists_side_effect), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=mediaserver_chain), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = [history]
        download_oper.return_value.get_files_by_hash.return_value = [download_file]
        result = chain.subscribe_files_info(subscribe)

    assert recognize_mock.call_args.kwargs["cache"] is True
    assert result is not None
    assert result.subscribe is not None
    assert result.subscribe.name == "断网降级剧"
    assert sorted(result.episodes.keys()) == [1, 2]
    assert result.episodes[1].title == "第 1 集"
    assert [item.file_path for item in result.episodes[1].download] == ["/downloads/Show.S01E01.1080p.mkv"]
    assert [item.file_path for item in result.episodes[1].library] == ["/media/Show.S01E01.1080p.mkv"]
    assert [item.server for item in result.episodes[2].library] == ["Emby1"]
    assert len(captured_mediainfo) == 1
    assert captured_mediainfo[0].tmdb_id == 12345
    assert captured_mediainfo[0].title == "断网降级剧"


def test_subscribe_files_info_falls_back_for_douban_movie_and_english_tv_type():
    """
    豆瓣电影在远端识别失败时仍保留订阅和下载详情，英文电视剧类型也能正确分集统计。
    """
    movie_subscribe = _build_subscribe(
        name="豆瓣电影",
        year="2025",
        type=MediaType.MOVIE.value,
        season=None,
        doubanid="35123456",
        media_source="douban",
        media_id="35123456",
        total_episode=0,
    )
    tv_subscribe = _build_subscribe(
        name="英文类型剧",
        year="2026",
        type="tv",
        season=1,
        media_source=None,
        media_id=None,
        start_episode=1,
        total_episode=1,
    )
    helper = MagicMock()
    helper.get_services.return_value = {}

    chain = SubscribeChain()
    captured_movie_media = []

    def _capture_movie_files(mediainfo):
        captured_movie_media.append(mediainfo)
        return [FileItem(storage="local", type="file", path="/media/movie.2025.mkv")]

    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=None), \
            patch.object(chain, "media_files", side_effect=_capture_movie_files), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=MagicMock()), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        movie_result = chain.subscribe_files_info(movie_subscribe)

    assert movie_result is not None
    assert movie_result.subscribe is not None
    assert movie_result.subscribe.name == "豆瓣电影"
    assert [item.file_path for item in movie_result.episodes[0].library] == ["/media/movie.2025.mkv"]
    assert captured_movie_media[0].douban_id == "35123456"

    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media", return_value=None), \
            patch.object(
                chain,
                "media_files",
                return_value=[FileItem(storage="local", type="file", path="/media/Show.S01E01.mkv")],
            ), \
            patch("app.chain.subscribe.MediaServerHelper", return_value=helper), \
            patch("app.chain.subscribe.MediaServerChain", return_value=MagicMock()), \
            patch("app.chain.subscribe.Subscribe", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
        download_oper.return_value.get_by_mediaid.return_value = []
        tv_result = chain.subscribe_files_info(tv_subscribe)

    assert tv_result is not None
    assert tv_result.subscribe is not None
    assert [item.file_path for item in tv_result.episodes[1].library] == ["/media/Show.S01E01.mkv"]


def test_subscribe_files_info_skips_corrupted_subscribe_with_blank_name():
    """
    名称为空白的损坏订阅记录仍保持跳过语义，避免构造无效元数据。
    """
    corrupted = _build_subscribe(name="   ", type=MediaType.TV.value)
    chain = SubscribeChain()

    with patch("app.chain.subscribe.DownloadHistoryOper") as download_oper, \
            patch.object(chain, "recognize_media") as recognize_mock:
        download_oper.return_value.get_by_mediaid.return_value = []
        result = chain.subscribe_files_info(corrupted)

    assert result is not None
    assert result.subscribe is None
    assert result.episodes == {}
    recognize_mock.assert_not_called()
