"""下载选集、Transmission 重复种子保护、识别词季集映射与订阅裁短回归测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.torrent import (
    EpisodeSelection,
    TorrentHelper,
    episode_selection_for_context,
    select_torrent_files,
)
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule
from app.modules.transmission.transmission import Transmission
from app.schemas import ExistMediaInfo, NotExistMediaInfo
from app.schemas.types import MediaType


def test_transmission_duplicate_add_skips_pause_and_file_mutation(monkeypatch):
    """Transmission 返回 torrent-duplicate 时必须跳过暂停与改勾选，保护已在跑的任务。"""
    tr = object.__new__(Transmission)
    tr._host = "127.0.0.1"
    tr._port = 9091
    fake_rpc = Mock()
    raw_resp = {
        "result": "success",
        "arguments": {
            "torrent-duplicate": {
                "id": 7,
                "name": "Show.S01",
                "hashString": "dup_hash_123",
            }
        },
    }
    fake_rpc._http_query.return_value = raw_resp
    tr.trc = fake_rpc

    added_torrent, is_dup = tr.add_torrent_with_status(content=b"fake-torrent")
    assert is_dup is True
    assert added_torrent.hash_string == "dup_hash_123"

    module = TransmissionModule()
    fake_server = Mock()
    fake_server.add_torrent_with_status = Mock(
        return_value=(SimpleNamespace(hashString="dup_hash_123"), True)
    )
    monkeypatch.setattr(module, "get_instance", lambda _name=None: fake_server)
    monkeypatch.setattr(module, "get_default_config_name", lambda: "TRDefault")

    res = module.download(
        content="magnet:?xt=urn:btih:dup_hash_123",
        download_dir=Path("/downloads"),
        cookie="",
        episodes={2},
    )
    assert res == ("TRDefault", "dup_hash_123", "Original", "下载任务已存在")
    fake_server.stop_torrents.assert_not_called()
    fake_server.set_files.assert_not_called()
    fake_server.delete_torrents.assert_not_called()


def test_select_torrent_files_multi_episode_attachments_and_custom_words():
    """选集应保留跨边界多集单文件与无集号附件，并冻结识别词季集映射。"""
    files = [
        (0, "Pack/Show.S01E01-E02.1080p.mkv"),
        (1, "Pack/Show.S01E03.1080p.mkv"),
        (2, "Pack/Show.S01E01-E02.chs.ass"),
        (3, "Pack/Show.S01E03.chs.ass"),
        (4, "Pack/Fonts/MainTheme.mka"),
    ]
    wanted, unwanted, matched = select_torrent_files(
        files=files,
        episodes={2},
        media_extensions=[".mkv", ".mp4"],
    )
    assert matched == {1, 2}
    assert wanted == [0, 2, 4]
    assert unwanted == [1, 3]

    # 识别词把 S01E20 映射为 S02E08（如分季番按总集号发布）
    custom_word_text = r"S01E20 => S02E08"
    custom_words = [custom_word_text]
    meta = MetaInfo("Anime S01E20 1080p", custom_words=custom_words)
    assert meta.begin_season == 2
    assert meta.episode_list == [8]

    ctx = Context(
        meta_info=meta,
        media_info=MediaInfo(),
        torrent_info=TorrentInfo(title="Anime S01E20 1080p"),
    )
    selection = episode_selection_for_context(ctx)
    assert selection == EpisodeSelection(season=2, source_season=1, episode_map=((20, 8),))

    mapped_files = [
        (10, "Anime/Show.E20.mkv"),
        (11, "Anime/Show.E08.mkv"),
    ]
    wanted_mapped, unwanted_mapped, matched_mapped = select_torrent_files(
        files=mapped_files,
        episodes={8},
        media_extensions=[".mkv"],
        custom_words=custom_word_text,
        episode_selection=selection,
    )
    assert matched_mapped == {8}
    assert wanted_mapped == [10]
    assert unwanted_mapped == [11]

    # 文件自身也命中 custom_words 时不会双重换算
    wanted_no_double, unwanted_no_double, matched_no_double = select_torrent_files(
        files=[(0, "Anime/Show.S01E20.mkv"), (1, "Anime/Show.S01E08.mkv")],
        episodes={8},
        media_extensions=[".mkv"],
        custom_words=custom_word_text,
        episode_selection=selection,
    )
    assert matched_no_double == {8}
    assert wanted_no_double == [0]
    assert unwanted_no_double == [1]

    parsed_eps = TorrentHelper().get_torrent_episodes(
        ["Anime/Anime S01E20.mkv", "Anime/Anime S01E21.mkv"],
        custom_words=custom_words,
        episode_selection=selection,
    )
    assert parsed_eps == [8]


def test_downloaders_discard_paused_seed_when_episode_selection_empty(monkeypatch):
    """新增种子在选集为空或失败时，QB/TR/RT 均应自动删除该暂停废种且不删本地文件。"""
    magnet_uri = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"

    # Transmission
    tr_mod = TransmissionModule()
    tr_server = Mock()
    tr_server.add_torrent_with_status = Mock(
        return_value=(SimpleNamespace(hashString="tr_empty_hash"), False)
    )
    tr_server.get_files.return_value = [
        SimpleNamespace(id=0, name="Show.S01E01.mkv"),
        SimpleNamespace(id=1, name="Show.S01E02.mkv"),
    ]
    monkeypatch.setattr(tr_mod, "get_instance", lambda _n=None: tr_server)
    tr_res = tr_mod.download(content=magnet_uri, download_dir=Path("/dl"), cookie="", episodes={9})
    assert tr_res[:3] == (None, None, None)
    assert "未找到目标集数" in tr_res[3]
    tr_server.delete_torrents.assert_called_once_with(delete_file=False, ids="tr_empty_hash")

    # qBittorrent
    qb_mod = QbittorrentModule()
    qb_server = Mock()
    qb_server.add_torrent.return_value = (True, ["qb_empty_hash"])
    qb_server.get_content_layout.return_value = "Original"
    qb_server.get_files.return_value = [
        {"index": 0, "name": "Show.S01E01.mkv"},
        {"index": 1, "name": "Show.S01E02.mkv"},
    ]
    monkeypatch.setattr(qb_mod, "get_instance", lambda _n=None: qb_server)
    qb_res = qb_mod.download(content=magnet_uri, download_dir=Path("/dl"), cookie="", episodes={9})
    assert qb_res[:3] == (None, None, None)
    assert "未找到目标集数" in qb_res[3]
    qb_server.delete_torrents.assert_called_once_with(delete_file=False, ids="qb_empty_hash")

    # rTorrent
    rt_mod = RtorrentModule()
    rt_server = Mock()
    rt_server.add_torrent.return_value = True
    rt_server.get_torrent_id_by_tag.return_value = "rt_empty_hash"
    rt_server.get_files.return_value = [
        {"id": 0, "name": "Show.S01E01.mkv"},
        {"id": 1, "name": "Show.S01E02.mkv"},
    ]
    monkeypatch.setattr(rt_mod, "get_instance", lambda _n=None: rt_server)
    rt_res = rt_mod.download(content=magnet_uri, download_dir=Path("/dl"), cookie="", episodes={9})
    assert rt_res[:3] == (None, None, None)
    rt_server.delete_torrents.assert_called_once_with(delete_file=False, ids="rt_empty_hash")


def test_download_history_records_multi_episode_and_zero_episode_files(monkeypatch, tmp_path):
    """DownloadChain.download_single 登记下载历史文件时应完整保留 E00 与多集单文件。"""
    monkeypatch.setattr(
        MediaChain, "supplement_tmdb_info", lambda self, media, _meta: media
    )
    monkeypatch.setattr(
        "app.chain.download.validate_download_save_path", lambda p: p
    )
    monkeypatch.setattr(
        TorrentHelper,
        "get_fileinfo_from_torrent_content",
        lambda self, _c: (
            "Show",
            [
                "Show/Show.S01E00.mkv",
                "Show/Show.S01E01-E02.mkv",
                "Show/Show.S01E03.mkv",
            ],
        ),
    )

    chain = object.__new__(DownloadChain)
    chain.eventmanager = Mock()
    chain._resolve_bt_default_config = staticmethod(lambda _t: None)
    chain._resolve_media_download_dir = lambda **_kw: ("local", tmp_path, "")
    chain.download = lambda **_kw: ("DefaultQB", "hash_hist_1", "Original", "ok")
    chain.post_message = lambda *_a, **_kw: None
    chain.run_module = lambda *_a, **_kw: None

    recorded_files = []
    fake_his = Mock()
    fake_his.add_files.side_effect = recorded_files.extend
    monkeypatch.setattr("app.chain.download.DownloadHistoryOper", lambda: fake_his)

    meta = MetaInfo("Show S01")
    media = MediaInfo()
    media.type = MediaType.TV
    media.title = "Show"
    media.year = "2026"
    media.tmdb_id = 1001
    torrent = TorrentInfo(title="Show S01", site_name="TestSite", enclosure="https://pt.test/1.torrent")
    ctx = Context(meta_info=meta, media_info=media, torrent_info=torrent)

    dl_id = chain.download_single(
        context=ctx,
        torrent_content=b"torrent-bytes",
        episodes={0, 2},
        save_path=str(tmp_path),
    )
    assert dl_id == "hash_hist_1"
    saved_paths = [item["filepath"] for item in recorded_files]
    assert saved_paths == [
        "Show/Show.S01E00.mkv",
        "Show/Show.S01E01-E02.mkv",
    ]


def test_get_no_exists_info_episode_group_trimmed_total_and_cumulative_clipping(monkeypatch):
    """get_no_exists_info 支持按剧集组重算、整季未入库裁短拆包与累计集号手工末集裁剪。"""
    chain = object.__new__(DownloadChain)
    monkeypatch.setattr(
        "app.chain.download.MediaServerOper.get_item_id",
        lambda self, **_kw: None,
    )

    recognized_calls = []

    def fake_recognize_media(**kwargs):
        recognized_calls.append(kwargs.get("episode_group"))
        grouped = MediaInfo()
        grouped.type = MediaType.TV
        grouped.title = "Grouped Anime"
        grouped.tmdb_id = 2002
        grouped.episode_group = kwargs.get("episode_group")
        grouped.seasons = {1: list(range(1, 13)), 2: list(range(13, 25))}
        return grouped

    chain.recognize_media = fake_recognize_media
    chain.media_exists = lambda **_kw: None

    # 即使入参 mediainfo 已有默认排序的 seasons，设置 episode_group 后也必须重识别
    initial_media = MediaInfo()
    initial_media.type = MediaType.TV
    initial_media.title = "Grouped Anime"
    initial_media.tmdb_id = 2002
    initial_media.episode_group = "eg-absolute"
    initial_media.seasons = {1: list(range(1, 25))}

    meta_s1 = MetaInfo("Grouped Anime S01")
    exists_flag, no_exists = chain.get_no_exists_info(
        meta=meta_s1,
        mediainfo=initial_media,
        totals={1: 6},
    )
    assert exists_flag is False
    assert recognized_calls == ["eg-absolute"]
    s1_missing = next(iter(no_exists.values()))[1]
    assert s1_missing.episodes == [1, 2, 3, 4, 5, 6]
    assert s1_missing.total_episode == 6

    # 累计集号季 S02 为 E13-E24，手工限制 manual_total_episode -> total_episode=18
    meta_s2 = MetaInfo("Grouped Anime S02")
    _flag2, no_exists_s2 = chain.get_no_exists_info(
        meta=meta_s2,
        mediainfo=initial_media,
        totals={2: 18},
        totals_are_episode_numbers=True,
    )
    s2_missing = next(iter(no_exists_s2.values()))[2]
    assert s2_missing.episodes == [13, 14, 15, 16, 17, 18]
    assert s2_missing.start_episode == 13

    # 已入库部分剧集（如已有 13, 14, 20），裁剪后只应缺 15..18，不应倒挂或包含 19+
    chain.media_exists = lambda **_kw: ExistMediaInfo(
        type=MediaType.TV, seasons={2: [13, 14, 20]}
    )
    _flag3, no_exists_partial = chain.get_no_exists_info(
        meta=meta_s2,
        mediainfo=initial_media,
        totals={2: 18},
        totals_are_episode_numbers=True,
    )
    s2_partial = next(iter(no_exists_partial.values()))[2]
    assert s2_partial.episodes == [15, 16, 17, 18]


def test_batch_download_update_seasons_keeps_other_missing_seasons(monkeypatch):
    """多季同时缺失时下载 S01 整季包仅移除 S01，不得误删同剧仍缺失的 S02。"""
    chain = object.__new__(DownloadChain)
    chain.torrenthelper = SimpleNamespace(
        sort_torrents=lambda contexts: contexts,
        get_torrent_episodes=lambda files, **_kw: [1, 2],
    )
    chain._matches_media_identity = staticmethod(lambda _m, _mid: True)
    chain.download_torrent = lambda *_a, **_kw: (
        b"pack-bytes",
        "MultiSeason",
        ["MultiSeason.S01E01.mkv", "MultiSeason.S01E02.mkv"],
    )
    chain.download_single = lambda **_kw: "dl_s1_pack"

    media = MediaInfo()
    media.type = MediaType.TV
    media.title = "MultiSeason"
    media.year = "2026"
    media.tmdb_id = 3003

    meta_s1 = MetaInfo("MultiSeason S01")
    torrent_s1 = TorrentInfo(title="MultiSeason S01 Complete", site_name="PT")
    ctx_s1 = Context(meta_info=meta_s1, media_info=media, torrent_info=torrent_s1)

    no_exists = {
        3003: {
            1: NotExistMediaInfo(season=1, episodes=[], total_episode=2, start_episode=1),
            2: NotExistMediaInfo(season=2, episodes=[], total_episode=2, start_episode=1),
        }
    }
    downloaded, remaining = chain.batch_download(
        contexts=[ctx_s1],
        no_exists=no_exists,
        save_path="/downloads",
    )
    assert len(downloaded) == 1
    assert 3003 in remaining
    assert set(remaining[3003].keys()) == {2}


def test_subscribe_no_exists_passes_manual_total_and_non_negative_lack(monkeypatch):
    """SubscribeChain 计算缺失信息时透传 manual_total_episode 并将负总集数安全夹紧为 0。"""
    sub_chain = object.__new__(SubscribeChain)
    captured = {}

    def fake_get_no_exists(
        self,
        meta,
        mediainfo,
        totals=None,
        *,
        totals_are_episode_numbers=False,
        raise_on_query_failure=False,
    ):
        captured["totals"] = totals
        captured["totals_are_episode_numbers"] = totals_are_episode_numbers
        return False, {
            "tmdb:5005": {
                2: NotExistMediaInfo(
                    season=2,
                    episodes=[13, 14, 15, 16, 17, 18],
                    total_episode=18,
                    start_episode=13,
                )
            }
        }

    monkeypatch.setattr(DownloadChain, "get_no_exists_info", fake_get_no_exists)
    monkeypatch.setattr(
        SubscribeChain,
        "_SubscribeChain__get_downloaded",
        lambda self, _sub: [13, 14],
    )

    media = MediaInfo()
    media.type = MediaType.TV
    media.title = "Anime"
    media.year = "2026"
    media.tmdb_id = 5005
    media.seasons = {2: list(range(13, 25))}

    subscribe = SimpleNamespace(
        id=1,
        name="Anime",
        year="2026",
        type="电视剧",
        keyword=None,
        season=2,
        tmdbid=5005,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source="tmdb",
        media_id="5005",
        episode_group=None,
        total_episode=18,
        start_episode=13,
        manual_total_episode=1,
        skip_library_check=0,
        best_version=0,
        best_version_full=0,
        version_rules=[],
        note=[],
    )
    meta = MetaInfo("Anime S02")
    meta.type = MediaType.TV
    satisfied, missing = sub_chain.resolve_subscribe_missing(
        subscribe=subscribe,
        meta=meta,
        mediainfo=media,
        mediakey="tmdb:5005",
    )
    assert satisfied is False
    assert captured["totals"] == {2: 18}
    assert captured["totals_are_episode_numbers"] is True
    assert sorted(missing["tmdb:5005"][2].episodes) == [15, 16, 17, 18]

    neg_sub = SimpleNamespace(
        type="电视剧",
        season=2,
        total_episode=-5,
        manual_total_episode=1,
    )
    assert sub_chain._SubscribeChain__resolve_effective_total_episode(neg_sub, media) == 0
