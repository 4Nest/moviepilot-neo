import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download as download_module
import app.helper.torrent as torrent_module
import app.modules.transmission.transmission as transmission_client_module
from app.chain.download import DownloadChain
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.torrent import TorrentHelper
from app.modules.qbittorrent import QbittorrentModule
from app.modules.transmission import TransmissionModule
from app.modules.transmission.transmission import Transmission
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType


class _Response:
    """构造离线 HTTP 响应，保留状态码的布尔语义。"""

    def __init__(self, payload=None, status_code=200, content=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload or {}).encode()
        self.text = self.content.decode(errors="replace")
        self.reason = "test response"

    def __bool__(self):
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _ticket_url(**options):
    """生成包含模拟请求参数的两段式换票地址。"""
    recipe = {"method": "post", "params": {"id": "39"}, "result": "data", **options}
    encoded = base64.b64encode(json.dumps(recipe).encode()).decode()
    return f"[{encoded}]https://api.tracker.example/ticket"


@pytest.mark.parametrize("message", [
    "换票失败：HTTP 404，ticket=secret",
    "临时下载地址失效：下载种子出错，状态码：404",
])
def test_ticket_failures_use_transient_cooldown(message):
    """换票与临时地址错误中的 404 不得升级为二十四小时资源冷却。"""
    assert DownloadChain._download_failure_ttl(message) == 3600
    assert DownloadChain._download_failure_ttl("下载种子内容为空") == 86400


def test_ticket_failure_is_sanitized_and_reports_cookie(monkeypatch):
    """换票诊断仅保留有限摘要，且不泄漏请求头、Cookie、令牌和完整签名 URL。"""
    api_key = "synthetic-api-key"
    cookie = "session=synthetic-cookie"
    signed_path = "private-path-ticket"
    signed_query = "private-query-ticket"
    message = (
        f"authentication failed {api_key} {cookie} Bearer synthetic-bearer "
        f"token=private-body-token https://tracker.example/{signed_path}?sign={signed_query}"
    )
    client = MagicMock()
    client.post_res.return_value = _Response({"code": 1, "message": message}, status_code=500)
    monkeypatch.setattr(download_module, "RequestUtils", lambda **_kwargs: client)
    helper = MagicMock()
    monkeypatch.setattr(download_module, "TorrentHelper", lambda: helper)
    chain = object.__new__(DownloadChain)
    chain.post_message = MagicMock()
    torrent = TorrentInfo(
        title="Example",
        site_name="Example",
        site_cookie=cookie,
        enclosure=_ticket_url(cookie=True, header={"x-api-key": api_key}),
    )
    errors = []

    assert chain.download_torrent(torrent, on_failure=errors.append) == (None, "", [])

    assert len(errors) == 1
    diagnostic = errors[0]
    assert diagnostic.startswith("换票失败：")
    assert "HTTP 500" in diagnostic and "code=1" in diagnostic
    assert "携带 Cookie：是" in diagnostic
    assert len(diagnostic) <= 500
    for secret in (api_key, "synthetic-cookie", "synthetic-bearer", "private-body-token", signed_path, signed_query):
        assert secret not in diagnostic
    assert "https://tracker.example" in diagnostic
    helper.download_torrent.assert_not_called()
    helper.add_invalid.assert_not_called()


def test_ticket_rejects_invalid_temporary_address(monkeypatch):
    """换票接口返回非下载地址时应在换票阶段失败，不进入临时地址取种。"""
    client = MagicMock()
    client.post_res.return_value = _Response({"data": "not a download URL"})
    monkeypatch.setattr(download_module, "RequestUtils", lambda **_kwargs: client)
    helper = MagicMock()
    monkeypatch.setattr(download_module, "TorrentHelper", lambda: helper)
    chain = object.__new__(DownloadChain)
    chain.post_message = MagicMock()
    errors = []

    result = chain.download_torrent(
        TorrentInfo(title="Example", enclosure=_ticket_url()), on_failure=errors.append,
    )

    assert result == (None, "", [])
    assert errors[0].startswith("换票失败：")
    helper.download_torrent.assert_not_called()


