import base64
from types import SimpleNamespace

from app import schemas
from app.api.endpoints import download as download_endpoint
from app.core.context import MediaInfo
from app.schemas.types import MediaType


def test_download_add_passes_generic_media_source(monkeypatch) -> None:
    """不含媒体信息的下载应按统一来源ID执行精确识别。"""
    captured = {}
    media = MediaInfo(
        anilist_info={
            "id": 154587,
            "title": {"english": "Frieren"},
            "format": "TV",
        }
    )

    class FakeMediaChain:
        """记录下载接口传入的媒体识别参数。"""

        def recognize_media(self, **kwargs):
            """返回固定媒体信息并保存识别参数。"""
            captured["recognize"] = kwargs
            return media

    class FakeDownloadChain:
        """模拟下载任务提交。"""

        def download_single(self, **kwargs):
            """保存下载上下文并返回任务ID。"""
            captured["download"] = kwargs
            return "download-1"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Frieren S01E01"),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["source"] == "anilist"
    assert captured["recognize"]["mediaid"] == "154587"
    assert captured["download"]["context"].media_info is media


def test_download_add_uses_selected_source_for_title_recognition(monkeypatch) -> None:
    """只选择来源而未填写ID时应在该来源内按标题识别。"""
    captured = {}
    media = MediaInfo(title="测试动画", type=MediaType.TV, bangumi_id=1)

    class FakeMediaChain:
        """记录按标题识别的请求级来源。"""

        def recognize_by_meta(self, metainfo, **kwargs):
            """返回固定媒体信息并保存来源。"""
            captured["metainfo"] = metainfo
            captured["kwargs"] = kwargs
            return media

    class FakeDownloadChain:
        """模拟下载任务提交。"""

        @staticmethod
        def download_single(**kwargs):
            """返回固定下载任务ID。"""
            return "download-2"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="测试动画 S01E01"),
        media_source="bangumi",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["kwargs"]["source"] == "bangumi"


def test_subtitle_download_passes_generic_media_source(monkeypatch) -> None:
    """字幕下载接口应把统一来源ID传递到下载链。"""
    captured = {}

    class FakeDownloadChain:
        """记录字幕下载参数。"""

        def download_subtitle(self, **kwargs):
            """保存参数并返回固定成功结果。"""
            captured.update(kwargs)
            return True, "字幕下载成功", ["/tmp/subtitle.ass"]

    monkeypatch.setattr(
        download_endpoint,
        "_prepare_subtitle_download",
        lambda _subtitle: (True, ""),
    )
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=schemas.SubtitleInfo(
            title="Frieren S01E01",
            enclosure="https://example.com/subtitle.ass",
        ),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["media_source"] == "anilist"
    assert captured["media_id"] == "154587"


class _UnrecognizedMediaChain:
    """模拟按标题识别失败的媒体链。"""

    def recognize_by_meta(self, metainfo, **kwargs):
        """始终返回未识别。"""
        return None

    def recognize_media(self, **kwargs):
        """始终返回未识别。"""
        return None


def _patch_unrecognized_download(monkeypatch, captured: dict, did="download-9"):
    """安装未识别媒体链与记录下载上下文的下载链。"""

    class FakeDownloadChain:
        """记录下载上下文并返回任务ID。"""

        def download_single(self, **kwargs):
            """保存下载参数。"""
            captured["download"] = kwargs
            return did

    monkeypatch.setattr(download_endpoint, "MediaChain", _UnrecognizedMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)


def test_download_add_requires_confirmation_when_unrecognized(monkeypatch) -> None:
    """未识别且未确认时只返回确认标记，不得创建下载任务。"""
    captured = {}
    _patch_unrecognized_download(monkeypatch, captured)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="未知合集 2020 1080p"),
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.data == {"requires_confirmation": True}
    assert "download" not in captured


def test_download_add_requires_confirmation_for_explicit_media_id(monkeypatch) -> None:
    """显式媒体ID识别失败同样需要确认，而不是直接失败。"""
    captured = {}
    _patch_unrecognized_download(monkeypatch, captured)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="未知合集 2020 1080p"),
        media_source="themoviedb",
        media_id="1",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.data == {"requires_confirmation": True}
    assert "download" not in captured


