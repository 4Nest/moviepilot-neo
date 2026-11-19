from pathlib import Path
from types import SimpleNamespace

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.modules.filemanager import FileManagerModule
from app.schemas import (
    FileItem,
    StorageOperSelectionEventData,
    TmdbEpisode,
    TransferDirectoryConf,
    TransferInfo,
)
from app.schemas.types import ChainEventType, MediaType


class GuardedStorage:
    """
    用于验证预览模式不会访问可能有副作用的存储整理接口。
    """

    def get_folder(self, path: Path):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"预览不应创建或获取目标目录：{path}")

    def get_item(self, path: Path):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"预览不应探测目标文件：{path}")

    def copy(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应复制文件")

    def move(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应移动文件")

    def rename(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应重命名文件")

    def delete(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应删除文件")


def _build_meta(
    title: str,
    media_type: MediaType = MediaType.TV,
    season: int = 1,
    episode: int = 1,
    part: str = None,
):
    meta = MetaBase(title)
    meta.type = media_type
    meta.name = "Breaking Bad" if media_type == MediaType.TV else "Test Movie"
    meta.year = "2008" if media_type == MediaType.TV else "2026"
    meta.begin_season = season
    meta.begin_episode = episode
    meta.part = part
    return meta


def test_cloud_storage_preview_only_calculates_target_path():
    fileitem = FileItem(
        storage="alist",
        path="/downloads/Test.Show.S01E01.mkv",
        type="file",
        name="Test.Show.S01E01.mkv",
        basename="Test.Show.S01E01",
        extension="mkv",
        size=1024,
    )
    meta = _build_meta("Test.Show.S01E01.mkv", season=1, episode=1)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        year="2026",
        tmdb_id=12345,
    )
    target_directory = TransferDirectoryConf(
        name="cloud-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path="/library",
        library_storage="alist",
        renaming=True,
        scraping=True,
        notify=True,
    )
    guarded_storage = GuardedStorage()

    transferinfo = FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
        preview=True,
    )

    assert transferinfo.success is True
    assert transferinfo.need_notify is False
    assert transferinfo.need_scrape is True
    assert transferinfo.target_item.storage == "alist"
    assert transferinfo.target_item.path.endswith(".mkv")
    assert transferinfo.target_diritem.path.startswith("/library/")
    assert transferinfo.file_list == [fileitem.path]
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def test_local_storage_preview_skips_target_conflict_checks(tmp_path):
    source_file = tmp_path / "downloads" / "Test.Show.S01E02.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"test video")
    library_path = tmp_path / "library"
    fileitem = FileItem(
        storage="local",
        path=source_file.as_posix(),
        type="file",
        name=source_file.name,
        basename=source_file.stem,
        extension="mkv",
        size=source_file.stat().st_size,
    )
    meta = _build_meta(source_file.name, season=1, episode=2)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        year="2026",
        tmdb_id=12345,
    )
    target_directory = TransferDirectoryConf(
        name="local-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path=library_path.as_posix(),
        library_storage="local",
        renaming=True,
        scraping=True,
        notify=True,
    )
    guarded_storage = GuardedStorage()

    transferinfo = FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
        preview=True,
    )

    assert transferinfo.success is True
    assert transferinfo.need_notify is False
    assert transferinfo.need_scrape is True
    assert transferinfo.target_item.storage == "local"
    assert transferinfo.target_item.path.startswith(library_path.as_posix())
    assert transferinfo.target_item.path.endswith(".mkv")
    assert transferinfo.file_list == [fileitem.path]
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def _build_bluray_dir_preview(
    source_name: str,
    media_type: MediaType,
    season: int = None,
    part: str = None,
):
    fileitem = FileItem(
        storage="alist",
        path=f"/downloads/{source_name}",
        type="dir",
        name=source_name,
        basename=source_name,
    )
    meta = _build_meta(
        fileitem.name,
        media_type=media_type,
        season=season,
        episode=None,
        part=part,
    )
    mediainfo = MediaInfo(
        type=media_type,
        title="Breaking Bad" if media_type == MediaType.TV else "Test Movie",
        year="2008" if media_type == MediaType.TV else "2026",
        tmdb_id=1396,
    )
    target_directory = TransferDirectoryConf(
        name="cloud-library",
        transfer_type="copy",
        overwrite_mode="never",
        library_path="/library",
        library_storage="alist",
        renaming=True,
        scraping=True,
        notify=True,
    )

    return FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=GuardedStorage(),
        target_oper=GuardedStorage(),
        preview=True,
    )


