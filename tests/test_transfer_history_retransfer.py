from types import SimpleNamespace


from app.api.endpoints.transfer import (
    manual_transfer,
    match_manual_transfer_target_path,
    recommend_episode_format,
)
from app.schemas import EpisodeFormatRecommendItem, ManualTransferItem, TransferDirectoryConf


def test_manual_transfer_from_history_preserves_download_context(monkeypatch):
    """复用历史识别信息时应传递原下载上下文。"""
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={"storage": "local", "path": "/downloads/test.mkv", "name": "test.mkv", "type": "file"},
        dest_fileitem=None,
        downloader="qbittorrent",
        download_hash="abc123",
        type="电视剧",
        tmdbid="100",
        doubanid="200",
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="100",
        seasons="S01",
        episodes="E01-E02",
        episode_group="WEB-DL",
    )

    captured = {}

    def fake_get(_db, logid):
        assert logid == 1
        return history

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, from_history=True),
        background=True,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["downloader"] == "qbittorrent"
    assert captured["download_hash"] == "abc123"
    assert captured["episode_group"] == "WEB-DL"
    assert captured["season"] == 1


def test_manual_transfer_without_history_recognition_ignores_old_hash(monkeypatch):
    """从历史重新识别时应忽略旧下载上下文。"""
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem=None,
        downloader="qbittorrent",
        download_hash="polluted-hash",
        type="电视剧",
        tmdbid="100",
        doubanid="200",
        seasons="S01",
        episodes="E01",
        episode_group="WEB-DL",
    )
    captured = {}

    def fake_get(_db, logid):
        """返回受污染的历史记录。"""
        assert logid == 1
        return history

    class FakeTransferChain:
        """记录 API 传入整理链的参数。"""

        def manual_transfer(self, **kwargs):
            """记录手动整理参数。"""
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, from_history=False),
        background=True,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["downloader"] is None
    assert captured["download_hash"] is None
    assert captured["tmdbid"] is None
    assert captured["doubanid"] is None
    assert captured["episode_group"] is None


def test_manual_transfer_from_history_passes_old_dest_cleanup_to_chain(monkeypatch):
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem={
            "storage": "local",
            "path": "/library/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        downloader="qbittorrent",
        download_hash="abc123",
        type=None,
        tmdbid=None,
        doubanid=None,
        seasons=None,
        episodes=None,
        episode_group=None,
    )
    captured = {}

    def fake_get(_db, logid):
        assert logid == 1
        return history

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"].path == "/downloads/test.mkv"
    assert captured["cleanup_dest_fileitem"].path == "/library/test.mkv"


def test_manual_transfer_from_history_preview_does_not_cleanup_old_dest(monkeypatch):
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem={
            "storage": "local",
            "path": "/library/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        downloader="qbittorrent",
        download_hash="abc123",
        type=None,
        tmdbid=None,
        doubanid=None,
        seasons=None,
        episodes=None,
        episode_group=None,
    )
    captured = {}

    def fake_get(_db, logid):
        assert logid == 1
        return history

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, {
                "summary": {"total": 0, "success": 0, "failed": 0},
                "items": [],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, preview=True),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["cleanup_dest_fileitem"] is None


