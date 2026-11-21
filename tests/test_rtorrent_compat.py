import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.modules import _DownloaderBase as ProductionDownloaderBase


def _load_rtorrent_modules():
    """
    使用轻量桩加载 rTorrent 客户端封装与模块，避免测试依赖完整应用启动。
    """
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    core_module = types.ModuleType("app.core")
    core_module.__path__ = []
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    modules_module = types.ModuleType("app.modules")
    modules_module.__path__ = []
    rtorrent_package_module = types.ModuleType("app.modules.rtorrent")
    rtorrent_package_module.__path__ = []
    log_module = types.ModuleType("app.log")
    cache_module = types.ModuleType("app.core.cache")
    config_module = types.ModuleType("app.core.config")
    metainfo_module = types.ModuleType("app.core.metainfo")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    string_module = types.ModuleType("app.utils.string")
    torrentool_module = types.ModuleType("torrentool")
    torrentool_module.__path__ = []
    torrentool_torrent_module = types.ModuleType("torrentool.torrent")

    class _Logger:
        """
        测试日志桩，仅提供被客户端封装调用的方法。
        """

        def info(self, *_args, **_kwargs):
            pass

        def warn(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    class _StringUtils:
        @staticmethod
        def is_magnet_link(value):
            if isinstance(value, bytes):
                return value.startswith(b"magnet:")
            return isinstance(value, str) and value.startswith("magnet:")

        @staticmethod
        def generate_random_str(_length):
            return "tmp-tag-01"

        @staticmethod
        def str_filesize(value):
            return str(value)

        @staticmethod
        def str_secends(value):
            return str(value)

    class _FileCache:
        def get(self, *_args, **_kwargs):
            return None

    class _MetaInfo:
        def __init__(self, name):
            self.name = name
            self.year = None
            self.season_episode = ""
            self.episode_list = []

    class _ModuleBase:
        pass

    class _DownloaderBase:
        _discard_failed_selection = staticmethod(ProductionDownloaderBase._discard_failed_selection)

        def __class_getitem__(cls, _item):
            return cls

    class _Torrent:
        @staticmethod
        def from_string(content):
            return types.SimpleNamespace(name="test", total_size=len(content))

    class _DownloaderTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TorrentStatus(Enum):
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"

    class TorrentQueryStatus(Enum):
        ALL = "all"
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"
        COMPLETED = "completed"
        PAUSED = "paused"

    class DownloadTaskState(Enum):
        DOWNLOADING = "downloading"
        PAUSED = "paused"
        COMPLETED = "completed"

    class ModuleType(Enum):
        Downloader = "Downloader"

    class DownloaderType(Enum):
        Rtorrent = "Rtorrent"

    log_module.logger = _Logger()
    cache_module.FileCache = _FileCache
    config_module.settings = types.SimpleNamespace(
        TORRENT_TAG="moviepilot-tag",
        RMT_MEDIAEXT=[".mkv", ".mp4"],
    )
    metainfo_module.MetaInfo = _MetaInfo
    schemas_module.DownloaderInfo = object
    schemas_module.DownloaderTorrent = _DownloaderTorrent
    schema_types_module.TorrentStatus = TorrentStatus
    schema_types_module.TorrentQueryStatus = TorrentQueryStatus
    schema_types_module.DownloadTaskState = DownloadTaskState
    schema_types_module.ModuleType = ModuleType
    schema_types_module.DownloaderType = DownloaderType
    string_module.StringUtils = _StringUtils
    modules_module._ModuleBase = _ModuleBase
    modules_module._DownloaderBase = _DownloaderBase
    torrentool_torrent_module.Torrent = _Torrent

    from app.helper.torrent import EpisodeSelection, select_torrent_files

    helper_module = types.ModuleType("app.helper")
    helper_torrent_module = types.ModuleType("app.helper.torrent")
    helper_torrent_module.EpisodeSelection = EpisodeSelection
    helper_torrent_module.select_torrent_files = select_torrent_files
    helper_module.torrent = helper_torrent_module

    app_module.core = core_module
    app_module.helper = helper_module
    app_module.log = log_module
    app_module.modules = modules_module
    app_module.schemas = schemas_module
    app_module.utils = utils_module
    core_module.cache = cache_module
    core_module.config = config_module
    core_module.metainfo = metainfo_module
    utils_module.string = string_module
    schemas_module.types = schema_types_module
    modules_module.rtorrent = rtorrent_package_module
    torrentool_module.torrent = torrentool_torrent_module

    stub_modules = {
        "app": app_module,
        "app.core": core_module,
        "app.core.cache": cache_module,
        "app.core.config": config_module,
        "app.core.metainfo": metainfo_module,
        "app.helper": helper_module,
        "app.helper.torrent": helper_torrent_module,
        "app.log": log_module,
        "app.modules": modules_module,
        "app.modules.rtorrent": rtorrent_package_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
        "app.utils": utils_module,
        "app.utils.string": string_module,
        "torrentool": torrentool_module,
        "torrentool.torrent": torrentool_torrent_module,
    }

    rtorrent_path = repo_root / "app" / "modules" / "rtorrent" / "rtorrent.py"
    rtorrent_spec = importlib.util.spec_from_file_location(
        "app.modules.rtorrent.rtorrent",
        rtorrent_path,
    )
    rtorrent_module = importlib.util.module_from_spec(rtorrent_spec)
    assert rtorrent_spec and rtorrent_spec.loader

    module_path = repo_root / "app" / "modules" / "rtorrent" / "__init__.py"
    rtorrent_module_spec = importlib.util.spec_from_file_location(
        "_test_rtorrent_module",
        module_path,
    )
    module_package = importlib.util.module_from_spec(rtorrent_module_spec)
    assert rtorrent_module_spec and rtorrent_module_spec.loader

    with patch.dict(sys.modules, stub_modules):
        sys.modules[rtorrent_spec.name] = rtorrent_module
        rtorrent_spec.loader.exec_module(rtorrent_module)
        rtorrent_package_module.rtorrent = rtorrent_module
        rtorrent_module_spec.loader.exec_module(module_package)

    return rtorrent_module, module_package


rtorrent_module, rtorrent_package_module = _load_rtorrent_modules()
Rtorrent = rtorrent_module.Rtorrent
RtorrentModule = rtorrent_package_module.RtorrentModule


def test_change_torrent_sets_per_task_speed_limits():
    """
    rTorrent 单任务限速应创建限速组并绑定到任务。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.change_torrent(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        download_limit=1024,
        upload_limit=512,
    )

    fake_proxy.throttle.down.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        1024 * 1024,
    )
    fake_proxy.throttle.up.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        512 * 1024,
    )
    fake_proxy.d.throttle_name.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "mp_abcdef1234567890",
    )


def test_change_torrent_allows_zero_limit_to_disable_limit():
    """
    rTorrent 单任务限速传 0 时应写入 0，表示关闭对应限速。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.change_torrent(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        download_limit=0,
    )

    fake_proxy.throttle.down.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        0,
    )
    fake_proxy.throttle.up.max.set.assert_not_called()
    fake_proxy.d.throttle_name.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "mp_abcdef1234567890",
    )


