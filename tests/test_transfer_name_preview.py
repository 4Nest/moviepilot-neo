"""
整理名称与目标目录预览接口及推荐名称扩展名过滤回归测试。
"""

from pathlib import Path
from typing import Any, Optional

import pytest

from app.api.endpoints import transfer as transfer_endpoint
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.directory import DirectoryHelper, build_media_library_path
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType


def _configure_rename_formats(
    monkeypatch: pytest.MonkeyPatch,
    *,
    movie_format: str = "{{title}}{% if year %} ({{year}}){% endif %}{% if tmdbid %} {tmdbid={{tmdbid}}}{% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}",
    tv_format: str = "{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - {{season_episode}}{{fileExt}}",
) -> None:
    """
    通过真实配置字段设置测试用重命名模板。
    """
    monkeypatch.setattr(transfer_endpoint.settings, "MOVIE_RENAME_FORMAT", movie_format)
    monkeypatch.setattr(transfer_endpoint.settings, "TV_RENAME_FORMAT", tv_format)


def test_recommend_name_ignores_non_media_suffix_from_torrent_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    不带媒体文件后缀的种子名不应把点分发布标签误拼成文件扩展名。
    """
    monkeypatch.setattr(
        "app.modules.filemanager.settings.MOVIE_RENAME_FORMAT",
        "{{title}} ({{year}})/{{title}} ({{year}}){{fileExt}}",
    )
    monkeypatch.setattr("app.modules.filemanager.settings.RMT_MEDIAEXT", [".mkv", ".mp4"])
    monkeypatch.setattr("app.modules.filemanager.settings.RMT_AUDIOEXT", [".flac"])
    monkeypatch.setattr("app.modules.filemanager.settings.RMT_SUBEXT", [".srt"])
    captured_exts: list[Optional[str]] = []

    def _fake_naming_dict(
        *,
        meta: Any,
        mediainfo: Any,
        episodes_info: Any = None,
        file_ext: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        记录传入命名上下文的文件扩展名并返回最小模板变量。
        """
        _ = (meta, episodes_info)
        captured_exts.append(file_ext)
        return {
            "title": mediainfo.title,
            "year": mediainfo.year,
            "fileExt": file_ext or "",
        }

    monkeypatch.setattr(TransHandler, "get_naming_dict", staticmethod(_fake_naming_dict))
    monkeypatch.setattr(
        TransHandler,
        "get_rename_path",
        staticmethod(
            lambda template_string, rename_dict, **_kwargs: Path(
                f"{rename_dict['title']} ({rename_dict['year']})/"
                f"{rename_dict['title']} ({rename_dict['year']}){rename_dict['fileExt']}"
            )
        ),
    )

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.year = "1998"

    torrent_meta = MetaInfo("The.Legend.of.1900.1998.1080p.BluRay.x264-GROUP")
    torrent_result = FileManagerModule.recommend_name(torrent_meta, mediainfo)
    assert captured_exts[-1] == ""
    assert torrent_result == "海上钢琴师 (1998)/海上钢琴师 (1998)"

    file_meta = MetaInfo("The.Legend.of.1900.1998.1080p.BluRay.mkv")
    file_result = FileManagerModule.recommend_name(file_meta, mediainfo)
    assert captured_exts[-1] == ".mkv"
    assert file_result == "海上钢琴师 (1998)/海上钢琴师 (1998).mkv"