def test_manual_transfer_preview_uses_explicit_fileitems_instead_of_directory(monkeypatch):
    dir_item = {
        "storage": "local",
        "path": "/downloads/Test Show/",
        "name": "Test Show",
        "type": "dir",
    }
    file_paths = [
        "/downloads/Test Show/Test.Show.S01E01.mkv",
        "/downloads/Test Show/Test.Show.S01E02.mkv",
        "/downloads/Test Show/Test.Show.S01E03.mkv",
    ]
    selected_fileitems = [
        {
            "storage": "local",
            "path": file_path,
            "name": file_path.rsplit("/", 1)[-1],
            "type": "file",
        }
        for file_path in file_paths
    ]
    captured = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.append(kwargs)
            fileitem = kwargs["fileitem"]
            return True, {
                "summary": {"total": 1, "success": 1, "failed": 0},
                "items": [
                    {
                        "source": fileitem.path,
                        "target": f"/library/{fileitem.name}",
                        "target_dir": "/library",
                        "success": True,
                        "message": "",
                        "type": "电视剧",
                        "title": "Test Show (2026)",
                        "season": 1,
                        "episode": 1,
                        "episode_end": None,
                        "part": None,
                    }
                ],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=dir_item,
            fileitems=selected_fileitems,
            preview=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert len(captured) == 3
    assert [item["fileitem"].path for item in captured] == file_paths
    assert all(item["sync_extra_files"] is False for item in captured)
    assert resp.data["summary"] == {"total": 3, "success": 3, "failed": 0}
    assert [item["source"] for item in resp.data["items"]] == file_paths


def test_manual_transfer_preview_multi_select_collects_failures(monkeypatch):
    file_paths = [
        "/downloads/Test Show/Test.Show.S01E01.mkv",
        "/downloads/Test Show/Test.Show.S01E02.mkv",
    ]
    selected_fileitems = [
        {
            "storage": "local",
            "path": file_path,
            "name": file_path.rsplit("/", 1)[-1],
            "type": "file",
        }
        for file_path in file_paths
    ]

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            fileitem = kwargs["fileitem"]
            if fileitem.path.endswith("E02.mkv"):
                return False, f"{fileitem.name} 没有找到可整理的媒体文件"
            return True, {
                "summary": {"total": 1, "success": 1, "failed": 0},
                "items": [
                    {
                        "source": fileitem.path,
                        "target": f"/library/{fileitem.name}",
                        "target_dir": "/library",
                        "success": True,
                        "message": "",
                        "type": "电视剧",
                        "title": "Test Show (2026)",
                        "season": 1,
                        "episode": 1,
                        "episode_end": None,
                        "part": None,
                    }
                ],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(
            fileitems=selected_fileitems,
            preview=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["summary"] == {"total": 2, "success": 1, "failed": 1}
    assert [item["source"] for item in resp.data["items"]] == file_paths
    assert resp.data["items"][1]["success"] is False


def test_match_manual_transfer_target_path_returns_directory_match(monkeypatch):
    captured = {}

    class FakeDirectoryHelper:
        def get_dir(self, **kwargs):
            captured.update(kwargs)
            return TransferDirectoryConf(
                library_storage="rclone",
                library_path="/library/tv",
                transfer_type="copy",
                scraping=True,
                library_type_folder=True,
                library_category_folder=False,
            )

    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(
            fileitem={
                "storage": "local",
                "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
                "name": "Test.Show.S01E01.mkv",
                "type": "file",
            },
        ),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["storage"] == "local"
    assert captured["src_path"].as_posix() == "/downloads/Test Show/Test.Show.S01E01.mkv"
    assert captured["target_storage"] is None
    assert resp.data == {
        "target_storage": "rclone",
        "target_path": "/library/tv",
        "transfer_type": "copy",
        "scrape": True,
        "library_type_folder": True,
        "library_category_folder": False,
    }


def test_match_manual_transfer_target_path_returns_null_for_ambiguous_matches(monkeypatch):
    class FakeDirectoryHelper:
        def get_dir(self, **kwargs):
            src_path = kwargs["src_path"].as_posix()
            return TransferDirectoryConf(
                library_storage="local",
                library_path="/library/tv" if "E01" in src_path else "/library/movie",
                transfer_type="copy",
            )

    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(
            fileitems=[
                {
                    "storage": "local",
                    "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
                    "name": "Test.Show.S01E01.mkv",
                    "type": "file",
                },
                {
                    "storage": "local",
                    "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
                    "name": "Test.Show.S01E02.mkv",
                    "type": "file",
                },
            ],
        ),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["target_path"] is None
    assert resp.data["target_storage"] is None


def test_match_manual_transfer_target_path_accepts_multiple_history_records(monkeypatch):
    histories = {
        1: SimpleNamespace(
            status=0,
            mode="copy",
            src_fileitem={
                "storage": "local",
                "path": "/downloads/Show/Show.S01E01.mkv",
                "name": "Show.S01E01.mkv",
                "type": "file",
            },
        ),
        2: SimpleNamespace(
            status=0,
            mode="copy",
            src_fileitem={
                "storage": "local",
                "path": "/downloads/Show/Show.S01E02.mkv",
                "name": "Show.S01E02.mkv",
                "type": "file",
            },
        ),
    }

    def fake_get(_db, logid):
        return histories.get(logid)

    class FakeDirectoryHelper:
        def get_dir(self, **_kwargs):
            return TransferDirectoryConf(
                library_storage="local",
                library_path="/library/tv",
                transfer_type="copy",
            )

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(logids=[1, 2]),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["target_path"] == "/library/tv"


def test_recommend_episode_format_passes_selected_fileitems(monkeypatch):
    selected_fileitems = [
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
            "name": "Test.Show.S01E01.mkv",
            "type": "file",
        },
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
            "name": "Test.Show.S01E02.mkv",
            "type": "file",
        },
    ]
    captured = {}

    class FakeTransferChain:
        def recommend_episode_format(self, **kwargs):
            captured.update(kwargs)
            return True, "", {"episode_format": "Show.S01E{ep}.mkv"}

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = recommend_episode_format(
        recommend_item=EpisodeFormatRecommendItem(
            fileitem=selected_fileitems[0],
            fileitems=selected_fileitems,
        ),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"].path == selected_fileitems[0]["path"]
    assert [item.path for item in captured["fileitems"]] == [
        item["path"] for item in selected_fileitems
    ]


def test_recommend_episode_format_accepts_fileitems_without_fileitem(monkeypatch):
    selected_fileitems = [
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
            "name": "Test.Show.S01E01.mkv",
            "type": "file",
        },
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
            "name": "Test.Show.S01E02.mkv",
            "type": "file",
        },
    ]
    captured = {}

    class FakeTransferChain:
        def recommend_episode_format(self, **kwargs):
            captured.update(kwargs)
            return True, "", {"episode_format": "Show.S01E{ep}.mkv"}

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = recommend_episode_format(
        recommend_item=EpisodeFormatRecommendItem(
            fileitems=selected_fileitems,
        ),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"] is None
    assert [item.path for item in captured["fileitems"]] == [
        item["path"] for item in selected_fileitems
    ]


def _make_history(index: int, *, status=0, mode="copy", with_dest=False, **extra):
    """构造用于批量历史整理测试的历史记录。"""
    values = dict(
        status=status,
        mode=mode,
        src_fileitem={
            "storage": "local",
            "path": f"/downloads/Show.S01E0{index}.mkv",
            "name": f"Show.S01E0{index}.mkv",
            "type": "file",
        },
        dest_fileitem={
            "storage": "local",
            "path": f"/library/Show.S01E0{index}.mkv",
            "name": f"Show.S01E0{index}.mkv",
            "type": "file",
        }
        if with_dest
        else None,
        downloader=f"downloader-{index}",
        download_hash=f"hash-{index}",
        type="电视剧",
        tmdbid=str(100 + index),
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id=str(100 + index),
        seasons="S01",
        episodes=f"E0{index}",
        episode_group=None,
    )
    values.update(extra)
    return SimpleNamespace(**values)


def test_manual_transfer_logids_runs_each_history_with_own_context(monkeypatch):
    """多个历史ID应一次请求处理，并分别保留各自的识别与下载上下文。"""
    histories = {1: _make_history(1), 2: _make_history(2, episodes="E02-E03")}
    calls = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            calls.append(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2], from_history=True),
        background=True,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert [call["fileitem"].path for call in calls] == [
        "/downloads/Show.S01E01.mkv",
        "/downloads/Show.S01E02.mkv",
    ]
    assert [call["tmdbid"] for call in calls] == [101, 102]
    assert [call["download_hash"] for call in calls] == ["hash-1", "hash-2"]
    assert [call["downloader"] for call in calls] == ["downloader-1", "downloader-2"]
    assert [call["epformat"].detail for call in calls] == ["01", "2,3"]
    assert all(call["force"] is True for call in calls)
    assert all(call["sync_extra_files"] is True for call in calls)


