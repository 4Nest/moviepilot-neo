# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import patch

from app.chain.transfer import JobManager, TransferChain
from app.core.config import settings
from app.schemas import EpisodeFormat, FileItem, TransferTask
from app.schemas.types import MediaType


class _FakeMeta:
    """构造最小可用的剧集元数据。"""

    def __init__(self, episode: int, season: int = 1):
        """初始化剧集编号相关字段。"""
        self.name = "Test Show"
        self.title = f"Test Show S{season:02d}E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = season
        self.end_season = None
        self.total_season = 1
        self.begin_episode = episode
        self.end_episode = None
        self.total_episode = 1
        self.episode_list = [episode]
        self.season_episode = f"S{season:02d}E{episode:02d}"
        self.part = None

    @property
    def season(self):
        """返回季字符串。"""
        return f"S{self.begin_season:02d}"

    @property
    def episode(self):
        """返回集字符串。"""
        return f"E{self.begin_episode:02d}"

    def to_dict(self):
        """返回元数据字典。"""
        return {
            "title": self.title,
            "name": self.name,
            "year": self.year,
            "type": self.type.value,
            "begin_season": self.begin_season,
            "end_season": self.end_season,
            "total_season": self.total_season,
            "begin_episode": self.begin_episode,
            "end_episode": self.end_episode,
            "total_episode": self.total_episode,
            "season_episode": self.season_episode,
            "episode_list": self.episode_list,
            "part": self.part,
        }


def _make_chain() -> TransferChain:
    """构造跳过初始化的 TransferChain，仅带作业视图。"""
    chain = object.__new__(TransferChain)
    chain.jobview = JobManager()
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._audio_exts + chain._subtitle_exts
    )
    chain._success_target_files = {}
    chain._scrape_batches = {}
    return chain


def _make_task(episode: int, download_hash: str, downloader: str) -> TransferTask:
    """构造带下载器信息的整理任务。"""
    name = f"Test.Show.S01E{episode:02d}.mkv"
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/Test Show/{name}",
            type="file",
            name=name,
            basename=name.removesuffix(".mkv"),
            extension="mkv",
            size=1024,
        ),
        meta=_FakeMeta(episode),
    )
    task.download_hash = download_hash
    task.downloader = downloader
    return task


def _mark(chain: TransferChain, download_hash: str, downloader: str):
    """调用私有的打标签方法。"""
    chain._TransferChain__mark_torrent_completed_if_done(download_hash, downloader)


def _finish_task(chain: TransferChain, task: TransferTask):
    """将任务登记并流转到完成状态。"""
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)
    chain.jobview.finish_task(task)


def test_mark_skips_tag_when_torrent_still_downloading():
    """种子未下载完成时（多集种子先完成单集），不得设置已整理标签（#6009）。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=52.3)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_mark_tags_when_torrent_completed():
    """种子已下载完成且任务全部结束时，正常设置已整理标签。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_mark_short_circuits_downloader_query_when_jobview_not_done():
    """作业视图还有未结束任务时，不应产生任何下载器查询。"""
    chain = _make_chain()
    queries = []
    chain.transfer_completed = lambda **kwargs: None
    chain.list_torrents = lambda **kwargs: queries.append(kwargs) or []
    task = _make_task(1, "hash1", "qbittorrent")
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)

    _mark(chain, "hash1", "qbittorrent")

    assert queries == []