def test_tv_bluray_dir_preview_preserves_disk_folder_from_meta_part():
    transferinfo = _build_bluray_dir_preview(
        source_name="Breaking Bad Season 2 - Disk 1",
        media_type=MediaType.TV,
        season=2,
        part="Disk1",
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Breaking Bad (2008)/Season 2/Disc 1"
    assert transferinfo.target_diritem.path == transferinfo.target_item.path
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def test_tv_bluray_dir_preview_preserves_disc_folder_from_source_name():
    transferinfo = _build_bluray_dir_preview(
        source_name="BREAKING_BAD_S01D01",
        media_type=MediaType.TV,
        season=1,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Breaking Bad (2008)/Season 1/Disc 1"


def test_tv_bluray_dir_preview_falls_back_to_source_name_without_disc_number():
    source_name = "Breaking Bad Season 3 Bonus Disc"
    transferinfo = _build_bluray_dir_preview(
        source_name=source_name,
        media_type=MediaType.TV,
        season=3,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == f"/library/Breaking Bad (2008)/Season 3/{source_name}"


def test_movie_bluray_dir_preview_keeps_movie_root_layout():
    transferinfo = _build_bluray_dir_preview(
        source_name="Test Movie Disc 1",
        media_type=MediaType.MOVIE,
        part="Disc1",
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Test Movie (2026)"


def _make_preview_chain(fileitems):
    """构造用于预览链路单测的轻量 TransferChain 实例。"""
    chain = object.__new__(TransferChain)
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._subtitle_exts + chain._audio_exts
    )
    active_jobs = set()
    chain.jobview = SimpleNamespace(
        migrate_task=lambda task: True,
        running_task=lambda task: active_jobs.add(task.fileitem.path),
        finish_task=lambda task: active_jobs.discard(task.fileitem.path),
        fail_task=lambda task: active_jobs.discard(task.fileitem.path),
        try_remove_job=lambda task: active_jobs.discard(task.fileitem.path),
        list_jobs=lambda: list(active_jobs),
    )
    chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (item, False) for item in fileitems
    ]
    chain._TransferChain__put_to_jobview = lambda task: (
        active_jobs.add(task.fileitem.path) or True
    )
    chain._TransferChain__register_scrape_batch_task = lambda task: None
    chain._TransferChain__close_scrape_batch = lambda batch_id: None
    chain._TransferChain__start_job_execution = lambda task: None
    chain._TransferChain__finish_job_execution = lambda task: None
    chain._TransferChain__finish_scrape_batch_task = lambda task: None
    chain._requires_automatic_category = lambda task: False
    return chain


def test_preview_multi_episode_reuses_request_cache_and_skips_images(monkeypatch):
    """多集同剧预览应跳过拉图，并复用单次请求级缓存避免重复识别、查库与事件广播。"""
    fileitems = [
        FileItem(
            storage="local",
            path=f"/downloads/TestShow/Test.Show.S01E0{idx}.mkv",
            type="file",
            name=f"Test.Show.S01E0{idx}.mkv",
            basename=f"Test.Show.S01E0{idx}",
            extension="mkv",
            size=1024,
        )
        for idx in range(1, 4)
    ]
    dir_item = FileItem(
        storage="local",
        path="/downloads/TestShow",
        type="dir",
        name="TestShow",
        basename="TestShow",
    )
    chain = _make_preview_chain(fileitems)

    counts = {
        "download_oper_init": 0,
        "resolve_download_history": 0,
        "subscribe_custom_words": 0,
        "recognize_by_meta": 0,
        "obtain_images": 0,
        "supplement_tmdb_info": 0,
        "transfer_history_lookup": 0,
        "tmdb_episodes": 0,
        "get_dir": 0,
        "storage_oper_event": 0,
    }
    obtain_images_flags = []

    def fake_resolve_download_history(**kwargs):
        counts["resolve_download_history"] += 1
        return None

    def fake_get_subscribe_custom_words(history_record):
        counts["subscribe_custom_words"] += 1
        return None

    chain._resolve_download_history = fake_resolve_download_history
    chain._get_subscribe_custom_words = fake_get_subscribe_custom_words
    chain.obtain_images = lambda mediainfo: counts.__setitem__(
        "obtain_images", counts["obtain_images"] + 1
    )

    cached_oper = lambda: None

    def fake_transfer(
        fileitem,
        meta,
        mediainfo,
        target_directory,
        episodes_info=None,
        source_oper=None,
        target_oper=None,
        preview=False,
        **kwargs,
    ):
        assert preview is True
        assert source_oper is cached_oper
        assert target_oper is cached_oper
        assert episodes_info and len(episodes_info) == 3
        target_path = (
            f"{target_directory.library_path}/{mediainfo.title} ({mediainfo.year})/"
            f"Season 1/{mediainfo.title} - S01E0{meta.begin_episode}.mkv"
        )
        return TransferInfo(
            success=True,
            fileitem=fileitem,
            target_item=FileItem(storage="local", path=target_path, type="file"),
            target_diritem=FileItem(
                storage="local",
                path=f"{target_directory.library_path}/{mediainfo.title} ({mediainfo.year})",
                type="dir",
            ),
        )

    chain.transfer = fake_transfer

    def fake_download_oper():
        counts["download_oper_init"] += 1
        return SimpleNamespace()

    def fake_transfer_oper():
        def get_by_type_tmdbid(tmdbid, mtype):
            counts["transfer_history_lookup"] += 1
            return SimpleNamespace(title="测试剧集")

        return SimpleNamespace(get_by_type_tmdbid=get_by_type_tmdbid)

    def fake_media_chain():
        def recognize_by_meta(meta, **kwargs):
            counts["recognize_by_meta"] += 1
            obtain_images_flags.append(kwargs.get("obtain_images"))
            return MediaInfo(
                type=MediaType.TV,
                title="Test Show",
                year="2026",
                tmdb_id=54321,
                source="themoviedb",
            )

        def supplement_tmdb_info(mediainfo, meta):
            counts["supplement_tmdb_info"] += 1
            return mediainfo

        return SimpleNamespace(
            recognize_by_meta=recognize_by_meta,
            supplement_tmdb_info=supplement_tmdb_info,
        )

    def fake_tmdb_chain():
        def tmdb_episodes(tmdbid, season, episode_group=None):
            counts["tmdb_episodes"] += 1
            return [
                TmdbEpisode(episode_number=idx, name=f"EP{idx}")
                for idx in range(1, 4)
            ]

        return SimpleNamespace(tmdb_episodes=tmdb_episodes)

    target_dir = TransferDirectoryConf(
        name="tv-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path="/library",
        library_storage="local",
        renaming=True,
        scraping=False,
        notify=False,
    )

    def fake_directory_helper():
        def get_dir(**kwargs):
            counts["get_dir"] += 1
            return target_dir

        return SimpleNamespace(get_dir=get_dir)

    def fake_send_event(etype, data):
        if etype == ChainEventType.StorageOperSelection:
            counts["storage_oper_event"] += 1
            return SimpleNamespace(
                event_data=StorageOperSelectionEventData(
                    storage=data.storage,
                    storage_oper=cached_oper,
                )
            )
        return None

    monkeypatch.setattr("app.chain.transfer.DownloadHistoryOper", fake_download_oper)
    monkeypatch.setattr("app.chain.transfer.TransferHistoryOper", fake_transfer_oper)
    monkeypatch.setattr("app.chain.transfer.MediaChain", fake_media_chain)
    monkeypatch.setattr("app.chain.transfer.TmdbChain", fake_tmdb_chain)
    monkeypatch.setattr("app.chain.transfer.DirectoryHelper", fake_directory_helper)
    monkeypatch.setattr("app.chain.transfer.eventmanager.send_event", fake_send_event)
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.StorageChain", lambda: SimpleNamespace())
    monkeypatch.setattr(settings, "SCRAP_FOLLOW_TMDB", False)

    def fake_meta_info_path(path, **kwargs):
        ep = int(Path(path).stem[-1])
        return _build_meta(
            Path(path).name, media_type=MediaType.TV, season=1, episode=ep
        )

    monkeypatch.setattr("app.chain.transfer.MetaInfoPath", fake_meta_info_path)

    state, result = chain.do_transfer(
        fileitem=dir_item,
        background=False,
        manual=True,
        preview=True,
    )

    assert state is True
    assert result["summary"] == {"total": 3, "success": 3, "failed": 0}
    assert [item["episode"] for item in result["items"]] == [1, 2, 3]
    assert all("测试剧集" in item["target"] for item in result["items"])
    assert obtain_images_flags == [False]
    assert counts["obtain_images"] == 0
    assert counts["download_oper_init"] == 1
    assert counts["resolve_download_history"] == 1
    assert counts["recognize_by_meta"] == 1
    assert counts["supplement_tmdb_info"] == 1
    assert counts["transfer_history_lookup"] == 1
    assert counts["tmdb_episodes"] == 1
    assert counts["get_dir"] == 1
    assert counts["storage_oper_event"] == 1
    assert chain.jobview.list_jobs() == []


def test_preview_explicit_download_history_skips_obtain_images(monkeypatch):
    """下载历史命中显式 ID 时，预览模式不应调用 obtain_images 且同剧只识别一次。"""
    fileitems = [
        FileItem(
            storage="local",
            path=f"/downloads/Anime/Anime.S01E0{idx}.mkv",
            type="file",
            name=f"Anime.S01E0{idx}.mkv",
            basename=f"Anime.S01E0{idx}",
            extension="mkv",
            size=1024,
        )
        for idx in range(1, 3)
    ]
    dir_item = FileItem(
        storage="local",
        path="/downloads/Anime",
        type="dir",
        name="Anime",
        basename="Anime",
    )
    chain = _make_preview_chain(fileitems)
    history = SimpleNamespace(
        id=10,
        download_hash="anime-hash",
        downloader="qbittorrent",
        type=MediaType.TV.value,
        title="Anime",
        year="2025",
        media_id=None,
        tmdbid=99999,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source=None,
        episode_group=None,
        media_category=None,
        username=None,
        custom_words="foo => bar",
        note=None,
    )

    counts = {
        "recognize_media": 0,
        "obtain_images": 0,
        "subscribe_custom_words": 0,
    }

    chain._resolve_download_history = lambda **kwargs: history
    orig_get_custom_words = TransferChain._get_subscribe_custom_words

    def counted_custom_words(history_record):
        counts["subscribe_custom_words"] += 1
        return orig_get_custom_words(history_record)

    chain._get_subscribe_custom_words = counted_custom_words

    def fake_recognize_media(**kwargs):
        counts["recognize_media"] += 1
        return MediaInfo(
            type=MediaType.TV,
            title="Anime",
            year="2025",
            tmdb_id=99999,
            source="themoviedb",
        )

    chain.recognize_media = fake_recognize_media
    chain.obtain_images = lambda mediainfo: counts.__setitem__(
        "obtain_images", counts["obtain_images"] + 1
    )
    chain.transfer = lambda fileitem, target_directory, **kwargs: TransferInfo(
        success=True,
        fileitem=fileitem,
        target_item=FileItem(
            storage="local",
            path=f"{target_directory.library_path}/{fileitem.name}",
            type="file",
        ),
        target_diritem=FileItem(
            storage="local", path=target_directory.library_path, type="dir"
        ),
    )

    target_dir = TransferDirectoryConf(
        name="anime-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path="/library",
        library_storage="local",
        renaming=True,
        scraping=False,
        notify=False,
    )

    monkeypatch.setattr("app.chain.transfer.DownloadHistoryOper", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(get_by_type_tmdbid=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        lambda: SimpleNamespace(supplement_tmdb_info=lambda media, meta: media),
    )
    monkeypatch.setattr(
        "app.chain.transfer.TmdbChain",
        lambda: SimpleNamespace(tmdb_episodes=lambda **kwargs: []),
    )
    monkeypatch.setattr(
        "app.chain.transfer.DirectoryHelper",
        lambda: SimpleNamespace(get_dir=lambda **kwargs: target_dir),
    )
    monkeypatch.setattr("app.chain.transfer.eventmanager.send_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.StorageChain", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.chain.transfer.MetaInfoPath",
        lambda path, **kwargs: _build_meta(
            Path(path).name,
            media_type=MediaType.TV,
            season=1,
            episode=int(Path(path).stem[-1]),
        ),
    )

    state, result = chain.do_transfer(
        fileitem=dir_item,
        background=False,
        manual=True,
        preview=True,
    )

    assert state is True
    assert result["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert counts["recognize_media"] == 1
    assert counts["obtain_images"] == 0
    assert counts["subscribe_custom_words"] == 1


def test_preview_caches_failed_media_recognition_within_request(monkeypatch):
    """同目录多集识别均返回 None 时，单次预览内应缓存失败结果避免重复网络请求。"""
    fileitems = [
        FileItem(
            storage="local",
            path=f"/downloads/UnknownShow/Unknown.Show.S01E0{idx}.mkv",
            type="file",
            name=f"Unknown.Show.S01E0{idx}.mkv",
            basename=f"Unknown.Show.S01E0{idx}",
            extension="mkv",
            size=1024,
        )
        for idx in range(1, 4)
    ]
    dir_item = FileItem(
        storage="local",
        path="/downloads/UnknownShow",
        type="dir",
        name="UnknownShow",
        basename="UnknownShow",
    )
    chain = _make_preview_chain(fileitems)
    chain._resolve_download_history = lambda **kwargs: None

    recognize_calls = 0

    def fake_recognize_by_meta(meta, **kwargs):
        nonlocal recognize_calls
        recognize_calls += 1
        assert kwargs.get("obtain_images") is False
        return None

    monkeypatch.setattr("app.chain.transfer.DownloadHistoryOper", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(get_by_type_tmdbid=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        lambda: SimpleNamespace(recognize_by_meta=fake_recognize_by_meta),
    )
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.StorageChain", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.chain.transfer.MetaInfoPath",
        lambda path, **kwargs: _build_meta(
            Path(path).name,
            media_type=MediaType.TV,
            season=1,
            episode=int(Path(path).stem[-1]),
        ),
    )

    state, result = chain.do_transfer(
        fileitem=dir_item,
        background=False,
        manual=True,
        preview=True,
    )

    assert state is False
    assert result["summary"] == {"total": 3, "success": 0, "failed": 3}
    assert recognize_calls == 1
    assert chain.jobview.list_jobs() == []


def test_preview_movie_collection_same_dir_recognizes_each_movie_independently(
    monkeypatch,
):
    """同目录不同标题或年份的电影在预览时必须独立识别，不能串台。"""
    fileitems = [
        FileItem(
            storage="local",
            path="/downloads/Collection/Movie.Alpha.2020.mkv",
            type="file",
            name="Movie.Alpha.2020.mkv",
            basename="Movie.Alpha.2020",
            extension="mkv",
            size=1024,
        ),
        FileItem(
            storage="local",
            path="/downloads/Collection/Movie.Beta.2023.mkv",
            type="file",
            name="Movie.Beta.2023.mkv",
            basename="Movie.Beta.2023",
            extension="mkv",
            size=1024,
        ),
    ]
    dir_item = FileItem(
        storage="local",
        path="/downloads/Collection",
        type="dir",
        name="Collection",
        basename="Collection",
    )
    chain = _make_preview_chain(fileitems)
    chain._resolve_download_history = lambda **kwargs: None

    recognized_keys = []

    def fake_recognize_by_meta(meta, **kwargs):
        recognized_keys.append((meta.name, meta.year))
        if meta.name == "Movie Alpha":
            return MediaInfo(
                type=MediaType.MOVIE,
                title="电影甲",
                year="2020",
                tmdb_id=1001,
                source="themoviedb",
            )
        return MediaInfo(
            type=MediaType.MOVIE,
            title="电影乙",
            year="2023",
            tmdb_id=1002,
            source="themoviedb",
        )

    target_dir = TransferDirectoryConf(
        name="movie-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path="/movies",
        library_storage="local",
        renaming=True,
        scraping=False,
        notify=False,
    )

    chain.transfer = lambda fileitem, mediainfo, target_directory, **kwargs: TransferInfo(
        success=True,
        fileitem=fileitem,
        target_item=FileItem(
            storage="local",
            path=f"{target_directory.library_path}/{mediainfo.title} ({mediainfo.year}).mkv",
            type="file",
        ),
        target_diritem=FileItem(
            storage="local",
            path=f"{target_directory.library_path}/{mediainfo.title} ({mediainfo.year})",
            type="dir",
        ),
    )

    monkeypatch.setattr("app.chain.transfer.DownloadHistoryOper", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(get_by_type_tmdbid=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        lambda: SimpleNamespace(
            recognize_by_meta=fake_recognize_by_meta,
            supplement_tmdb_info=lambda media, meta: media,
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.DirectoryHelper",
        lambda: SimpleNamespace(get_dir=lambda **kwargs: target_dir),
    )
    monkeypatch.setattr("app.chain.transfer.eventmanager.send_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.StorageChain", lambda: SimpleNamespace())

    def fake_meta_info_path(path, **kwargs):
        name = Path(path).name
        meta = MetaBase(name)
        meta.type = MediaType.MOVIE
        if "Alpha" in name:
            meta.name = "Movie Alpha"
            meta.year = "2020"
        else:
            meta.name = "Movie Beta"
            meta.year = "2023"
        return meta

    monkeypatch.setattr("app.chain.transfer.MetaInfoPath", fake_meta_info_path)

    state, result = chain.do_transfer(
        fileitem=dir_item,
        background=False,
        manual=True,
        preview=True,
    )

    assert state is True
    assert result["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert recognized_keys == [("Movie Alpha", "2020"), ("Movie Beta", "2023")]
    assert [item["target"] for item in result["items"]] == [
        "/movies/电影甲 (2020).mkv",
        "/movies/电影乙 (2023).mkv",
    ]