def test_temporary_ticket_content_failure_uses_transient_reason(monkeypatch):
    """换票后的无效种子响应使用临时地址分类，不写入损坏种子黑名单。"""
    client = MagicMock()
    client.post_res.return_value = _Response({"data": "https://tracker.example/download?sign=secret"})
    monkeypatch.setattr(download_module, "RequestUtils", lambda **_kwargs: client)
    helper = MagicMock()
    helper.download_torrent.return_value = (None, None, "", [], "种子数据有误，请确认链接是否正确")
    monkeypatch.setattr(download_module, "TorrentHelper", lambda: helper)
    chain = object.__new__(DownloadChain)
    chain.post_message = MagicMock()
    torrent = TorrentInfo(title="Example", site_name="Example", enclosure=_ticket_url())
    errors = []

    assert chain.download_torrent(torrent, on_failure=errors.append) == (None, "", [])

    assert len(errors) == 1 and errors[0].startswith("临时下载地址失效：")
    assert DownloadChain._download_failure_ttl(errors[0]) == 3600
    assert helper.download_torrent.call_args.kwargs["cache_invalid"] is False
    helper.add_invalid.assert_not_called()


def test_traditional_first_download_confirmation_is_submitted(monkeypatch):
    """繁体首次下载提示也应提取确认表单并提交，而非按损坏种子处理。"""
    marker = "下載種子檔案"
    form = '<form action="?"><input name="confirm" value="yes"></form>'
    valid_content = b"valid-torrent"
    client = MagicMock()
    client.get_res.return_value = _Response(content=(marker + form).encode())
    client.post_res.return_value = _Response(content=valid_content)
    monkeypatch.setattr(torrent_module, "RequestUtils", lambda **_kwargs: client)
    cache = MagicMock()
    cache.get.return_value = None
    monkeypatch.setattr(torrent_module, "FileCache", lambda: cache)

    def parse_torrent(content):
        """仅接受确认后返回的模拟种子内容。"""
        if content != valid_content:
            raise ValueError("invalid torrent")
        return MagicMock()

    monkeypatch.setattr(torrent_module.Torrent, "from_string", parse_torrent)

    def parse_content(_self, content):
        """仅接受确认后返回的模拟种子内容。"""
        if content != valid_content:
            raise ValueError("invalid torrent")
        return "", ["Show.S01E01.mkv"]

    monkeypatch.setattr(TorrentHelper, "get_fileinfo_from_torrent_content", parse_content)
    helper = TorrentHelper()
    helper.add_invalid = MagicMock()

    result = helper.download_torrent("https://tracker.example/download?id=1")

    assert result[1] == valid_content
    assert client.post_res.call_args.kwargs["data"] == {"confirm": "yes"}
    helper.add_invalid.assert_not_called()


class _BatchTorrentHelper:
    """提供离线整包文件集数和稳定候选顺序。"""

    episodes = []

    def sort_torrents(self, contexts):
        """保持测试候选顺序。"""
        return contexts

    def get_torrent_episodes(self, _files):
        """返回预设的种子内离散集号。"""
        return list(self.episodes)


def _batch_context(episodes=None):
    """构造单季电视剧候选及下载链需要的上下文字段。"""
    meta = SimpleNamespace(
        season_list=[1],
        episode_list=list(episodes or []),
        title="Example Show",
        org_string="Example Show S01",
        set_episodes=lambda begin, end: None,
    )

    def set_episode_list(parsed):
        """记录种子文件解析出的集号。"""
        meta.episode_list = sorted(set(parsed))

    meta.set_episode_list = set_episode_list
    return SimpleNamespace(
        media_info=SimpleNamespace(
            type=MediaType.TV,
            title_year="Example Show (2026)",
            tmdb_id=1,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
        ),
        meta_info=meta,
        torrent_info=SimpleNamespace(title="Example Show S01", site_name="Example"),
        allowed_episodes=None,
        confirmed_full_coverage=False,
    )


def _complete_missing(start, end):
    """构造要求完整覆盖的订阅缺失范围。"""
    return {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=end,
                start_episode=start,
                require_complete_coverage=True,
            )
        }
    }


