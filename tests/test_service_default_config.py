"""
下载器默认配置选择与缺失实例诊断的回归测试。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain.download import DownloadChain
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.meta.metabase import MetaBase
from app.helper.service import ServiceConfigHelper
from app.modules import _DownloaderBase
from app.schemas.system import DownloaderConf
from app.schemas.types import MediaType


def _conf(name: str, *, default: bool = False, enabled: bool = True) -> DownloaderConf:
    """构造一份下载器配置。"""
    return DownloaderConf(
        name=name,
        type="qbittorrent",
        enabled=enabled,
        default=default,
        config={},
    )


def _downloader_service(
    monkeypatch: pytest.MonkeyPatch,
    configs: list[DownloaderConf],
) -> _DownloaderBase[object]:
    """构造隔离了全局配置来源的下载器基类实例，只注入已启用配置的运行实例。"""
    monkeypatch.setattr(ServiceConfigHelper, "get_downloader_configs", staticmethod(lambda: configs))
    service: _DownloaderBase[object] = _DownloaderBase()
    service._service_name = "qbittorrent"
    service._configs = {conf.name: conf for conf in configs if conf.enabled}
    service._instances = {conf.name: f"instance:{conf.name}" for conf in configs if conf.enabled}
    return service


def test_downloader_default_skips_disabled_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """已禁用的下载器即使仍带有 default 标记，也不得盖过已启用的默认下载器。"""
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-default", default=True, enabled=False),
            _conf("backup-qb", enabled=True),
            _conf("active-qb", default=True, enabled=True),
        ],
    )

    assert service.get_default_config_name() == "active-qb"
    assert service.get_instance() == "instance:active-qb"


def test_downloader_default_falls_back_to_first_enabled_when_only_disabled_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅已禁用的下载器带有 default 标记且其余已启用项均未标记时，默认下载器应落到首个已启用配置。"""
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-default", default=True, enabled=False),
            _conf("first-enabled", enabled=True),
            _conf("second-enabled", enabled=True),
        ],
    )

    assert service.get_default_config_name() == "first-enabled"
    assert service.get_instance() == "instance:first-enabled"


def test_downloader_default_skips_disabled_first_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有默认标记且首个配置已禁用时，默认下载器应落到首个已启用配置。"""
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-first", enabled=False),
            _conf("second-enabled", enabled=True),
        ],
    )

    assert service.get_default_config_name() == "second-enabled"
    assert service.get_instance() == "instance:second-enabled"


def test_downloader_default_returns_none_when_all_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有下载器都已禁用时，默认配置名称与默认实例均应返回 None。"""
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-only", default=True, enabled=False),
        ],
    )

    assert service.get_default_config_name() is None
    assert service.get_instance() is None


def _build_download_context(site_downloader: str | None = None) -> Context:
    """构造最小下载上下文。"""
    meta = MetaBase(title="Example.S01E01")
    media = MediaInfo(type=MediaType.TV, title="Example", year="2026", tmdb_id=1)
    torrent = TorrentInfo(
        title="Example.S01E01.1080p",
        enclosure="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        site_name="测试站点",
        site_downloader=site_downloader,
    )
    return Context(meta_info=meta, media_info=media, torrent_info=torrent)


def _prepare_chain(monkeypatch: pytest.MonkeyPatch, download_return):
    """构造隔离外部依赖的 DownloadChain 实例。"""
    chain = DownloadChain.__new__(DownloadChain)
    chain.messagehelper = Mock()
    chain.eventmanager = Mock()
    chain.download = Mock(return_value=download_return)
    chain.post_message = Mock()
    chain._record_download_failure = Mock()
    chain._resolve_bt_default_config = Mock(return_value=None)
    chain._resolve_media_download_dir = Mock(return_value=("local", Path("/downloads"), ""))

    monkeypatch.setattr(
        "app.chain.download.MediaChain.supplement_tmdb_info",
        lambda _self, media, _meta: media,
    )
    monkeypatch.setattr(
        "app.chain.download.eventmanager.send_event",
        lambda *_args, **_kwargs: SimpleNamespace(event_data=None),
    )
    monkeypatch.setattr(
        "app.chain.download.TorrentHelper.get_fileinfo_from_torrent_content",
        lambda _self, _content: ("Example", ["Example.S01E01.mkv"]),
    )
    return chain


@pytest.mark.parametrize(
    ("downloader_arg", "site_downloader", "expected_fragments"),
    [
        ("stale-qb", None, ("未找到可用下载器实例", "stale-qb")),
        (None, "site-qb", ("未找到站点", "测试站点", "site-qb")),
        (None, None, ("未找到可用的默认下载器实例",)),
    ],
    ids=["explicit-downloader", "site-downloader", "default-downloader"],
)
def test_missing_downloader_reports_target_without_recording_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    downloader_arg: str | None,
    site_downloader: str | None,
    expected_fragments: tuple[str, ...],
) -> None:
    """下载器模块均未匹配到可用实例时，必须明确指明目标且不写入失败冷却台账。"""
    chain = _prepare_chain(monkeypatch, download_return=None)
    context = _build_download_context(site_downloader=site_downloader)

    download_hash, error_msg = chain.download_single(
        context=context,
        torrent_content=b"dummy-torrent",
        source="Subscribe|1",
        downloader=downloader_arg,
        return_detail=True,
    )

    assert download_hash is None
    assert chain.download.call_count == 1
    assert chain.download.call_args.kwargs["downloader"] == (downloader_arg or site_downloader)
    for fragment in expected_fragments:
        assert fragment in error_msg
    assert chain.post_message.call_count == 1
    assert error_msg in chain.post_message.call_args.args[0].text
    assert chain._record_download_failure.call_count == 0


def test_downloader_rejection_tuple_still_records_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载器明确返回失败四元组时，应原样透出原因并照常写入资源失败冷却台账。"""
    chain = _prepare_chain(
        monkeypatch,
        download_return=(None, None, None, "添加种子任务失败：demo.torrent"),
    )
    context = _build_download_context()

    download_hash, error_msg = chain.download_single(
        context=context,
        torrent_content=b"dummy-torrent",
        source="Subscribe|1",
        downloader="qb",
        return_detail=True,
    )

    assert download_hash is None
    assert error_msg == "添加种子任务失败：demo.torrent"
    assert chain._record_download_failure.call_count == 1
    assert chain.post_message.call_count == 1