def test_download_add_downloads_unrecognized_after_confirmation(monkeypatch) -> None:
    """确认后使用同一种子、下载器与路径，基于标题解析结果构造最小媒体信息。"""
    captured = {}
    _patch_unrecognized_download(monkeypatch, captured)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            title="Unknown Movie 2020 1080p BluRay",
            enclosure="https://example.com/a.torrent",
        ),
        downloader="qb",
        save_path="/downloads/manual",
        allow_unrecognized=True,
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert response.data == {"download_id": "download-9"}
    download = captured["download"]
    context = download["context"]
    assert context.torrent_info.enclosure == "https://example.com/a.torrent"
    assert context.torrent_info.title == "Unknown Movie 2020 1080p BluRay"
    assert "Unknown Movie" in context.media_info.title
    assert context.media_info.year == "2020"
    assert context.media_info.type in (MediaType.MOVIE, MediaType.UNKNOWN)
    assert context.media_info.tmdb_id is None
    assert download["downloader"] == "qb"
    assert download["save_path"] == "/downloads/manual"
    assert download["source"] == "Manual"


def test_download_add_confirmed_tv_keeps_tv_type(monkeypatch) -> None:
    """标题明确为剧集时，最小媒体信息保留电视剧类型。"""
    captured = {}
    _patch_unrecognized_download(monkeypatch, captured)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Unknown Show S01E02 1080p"),
        allow_unrecognized=True,
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["download"]["context"].media_info.type == MediaType.TV


def test_download_add_confirmed_failure_has_no_confirmation_marker(monkeypatch) -> None:
    """确认后下载器失败属于普通业务失败，不再要求确认。"""
    captured = {}
    _patch_unrecognized_download(monkeypatch, captured, did=None)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Unknown Movie 2020"),
        allow_unrecognized=True,
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert response.message == "任务添加失败"
    assert not response.data or response.data.get("requires_confirmation") is not True


def test_download_add_recognized_failure_has_no_confirmation_marker(monkeypatch) -> None:
    """已识别资源的下载失败不得携带确认标记。"""

    class FakeMediaChain:
        """返回固定媒体信息。"""

        def recognize_by_meta(self, metainfo, **kwargs):
            """返回已识别媒体。"""
            return MediaInfo(title="测试电影", type=MediaType.MOVIE)

    class FakeDownloadChain:
        """模拟下载失败。"""

        def download_single(self, **kwargs):
            """返回空任务ID。"""
            return None

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="测试电影 2020"),
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is False
    assert not response.data or response.data.get("requires_confirmation") is not True


def _patch_manual_download(monkeypatch, captured: dict) -> None:
    """安装记录识别元数据与下载参数的模拟链。"""

    class FakeMediaChain:
        """返回固定剧集媒体信息。"""

        def recognize_by_meta(self, metainfo, **kwargs):
            """保存自动提取的识别标题。"""
            captured["metainfo"] = metainfo
            return MediaInfo(title="Frieren", type=MediaType.TV)

    class FakeDownloadChain:
        """记录手动下载任务参数。"""

        def download_single(self, **kwargs):
            """保存种子内容与保存路径。"""
            captured["download"] = kwargs
            return "download-manual"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)


def test_download_add_extracts_torrent_name_and_passes_content(monkeypatch) -> None:
    """上传种子文件时应提取内部名称与大小并直接传递文件内容。"""
    captured = {}
    _patch_manual_download(monkeypatch, captured)
    torrent_bytes = (
        b"d4:infod6:lengthi4e4:name20:Frieren.S01E01.1080p"
        b"12:piece lengthi16384e6:pieces20:01234567890123456789ee"
    )

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(
            torrent_content=base64.b64encode(torrent_bytes).decode("ascii")
        ),
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["metainfo"].org_string == "Frieren.S01E01.1080p"
    assert captured["download"]["torrent_content"] == torrent_bytes
    assert captured["download"]["save_path"] is None
    context = captured["download"]["context"]
    assert context.torrent_info.title == "Frieren.S01E01.1080p"
    assert context.torrent_info.size == 4.0


def test_download_add_extracts_magnet_display_name(monkeypatch) -> None:
    """磁力链接未指定标题时应使用 dn 参数识别媒体。"""
    captured = {}
    _patch_manual_download(monkeypatch, captured)
    magnet = (
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        "&dn=Frieren.S01E01.1080p"
    )

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(enclosure=magnet),
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["metainfo"].org_string == "Frieren.S01E01.1080p"
    assert captured["download"]["torrent_content"] is None
    assert captured["download"]["context"].torrent_info.enclosure == magnet