def test_mark_skips_tag_when_torrent_not_found():
    """下载器中查不到种子时不打标签，留待定时轮询兜底。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: []
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_mark_skips_tag_when_list_torrents_raises():
    """查询下载器异常时不打标签且不向上抛出。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)

    def _raise(**_kwargs):
        raise RuntimeError("downloader unreachable")

    chain.list_torrents = _raise
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_multi_episode_torrent_tags_only_after_last_episode():
    """#6009 回归：E01 先整理完不打标签，种子整体下载完成后才打标签。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    progress = {"value": 60}
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=progress["value"])]

    # E01 下载完成并整理，此时种子整体仍在下载
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))
    _mark(chain, "hash1", "qbittorrent")
    assert completed == []

    # E02 下载完成并整理，种子整体到 100%
    progress["value"] = 100
    _finish_task(chain, _make_task(2, "hash1", "qbittorrent"))
    _mark(chain, "hash1", "qbittorrent")
    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_retransfer_passes_history_downloader_to_do_transfer(tmp_path):
    """#6909 边界 1：重整理入口应透传 history.downloader 给 do_transfer。"""
    chain = _make_chain()
    src_file = tmp_path / "Test.Show.S01E01.mkv"
    src_file.write_bytes(b"video")

    history = SimpleNamespace(
        id=42,
        src=src_file.as_posix(),
        src_fileitem={
            "storage": "local",
            "path": src_file.as_posix(),
            "type": "file",
            "name": src_file.name,
            "basename": src_file.stem,
            "extension": "mkv",
            "size": 5,
        },
        dest_fileitem=None,
        downloader="transmission",
        download_hash="hash-retransfer",
        episode_group=None,
    )
    captured = {}

    def _fake_do_transfer(**kwargs):
        captured.update(kwargs)
        return True, ""

    chain.do_transfer = _fake_do_transfer

    fake_media = SimpleNamespace(title_year="Test Show (2026)")
    fake_media_chain = SimpleNamespace(
        recognize_by_path=lambda *args, **kwargs: SimpleNamespace(media_info=fake_media)
    )

    with patch(
        "app.chain.transfer.TransferHistoryOper",
        return_value=SimpleNamespace(get=lambda logid: history),
    ), patch("app.chain.transfer.MediaChain", return_value=fake_media_chain):
        state, errmsg = chain.redo_transfer_history(42)

    assert state is True
    assert errmsg == ""
    assert captured["downloader"] == "transmission"
    assert captured["download_hash"] == "hash-retransfer"
    assert captured["force"] is True
    assert captured["manual"] is True


def _make_dir_fileitem(path: str = "/downloads/Test Show") -> FileItem:
    """构造测试用目录 FileItem。"""
    return FileItem(
        storage="local",
        path=path,
        type="dir",
        name="Test Show",
        basename="Test Show",
    )


def _make_video_fileitem(path: str) -> FileItem:
    """构造测试用视频 FileItem。"""
    name = path.rsplit("/", 1)[-1]
    return FileItem(
        storage="local",
        path=path,
        type="file",
        name=name,
        basename=name.removesuffix(".mkv"),
        extension="mkv",
        size=1024,
    )


def test_do_transfer_marks_completed_when_mixed_history_and_missing_meta_skip():
    """#6909 边界 2：部分命中历史跳过 + 部分无 file_meta 跳过导致 queued_count == 0 时补打已整理标签。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]

    file_a = _make_video_fileitem("/downloads/Test Show/Test.Show.S01E01.mkv")
    file_b = _make_video_fileitem("/downloads/Test Show/unrecognized.mkv")
    chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (file_a, False),
        (file_b, False),
    ]

    history_a = SimpleNamespace(
        status=True,
        download_hash="hash-mixed",
        downloader="qbittorrent",
    )
    transfer_history_oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: history_a if src == file_a.path else None
    )
    download_history_oper = SimpleNamespace(
        get_by_hash=lambda download_hash: None,
        get_by_path=lambda path: None,
        get_file_by_fullpath=lambda fullpath: None,
    )
    system_config_oper = SimpleNamespace(get=lambda key: None)

    with patch(
        "app.chain.transfer.TransferHistoryOper", return_value=transfer_history_oper
    ), patch(
        "app.chain.transfer.DownloadHistoryOper", return_value=download_history_oper
    ), patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch(
        "app.chain.transfer.WordsHelper.get_merged_words", return_value=[]
    ), patch(
        "app.chain.transfer.MetaInfoPath", return_value=None
    ):
        state, _ = chain.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-mixed",
            background=False,
        )

    assert state is False
    assert completed == [{"hashs": "hash-mixed", "downloader": "qbittorrent"}]


def test_do_transfer_missing_meta_skips_tag_when_incomplete_or_preview():
    """#6909 边界 2：无 file_meta 跳过未入队时，未下完或预览模式不得打已整理标签。"""
    file_b = _make_video_fileitem("/downloads/Test Show/unrecognized.mkv")
    transfer_history_oper = SimpleNamespace(get_by_src=lambda src, storage=None: None)
    download_history_oper = SimpleNamespace(
        get_by_hash=lambda download_hash: None,
        get_by_path=lambda path: None,
        get_file_by_fullpath=lambda fullpath: None,
    )
    system_config_oper = SimpleNamespace(get=lambda key: None)

    # 未下载完成（progress=50）不打标
    chain_incomplete = _make_chain()
    completed_incomplete = []
    chain_incomplete.transfer_completed = lambda **kwargs: completed_incomplete.append(kwargs)
    chain_incomplete.list_torrents = lambda **kwargs: [SimpleNamespace(progress=50)]
    chain_incomplete._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (file_b, False)
    ]

    with patch(
        "app.chain.transfer.TransferHistoryOper", return_value=transfer_history_oper
    ), patch(
        "app.chain.transfer.DownloadHistoryOper", return_value=download_history_oper
    ), patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch(
        "app.chain.transfer.WordsHelper.get_merged_words", return_value=[]
    ), patch(
        "app.chain.transfer.MetaInfoPath", return_value=None
    ):
        chain_incomplete.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-incomplete",
            background=False,
        )
    assert completed_incomplete == []

    # 预览模式不打标
    chain_preview = _make_chain()
    completed_preview = []
    chain_preview.transfer_completed = lambda **kwargs: completed_preview.append(kwargs)
    chain_preview.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    chain_preview._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (file_b, False)
    ]

    with patch(
        "app.chain.transfer.TransferHistoryOper", return_value=transfer_history_oper
    ), patch(
        "app.chain.transfer.DownloadHistoryOper", return_value=download_history_oper
    ), patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch(
        "app.chain.transfer.WordsHelper.get_merged_words", return_value=[]
    ), patch(
        "app.chain.transfer.MetaInfoPath", return_value=None
    ):
        chain_preview.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-preview",
            background=False,
            preview=True,
        )
    assert completed_preview == []