def test_manual_transfer_logids_without_history_recognition_ignores_old_context(monkeypatch):
    """不复用历史识别信息时，批量历史整理不应带入旧识别与下载上下文。"""
    histories = {1: _make_history(1), 2: _make_history(2)}
    calls = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            calls.append(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2], reorganize=True),
        background=True,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert len(calls) == 2
    assert all(call["tmdbid"] is None for call in calls)
    assert all(call["downloader"] is None for call in calls)
    assert all(call["download_hash"] is None for call in calls)
    assert all(call["epformat"] is None for call in calls)
    assert all(call["reorganize"] is True for call in calls)


def test_manual_transfer_logids_uses_dest_for_moved_history_and_cleans_old_dest(monkeypatch):
    """成功移动的历史应以目的文件重新整理，其余历史在非重整时清理旧目的文件。"""
    histories = {
        1: _make_history(1, status=1, mode="move", with_dest=True),
        2: _make_history(2, with_dest=True),
    }
    calls = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            calls.append(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2]),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert calls[0]["fileitem"].path == "/library/Show.S01E01.mkv"
    assert calls[0]["cleanup_dest_fileitem"] is None
    assert calls[1]["fileitem"].path == "/downloads/Show.S01E02.mkv"
    assert calls[1]["cleanup_dest_fileitem"].path == "/library/Show.S01E02.mkv"