def test_set_torrent_location_updates_directory():
    """
    rTorrent 保存目录修改应调用 d.directory.set。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.set_torrent_location(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        location="/downloads/new",
    )

    fake_proxy.d.directory.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "/downloads/new",
    )


def _build_rtorrent_module(server):
    """构造仅包含下载所需方法的 RtorrentModule 测试实例。"""
    module = RtorrentModule.__new__(RtorrentModule)
    module.get_instance = MagicMock(return_value=server)
    module.normalize_path = MagicMock(side_effect=lambda path, _downloader: str(path))
    module.get_default_config_name = MagicMock(return_value="default-rt")
    return module


def test_rtorrent_download_with_info_hash_skips_temp_tag():
    """rTorrent 添加自带 info_hash 的种子时不应生成随机临时标签。"""
    expected_hash = "abcdef0123456789abcdef0123456789abcdef01"
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = True

    module = _build_rtorrent_module(fake_server)
    torrent_with_hash = types.SimpleNamespace(
        name="Show.S01",
        total_size=1024,
        info_hash=expected_hash.upper(),
    )
    with patch.object(rtorrent_package_module.Torrent, "from_string", return_value=torrent_with_hash):
        result = module.download(
            content=b"torrent-bytes",
            download_dir=Path("/downloads"),
            cookie="",
            downloader="rt",
        )

    assert result == ("rt", expected_hash, "Original", "添加下载成功")
    assert fake_server.add_torrent.call_args.kwargs["tags"] == ["moviepilot-tag"]
    fake_server.get_torrent_id_by_tag.assert_not_called()


@pytest.mark.parametrize(
    ("magnet", "expected_hash"),
    [
        (
            "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Test",
            "abcdef0123456789abcdef0123456789abcdef01",
        ),
        (
            "magnet:?xt=urn:btih:VPU66AJFIVP66AJFIVP66AJFIVP66AJF&dn=Base32",
            "abe9ef0125455fef0125455fef0125455fef0125",
        ),
        (
            "magnet:?xt=urn:btmh:1220FEDCBA9876543210FEDCBA9876543210FEDCBA9876543210FEDCBA9876543210&dn=V2",
            "fedcba9876543210fedcba9876543210fedcba98",
        ),
    ],
)
def test_rtorrent_download_magnet_skips_temp_tag(magnet, expected_hash):
    """rTorrent 添加可解析 Hash 的磁力链接时不应生成随机临时标签。"""
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = True

    module = _build_rtorrent_module(fake_server)
    result = module.download(
        content=magnet,
        download_dir=Path("/downloads"),
        cookie="",
        label=" custom-a , custom-b ",
        downloader="rt",
    )

    assert result == ("rt", expected_hash, "Original", "添加下载成功")
    assert fake_server.add_torrent.call_args.kwargs["tags"] == ["custom-a", "custom-b"]
    fake_server.get_torrent_id_by_tag.assert_not_called()


def test_rtorrent_download_unknown_hash_falls_back_to_temp_tag():
    """rTorrent 无法预知 Hash 时应回退使用临时标签反查任务 ID。"""
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = True
    fake_server.get_torrent_id_by_tag.return_value = "fallback-rt-hash"

    module = _build_rtorrent_module(fake_server)
    result = module.download(
        content="magnet:?dn=missing-xt",
        download_dir=Path("/downloads"),
        cookie="",
        downloader="rt",
    )

    assert result == ("rt", "fallback-rt-hash", "Original", "添加下载成功")
    assert fake_server.add_torrent.call_args.kwargs["tags"] == ["tmp-tag-01", "moviepilot-tag"]
    fake_server.get_torrent_id_by_tag.assert_called_once_with(tags="tmp-tag-01")


def test_rtorrent_existing_torrent_appends_new_episodes_without_disabling_existing():
    """rTorrent 已有整季包追加新集数时应仅启用未下载目标文件并启动任务，不关闭已有文件。"""
    expected_hash = "abcdef0123456789abcdef0123456789abcdef01"
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = False
    fake_server.set_files.return_value = True
    fake_server.start_torrents.return_value = True
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag,已整理",
    }], False)
    fake_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv", "priority": 1},
        {"id": 1, "name": "Show.S01E02.mkv", "priority": 1},
        {"id": 2, "name": "Show.S01E03.mkv", "priority": 0},
        {"id": 3, "name": "Show.S01E04.mkv", "priority": 0},
    ]

    module = _build_rtorrent_module(fake_server)
    result = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={3},
        downloader="rt",
    )

    assert result == ("rt", expected_hash, "Original", "下载任务已存在，已追加选择集数：[3]")
    fake_server.set_files.assert_called_once_with(
        torrent_hash=expected_hash, file_ids=[2], priority=1
    )
    fake_server.start_torrents.assert_called_once_with(expected_hash)
    fake_server.remove_torrents_tag.assert_called_once_with(
        ids=expected_hash, tag=["已整理"]
    )
    fake_server.stop_torrents.assert_not_called()
    fake_server.delete_torrents.assert_not_called()


def test_rtorrent_existing_torrent_without_episodes_or_already_enabled_keeps_compat():
    """rTorrent 未传 episodes 或目标集文件已启用时保持原有安全兼容行为。"""
    expected_hash = "1234567890abcdef1234567890abcdef12345678"
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = False
    module = _build_rtorrent_module(fake_server)

    # 1. 未传 episodes
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag",
    }], False)
    res_whole = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes=None,
        downloader="rt",
    )
    assert res_whole == ("rt", expected_hash, "Original", "下载任务已存在")
    fake_server.get_files.assert_not_called()
    fake_server.set_files.assert_not_called()

    # 2. 目标集文件本就已启用
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag",
    }], False)
    fake_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv", "priority": 1},
        {"id": 1, "name": "Show.S01E02.mkv", "priority": 0},
    ]
    res_enabled = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={1},
        downloader="rt",
    )
    assert res_enabled == ("rt", expected_hash, "Original", "下载任务已存在")
    fake_server.set_files.assert_not_called()
    fake_server.start_torrents.assert_not_called()


def test_rtorrent_existing_torrent_append_failures_return_error_without_deleting():
    """rTorrent 已有任务追加选集在目标集缺失、优先级设置失败或启动失败时应报错且不删任务。"""
    expected_hash = "fedcba9876543210fedcba9876543210fedcba98"
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = False
    module = _build_rtorrent_module(fake_server)

    # 1. 目标集缺失
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag,已整理",
    }], False)
    fake_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv", "priority": 1},
        {"id": 1, "name": "Show.S01E02.mkv", "priority": 0},
    ]
    res_missing = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={9},
        downloader="rt",
    )
    assert res_missing[:3] == (None, None, None)
    assert "未找到目标集数" in res_missing[3]
    fake_server.remove_torrents_tag.assert_not_called()
    fake_server.delete_torrents.assert_not_called()

    # 2. 优先级设置失败
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag",
    }], False)
    fake_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv", "priority": 1},
        {"id": 1, "name": "Show.S01E02.mkv", "priority": 0},
    ]
    fake_server.set_files.return_value = False
    res_set_fail = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={2},
        downloader="rt",
    )
    assert res_set_fail[:3] == (None, None, None)
    assert "设置选集优先级失败" in res_set_fail[3]
    fake_server.start_torrents.assert_not_called()
    fake_server.delete_torrents.assert_not_called()

    # 3. 启动任务失败
    fake_server.get_torrents.return_value = ([{
        "name": "Show.S01",
        "total_size": 2048,
        "hash": expected_hash,
        "tags": "moviepilot-tag",
    }], False)
    fake_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv", "priority": 1},
        {"id": 1, "name": "Show.S01E02.mkv", "priority": 0},
    ]
    fake_server.set_files.return_value = True
    fake_server.start_torrents.return_value = False
    res_start_fail = module.download(
        content=f"magnet:?xt=urn:btih:{expected_hash}",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={2},
        downloader="rt",
    )
    assert res_start_fail[:3] == (None, None, None)
    assert "启动任务未确认" in res_start_fail[3]
    fake_server.stop_torrents.assert_not_called()
    fake_server.delete_torrents.assert_not_called()
