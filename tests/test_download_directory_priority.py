"""自动下载目录按用户优先级选择的离线回归测试。"""

from pathlib import Path

from app.chain.download import DownloadChain
from app.core.context import MediaInfo
from app.helper.directory import DirectoryHelper
from app.schemas import TransferDirectoryConf
from app.schemas.types import MediaType


def test_default_download_dir_uses_priority_before_match_specificity(monkeypatch):
    """更精确的类型分类不得盖过具有下载路径的高优先级通用目录。"""
    general = TransferDirectoryConf(
        name="通用目录",
        priority=1,
        storage="local",
        download_path="/downloads/general",
    )
    specific = TransferDirectoryConf(
        name="剧集目录",
        priority=2,
        storage="local",
        download_path="/downloads/tv",
        media_type=MediaType.TV.value,
    )
    unavailable = TransferDirectoryConf(
        name="仅媒体库",
        priority=0,
        library_path="/library",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [specific, unavailable, general]),
    )
    media = MediaInfo(title="测试剧", type=MediaType.TV)

    assert DirectoryHelper().get_download_dir(media) is general
    assert DownloadChain._resolve_media_download_dir(media) == (
        "local",
        Path("/downloads/general"),
        "",
    )


def test_default_download_dir_preserves_order_for_equal_priority(monkeypatch):
    """相同优先级应保持用户配置顺序。"""
    first = TransferDirectoryConf(download_path="/downloads/first")
    second = TransferDirectoryConf(
        download_path="/downloads/second",
        media_type=MediaType.TV.value,
    )
    monkeypatch.setattr(DirectoryHelper, "get_dirs", staticmethod(lambda: [first, second]))

    assert DirectoryHelper().get_download_dir(MediaInfo(type=MediaType.TV)) is first