def test_build_media_library_path_matches_transhandler_dest_dir() -> None:
    """
    共享媒体库路径构造函数应正确拼接类型与二级分类目录，并与整理处理器保持一致。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.category = "外语电影"

    directory = TransferDirectoryConf(
        name="电影库",
        library_path="/media",
        library_type_folder=True,
        library_category_folder=True,
    )

    expected = Path("/media/电影/外语电影")
    assert build_media_library_path(directory, mediainfo) == expected
    assert TransHandler.get_dest_dir(mediainfo, directory) == expected

    empty_dir = TransferDirectoryConf(name="无路径")
    with pytest.raises(ValueError, match="媒体库目录路径为空"):
        build_media_library_path(empty_dir, mediainfo)


def test_preview_name_returns_category_dir_file_and_library_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    预览接口应返回命中的分类路径、目标目录名、最终文件名以及完整目标路径。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.year = "1998"
    mediainfo.tmdb_id = 10376
    mediainfo.source = "themoviedb"
    mediainfo.category = "外语电影"

    captured_recognize: dict[str, Any] = {}

    def _fake_recognize_media(
        self: Any,
        *,
        meta: Any,
        mtype: Any = None,
        source: Any = None,
        mediaid: Any = None,
    ) -> MediaInfo:
        """
        捕获识别调用参数并返回固定电影信息。
        """
        _ = self
        captured_recognize.update(
            {
                "meta": meta,
                "mtype": mtype,
                "source": source,
                "mediaid": mediaid,
            }
        )
        return mediainfo

    _configure_rename_formats(monkeypatch)
    monkeypatch.setattr(transfer_endpoint.settings, "RMT_MEDIAEXT", [".mkv", ".mp4"])
    monkeypatch.setattr(transfer_endpoint.settings, "RMT_AUDIOEXT", [".flac"])
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "recognize_media",
        _fake_recognize_media,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "get_dir",
        lambda self, media=None, src_path=None: TransferDirectoryConf(
            name="电影库",
            library_path="/media",
            library_type_folder=True,
            library_category_folder=True,
        ),
    )

    response = transfer_endpoint.preview_name(
        title="/downloads/The.Legend.of.1900.1998.1080p.mkv",
        subtitle="海上钢琴师",
        custom_words="屏蔽词",
        media_source="tmdb",
        media_id="10376",
        mtype=MediaType.MOVIE,
    )

    assert response.success is True
    assert captured_recognize["source"] == "themoviedb"
    assert captured_recognize["mediaid"] == "10376"
    assert captured_recognize["mtype"] == MediaType.MOVIE
    assert response.data is not None
    assert response.data.category == "外语电影"
    assert response.data.target_dir == "海上钢琴师 (1998) {tmdbid=10376}"
    assert response.data.file_name == "海上钢琴师 (1998) - 1080p.mkv"
    assert response.data.rename_path == "海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv"
    assert response.data.library_path == "/media/电影/外语电影"
    assert (
        response.data.full_path
        == "/media/电影/外语电影/海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv"
    )


def test_preview_name_handles_tv_season_and_no_configured_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    电视剧预览应保留季目录层级；未配置匹配整理目录时允许媒体库路径为空。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.TV
    mediainfo.title = "测试剧集"
    mediainfo.year = "2026"
    mediainfo.category = "日漫"

    def _fake_recognize_by_meta(
        self: Any,
        metainfo: Any,
        *,
        source: Any = None,
    ) -> MediaInfo:
        """
        返回测试电视剧信息。
        """
        _ = (self, metainfo, source)
        return mediainfo

    _configure_rename_formats(monkeypatch)
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "recognize_by_meta",
        _fake_recognize_by_meta,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "测试剧集 (2026)/Season 1/测试剧集 - S01E01",
    )
    monkeypatch.setattr(DirectoryHelper, "get_dir", lambda self, media=None, src_path=None: None)

    response = transfer_endpoint.preview_name(title="Test.Show.S01E01.1080p")

    assert response.success is True
    assert response.data is not None
    assert response.data.category == "日漫"
    assert response.data.target_dir == "测试剧集 (2026)"
    assert response.data.file_name == "测试剧集 - S01E01"
    assert response.data.rename_path == "测试剧集 (2026)/Season 1/测试剧集 - S01E01"
    assert response.data.library_path is None
    assert response.data.full_path is None


def test_preview_name_non_tmdb_source_supplements_missing_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    非 TMDB 来源带原生 ID 识别且缺少分类时，应补充 TMDB 二级分类。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.TV
    mediainfo.title = "葬送的芙莉莲"
    mediainfo.year = "2023"
    mediainfo.source = "bangumi"
    mediainfo.category = None

    def _fake_supplement(self: Any, media: MediaInfo, _meta: Any) -> MediaInfo:
        _ = self
        media.category = "动漫"
        return media

    _configure_rename_formats(monkeypatch)
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "recognize_media",
        lambda self, **_kwargs: mediainfo,
    )
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "supplement_tmdb_info",
        _fake_supplement,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "葬送的芙莉莲 (2023)/Season 1/葬送的芙莉莲 - S01E01.mkv",
    )
    monkeypatch.setattr(DirectoryHelper, "get_dir", lambda self, media=None, src_path=None: None)

    response = transfer_endpoint.preview_name(
        title="Frieren.S01E01.1080p.mkv",
        media_source="bangumi",
        media_id="400602",
        mtype=MediaType.TV,
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.category == "动漫"
    assert response.data.target_dir == "葬送的芙莉莲 (2023)"


def test_preview_name_reports_unrecognized_and_template_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    未识别媒体或模板渲染失败时应返回结构化失败响应，而非抛出未处理异常。
    """

    def _fake_unrecognized(self: Any, metainfo: Any, **_kwargs: Any) -> None:
        """
        模拟识别失败返回空媒体信息。
        """
        _ = (self, metainfo)
        return None

    _configure_rename_formats(monkeypatch)
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "recognize_by_meta",
        _fake_unrecognized,
    )

    unrecognized_response = transfer_endpoint.preview_name(title="Unknown.Release")
    assert unrecognized_response.success is False
    assert "未识别到媒体信息" in (unrecognized_response.message or "")

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"

    def _fake_recognized(self: Any, metainfo: Any, **_kwargs: Any) -> MediaInfo:
        """
        模拟识别成功以验证模板渲染失败路径。
        """
        _ = (self, metainfo)
        return mediainfo

    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "recognize_by_meta",
        _fake_recognized,
    )

    def _raise_template_error(self: Any, meta: Any, mediainfo: Any) -> str:
        """
        模拟 Jinja2 模板语法错误。
        """
        _ = (self, meta, mediainfo)
        raise ValueError("模板语法错误")

    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        _raise_template_error,
    )

    error_response = transfer_endpoint.preview_name(title="海上钢琴师.1998.mkv")
    assert error_response.success is False
    assert "重命名模板渲染失败" in (error_response.message or "")