def test_manual_transfer_logids_preview_merges_results_and_failures(monkeypatch):
    """批量历史预览应合并各记录的预览项，并保留失败项。"""
    histories = {1: _make_history(1), 2: _make_history(2)}

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            fileitem = kwargs["fileitem"]
            if fileitem.path.endswith("E02.mkv"):
                return False, "没有找到可整理的媒体文件"
            return True, {
                "summary": {"total": 1, "success": 1, "failed": 0},
                "items": [{"source": fileitem.path, "success": True}],
                "message": "",
            }

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2], preview=True),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["summary"] == {"total": 2, "success": 1, "failed": 1}
    assert [item["source"] for item in resp.data["items"]] == [
        "/downloads/Show.S01E01.mkv",
        "/downloads/Show.S01E02.mkv",
    ]


def test_manual_transfer_logids_execution_failure_returns_error(monkeypatch):
    """批量历史执行中任一记录失败时应返回失败响应。"""
    histories = {1: _make_history(1), 2: _make_history(2)}

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            if kwargs["fileitem"].path.endswith("E02.mkv"):
                return False, "整理失败"
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2]),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is False
    assert resp.message == "整理失败"


def test_manual_transfer_logids_reports_missing_history(monkeypatch):
    """批量历史中存在无效ID时应返回标准失败响应且不执行整理。"""
    histories = {1: _make_history(1)}
    calls = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            calls.append(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 99]),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is False
    assert resp.message == "整理记录不存在，ID：99"
    assert calls == []


def test_manual_transfer_empty_logids_reports_missing_parameter(monkeypatch):
    """空 logids 且无其他来源时应返回缺少参数。"""
    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[]),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is False
    assert resp.message == "缺少参数"


def test_manual_transfer_logids_deduplicates_same_source(monkeypatch):
    """多个历史指向同一源文件时只整理一次。"""
    histories = {1: _make_history(1), 2: _make_history(1)}
    calls = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            calls.append(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda _db, logid: histories.get(logid),
    )
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logids=[1, 2]),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert len(calls) == 1