def test_full_season_pack_only_selects_subscribed_range(monkeypatch):
    """整季包覆盖单集订阅时只提交目标集，不下载范围外剧集。"""
    _BatchTorrentHelper.episodes = list(range(1, 11))
    monkeypatch.setattr(download_module, "TorrentHelper", _BatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    chain = object.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")
    context = _batch_context()

    downloads, lefts = chain.batch_download([context], _complete_missing(5, 5))

    assert downloads == [context] and lefts == {}
    assert chain.download_single.call_args.kwargs["episodes"] == {5}


def test_labelled_full_pack_only_selects_subscribed_range(monkeypatch):
    """文件名已标完整集数的整包同样只提交订阅范围。"""
    monkeypatch.setattr(download_module, "TorrentHelper", _BatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    chain = object.__new__(DownloadChain)
    chain.download_single = MagicMock(return_value="hash")
    context = _batch_context(range(1, 11))

    downloads, lefts = chain.batch_download([context], _complete_missing(5, 6))

    assert downloads == [context] and lefts == {}
    assert chain.download_single.call_args.kwargs["episodes"] == {5, 6}


def test_exact_full_season_range_keeps_whole_submission(monkeypatch):
    """种子集数与订阅目标完全一致时不传选集，保留整包附属文件。"""
    _BatchTorrentHelper.episodes = [1, 2, 3, 4]
    monkeypatch.setattr(download_module, "TorrentHelper", _BatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    chain = object.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    downloads, lefts = chain.batch_download([_batch_context()], _complete_missing(1, 4))

    assert downloads and lefts == {}
    assert chain.download_single.call_args.kwargs["episodes"] is None


def test_batch_failure_ledger_preserves_ticket_classification(monkeypatch):
    """整包准备失败台账保留换票原因，不被改写为二十四小时空内容冷却。"""
    monkeypatch.setattr(download_module, "TorrentHelper", _BatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    chain = object.__new__(DownloadChain)

    def fail_download(_torrent, on_failure=None):
        """模拟换票接口返回资源不存在的瞬态诊断。"""
        on_failure("换票失败：HTTP 404")
        return None, "", []

    chain.download_torrent = MagicMock(side_effect=fail_download)
    chain.download_single = MagicMock()
    chain._record_download_failure = MagicMock()

    downloads, _lefts = chain.batch_download([_batch_context()], _complete_missing(5, 5))

    assert not downloads
    error = chain._record_download_failure.call_args.kwargs["error_msg"]
    assert error == "换票失败：HTTP 404"
    assert DownloadChain._download_failure_ttl(error) == 3600


def _downloader_module(kind, files):
    """构造使用磁力内容且文件列表可配置的下载器模块与客户端。"""
    server = MagicMock()
    server.get_files.return_value = files
    server.start_torrents.return_value = True
    server.stop_torrents.return_value = True
    if kind == "qb":
        server.add_torrent.return_value = (True, ["hash"])
        server.get_content_layout.return_value = "Original"
        server.is_force_resume.return_value = False
        module = object.__new__(QbittorrentModule)
    else:
        server.add_torrent.return_value = SimpleNamespace(hashString="hash")
        module = object.__new__(TransmissionModule)
    module.get_instance = MagicMock(return_value=server)
    module.normalize_path = MagicMock(side_effect=lambda path, _downloader: path)
    module.get_default_config_name = MagicMock(return_value="default")
    return module, server


def _download_selected(module, episodes):
    """通过公开添加下载入口执行选集并返回模块结果。"""
    return module.download(
        content="magnet:?xt=urn:btih:test",
        download_dir=Path("/downloads"),
        cookie="",
        episodes=episodes,
        downloader="test",
    )


@pytest.mark.parametrize("kind", ["qb", "tr"])
def test_paused_magnet_fetches_metadata_then_selects(kind):
    """暂停磁力先启动获取元数据，重新暂停后再选集并启动。"""
    files = (
        [{"id": 0, "name": "Show.S01E06.mkv"}]
        if kind == "qb" else [SimpleNamespace(id=0, name="Show.S01E06.mkv")]
    )
    module, server = _downloader_module(kind, [])
    server.get_files.side_effect = [[], files]

    result = _download_selected(module, {6})

    assert result[1] == "hash"
    assert server.get_files.call_args_list[1].kwargs == {"retry": 10, "interval": 1}
    server.stop_torrents.assert_called_with("hash")
    assert server.start_torrents.call_count == 2


@pytest.mark.parametrize("kind", ["qb", "tr"])
def test_single_video_fallback_selects_mislabeled_episode(kind):
    """唯一视频文件的累计集号不匹配目标季集时仍按单集兜底选中。"""
    files = (
        [
            {"id": 0, "name": "Show.S03E54.mp4"},
            {"id": 1, "name": "Show.S03E54.ass"},
        ]
        if kind == "qb" else [
            SimpleNamespace(id=0, name="Show.S03E54.mp4"),
            SimpleNamespace(id=1, name="Show.S03E54.ass"),
        ]
    )
    module, server = _downloader_module(kind, files)

    result = _download_selected(module, {6})

    assert result[1] == "hash"
    if kind == "qb":
        server.set_files.assert_called_once_with(torrent_hash="hash", file_ids=[1], priority=0)
    else:
        server.set_files.assert_called_once_with("hash", [0])
        server.set_unwanted_files.assert_called_once_with("hash", [1])


@pytest.mark.parametrize("kind", ["qb", "tr"])
def test_unmatched_selection_fails_without_starting_or_empty_selection(kind):
    """完全无匹配时保持暂停并返回失败，不能放任整包或提交空文件列表。"""
    files = (
        [
            {"id": 0, "name": "Show.S01E07.mkv"},
            {"id": 1, "name": "Show.S01E08.mkv"},
        ]
        if kind == "qb" else [
            SimpleNamespace(id=0, name="Show.S01E07.mkv"),
            SimpleNamespace(id=1, name="Show.S01E08.mkv"),
        ]
    )
    module, server = _downloader_module(kind, files)

    result = _download_selected(module, {6})

    assert result[1] is None
    assert "未找到目标集数" in result[3]
    server.set_files.assert_not_called()
    server.start_torrents.assert_not_called()


@pytest.mark.parametrize("kind", ["qb", "tr"])
def test_metadata_timeout_repauses_and_fails(kind):
    """元数据等待超时后恢复暂停，不设置空文件列表也不记录成功。"""
    module, server = _downloader_module(kind, [])

    result = _download_selected(module, {6})

    assert result[1] is None and "获取种子文件失败" in result[3]
    server.stop_torrents.assert_called_once_with("hash")
    server.set_files.assert_not_called()
    server.start_torrents.assert_called_once_with("hash")


@pytest.mark.parametrize("kind", ["qb", "tr"])
def test_single_multi_episode_video_does_not_use_fallback(kind):
    """唯一文件包含多集范围时不得冒充目标单集。"""
    files = (
        [{"id": 0, "name": "Show.S01E07-E08.mkv"}]
        if kind == "qb" else [SimpleNamespace(id=0, name="Show.S01E07-E08.mkv")]
    )
    module, server = _downloader_module(kind, files)

    result = _download_selected(module, {6})

    assert result[1] is None
    server.set_files.assert_not_called()
    server.start_torrents.assert_not_called()


def test_transmission_get_files_retries_empty_metadata(monkeypatch):
    """Transmission 文件接口按次数与间隔等待暂停磁力的元数据。"""
    client = object.__new__(Transmission)
    client.trc = MagicMock()
    torrent = MagicMock()
    video = SimpleNamespace(id=0, name="Show.mkv")
    torrent.get_files.side_effect = [[], [video]]
    client.trc.get_torrent.return_value = torrent
    sleep = MagicMock()
    monkeypatch.setattr(transmission_client_module.time, "sleep", sleep)

    assert client.get_files("hash", retry=2, interval=0.5) == [video]
    sleep.assert_called_once_with(0.5)


def test_download_history_records_single_video_fallback(monkeypatch):
    """下载器采用唯一视频兜底后，历史明细也保留该文件。"""
    history = MagicMock()
    monkeypatch.setattr(download_module, "DownloadHistoryOper", lambda: history)
    monkeypatch.setattr(download_module, "MediaChain", lambda: MagicMock(
        supplement_tmdb_info=MagicMock(side_effect=lambda media, _meta: media),
    ))
    chain = object.__new__(DownloadChain)
    chain.download = MagicMock(return_value=("qb", "hash", "Original", "添加下载成功"))
    chain._resolve_media_download_dir = MagicMock(return_value=("local", Path("/downloads"), ""))
    chain._submit_download_added_task = MagicMock()
    chain.post_message = MagicMock()
    chain.eventmanager = MagicMock()
    helper = MagicMock()
    helper.get_fileinfo_from_torrent_content.return_value = ("", ["Show.S03E54.mp4"])
    monkeypatch.setattr(download_module, "TorrentHelper", lambda: helper)
    context = Context(
        meta_info=MetaInfo("Show.S03E06"),
        media_info=MediaInfo(type=MediaType.TV, title="Show", tmdb_id=1),
        torrent_info=TorrentInfo(title="Show.S03E54"),
    )

    result = chain.download_single(context=context, torrent_content=b"torrent", episodes={6})

    assert result == "hash"
    files = history.add_files.call_args.args[0]
    assert [item["filepath"] for item in files] == ["Show.S03E54.mp4"]