def test_do_transfer_marks_completed_when_no_valid_media_files():
    """#6909 边界 3：目录内无有效媒体文件直接返回时，已下完种子应补打标签，未下完与预览模式不打标。"""
    system_config_oper = SimpleNamespace(get=lambda key: None)

    # 1) 普通无有效媒体文件 + 已下载完成 -> 补打标
    chain_done = _make_chain()
    completed_done = []
    chain_done.transfer_completed = lambda **kwargs: completed_done.append(kwargs)
    chain_done.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    chain_done._TransferChain__get_trans_fileitems = lambda fileitem, predicate: []

    with patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch("app.chain.transfer.WordsHelper.get_merged_words", return_value=[]):
        state, _ = chain_done.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-empty-done",
            background=False,
        )
    assert state is False
    assert completed_done == [{"hashs": "hash-empty-done", "downloader": "qbittorrent"}]

    # 2) 普通无有效媒体文件 + 未下载完成 -> 不打标
    chain_downloading = _make_chain()
    completed_downloading = []
    chain_downloading.transfer_completed = lambda **kwargs: completed_downloading.append(kwargs)
    chain_downloading.list_torrents = lambda **kwargs: [SimpleNamespace(progress=60)]
    chain_downloading._TransferChain__get_trans_fileitems = lambda fileitem, predicate: []

    with patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch("app.chain.transfer.WordsHelper.get_merged_words", return_value=[]):
        chain_downloading.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-empty-downloading",
            background=False,
        )
    assert completed_downloading == []

    # 3) 预览模式 -> 不打标
    chain_preview = _make_chain()
    completed_preview = []
    chain_preview.transfer_completed = lambda **kwargs: completed_preview.append(kwargs)
    chain_preview.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    chain_preview._TransferChain__get_trans_fileitems = lambda fileitem, predicate: []

    with patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch("app.chain.transfer.WordsHelper.get_merged_words", return_value=[]):
        chain_preview.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-empty-preview",
            background=False,
            preview=True,
        )
    assert completed_preview == []

    # 4) 未匹配集数定位模板导致无有效媒体文件 + 已下载完成 -> 补打标
    chain_epformat = _make_chain()
    completed_epformat = []
    chain_epformat.transfer_completed = lambda **kwargs: completed_epformat.append(kwargs)
    chain_epformat.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    chain_epformat._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (_make_video_fileitem("/downloads/Test Show/Unmatched.mkv"), False)
    ]

    with patch(
        "app.chain.transfer.SystemConfigOper", return_value=system_config_oper
    ), patch("app.chain.transfer.WordsHelper.get_merged_words", return_value=[]):
        state, errmsg = chain_epformat.do_transfer(
            fileitem=_make_dir_fileitem(),
            downloader="qbittorrent",
            download_hash="hash-empty-epformat",
            epformat=EpisodeFormat(format="Episode-{ep}.mkv"),
            background=False,
        )
    assert state is True
    assert errmsg == ""
    assert completed_epformat == [
        {"hashs": "hash-empty-epformat", "downloader": "qbittorrent"}
    ]
