"""
精确搜索同名剧集别名消歧与季年核对测试。
"""

from collections import Counter
from unittest.mock import Mock

import pytest

from app.chain import search as search_module
from app.chain.search import SearchChain
from app.core.context import MediaInfo, TorrentInfo
from app.schemas.types import MediaType


@pytest.fixture
def yearless_evidence():
    """
    构造有明确媒体编号、尚缺首播日期的《铁证》订阅目标。
    """
    return MediaInfo(
        source="themoviedb",
        media_id="304086",
        tmdb_id=304086,
        title="铁证",
        original_title="铁证",
        names=["Evidence"],
        type=MediaType.TV,
        year=None,
    )


def _parse_resources(
    monkeypatch,
    target,
    torrents,
    candidate=None,
    *,
    recognize_side_effect=None,
    diagnostics=None,
):
    """
    走精确搜索解析资源，用离线识别结果代替媒体源查询。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta = Mock(
        return_value=candidate,
        side_effect=recognize_side_effect,
    )
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    monkeypatch.setattr(
        search_module.SearchChain,
        "_SearchChain__remove_duplicate",
        staticmethod(lambda contexts: contexts),
    )
    monkeypatch.setattr(
        search_module.ProgressHelper,
        "start",
        lambda self: None,
    )
    monkeypatch.setattr(
        search_module.ProgressHelper,
        "update",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        search_module.ProgressHelper,
        "end",
        lambda self: None,
    )
    chain = object.__new__(SearchChain)

    contexts = chain._SearchChain__parse_result(
        torrents=list(torrents),
        mediainfo=target,
        rule_groups=[],
        diagnostics=diagnostics,
    )
    return contexts, media_chain.recognize_by_meta


def test_exact_search_rejects_yearless_alias_of_another_work(monkeypatch):
    """
    目标有年份、资源无年份且仅靠别名命中时，若识别到另一部不同年份的剧集应跳过并记入诊断。
    """
    target = MediaInfo(
        source="themoviedb",
        media_id="202400",
        tmdb_id=202400,
        title="碰撞",
        original_title="크래시",
        names=["Crash"],
        type=MediaType.TV,
        year="2024",
        season_years={1: "2024"},
    )
    candidate = MediaInfo(
        source="themoviedb",
        media_id="200800",
        tmdb_id=200800,
        title="撞车",
        original_title="Crash",
        type=MediaType.TV,
        year="2008",
    )
    torrent = TorrentInfo(
        title="Crash S01 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    diagnostics = Counter()

    contexts, recognize = _parse_resources(
        monkeypatch, target, [torrent], candidate, diagnostics=diagnostics
    )

    assert contexts == []
    assert diagnostics["identity_mismatch"] == 1
    recognize.assert_called_once()


def test_exact_search_reuses_disambiguation_for_same_parsed_title(monkeypatch):
    """
    同名称、同年且同季范围的多个资源应复用消歧缓存，避免重复识别。
    """
    target = MediaInfo(
        source="themoviedb",
        media_id="202400",
        tmdb_id=202400,
        title="碰撞",
        original_title="크래시",
        names=["Crash"],
        type=MediaType.TV,
        year="2024",
        season_years={1: "2024"},
    )
    candidate = MediaInfo(
        source="themoviedb",
        media_id="202400",
        tmdb_id=202400,
        title="碰撞",
        original_title="크래시",
        type=MediaType.TV,
        year="2024",
    )
    torrents = [
        TorrentInfo(
            title="Crash S01E01 1080p WEB-DL H264 AAC",
            site_name="站点A",
            category=MediaType.TV.value,
        ),
        TorrentInfo(
            title="Crash S01E02 2160p WEB-DL H265 AAC",
            site_name="站点B",
            category=MediaType.TV.value,
        ),
    ]

    contexts, recognize = _parse_resources(monkeypatch, target, torrents, candidate)

    assert len(contexts) == 2
    recognize.assert_called_once()


@pytest.mark.parametrize(
    ("season", "resource_year", "candidate_year", "season_years", "kept"),
    [
        pytest.param(1, "2023", "2023", {}, False, id="other-work-same-year"),
        pytest.param(1, "2026", "2023", {}, True, id="fallback-other-year"),
        pytest.param(2, "2026", "2023", {2: "2026"}, False, id="other-work-same-season-year"),
        pytest.param(2, "2026", None, {2: "2026"}, False, id="season-year-only"),
        pytest.param(2, "2026", "2023", {3: "2026"}, True, id="unrelated-season-year"),
        pytest.param(2, "2026", "2026", {2: "2027"}, True, id="known-season-year-takes-precedence"),
    ],
)
def test_exact_search_checks_candidate_year_for_yearless_alias(
    monkeypatch, yearless_evidence, season, resource_year, candidate_year, season_years, kept,
):
    """
    只有二次识别候选也符合资源年份时，不同媒体编号才足以拒绝别名资源。
    """
    candidate = MediaInfo(
        source="themoviedb",
        media_id="330828",
        tmdb_id=330828,
        title="证词",
        original_title="证词",
        type=MediaType.TV,
        year=candidate_year,
        season_years=season_years,
        original_language="zh",
    )
    torrent = TorrentInfo(
        title=f"Evidence S{season:02d} {resource_year} 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    contexts, recognize = _parse_resources(monkeypatch, yearless_evidence, [torrent], candidate)

    assert bool(contexts) is kept
    recognize.assert_called_once()


def test_exact_search_accepts_yearless_alias_when_identity_is_confirmed(monkeypatch, yearless_evidence):
    """
    目标缺少年份时，二次识别确认同一媒体仍可保留资源。
    """
    torrent = TorrentInfo(
        title="Evidence S01 2026 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    contexts, recognize = _parse_resources(
        monkeypatch, yearless_evidence, [torrent], yearless_evidence,
    )

    assert len(contexts) == 1
    recognize.assert_called_once()


def test_exact_search_accepts_yearless_target_when_target_season_year_matches(monkeypatch):
    """
    目标缺少整剧首播年但季首播年印证资源年份时，应保留资源。
    """
    target = MediaInfo(
        source="themoviedb",
        media_id="304086",
        tmdb_id=304086,
        title="铁证",
        original_title="铁证",
        names=["Evidence"],
        type=MediaType.TV,
        year=None,
        season_years={2: "2026"},
    )
    candidate = MediaInfo(
        source="themoviedb",
        media_id="330828",
        tmdb_id=330828,
        title="证词",
        original_title="证词",
        type=MediaType.TV,
        year="2026",
    )
    torrent = TorrentInfo(
        title="Evidence S02 2026 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )

    contexts, recognize = _parse_resources(monkeypatch, target, [torrent], candidate)

    assert len(contexts) == 1
    recognize.assert_called_once()


@pytest.mark.parametrize("seasons", [(1, 2), (2, 1)])
def test_exact_search_keeps_alias_recognition_separate_by_season(monkeypatch, yearless_evidence, seasons):
    """
    同名同年的两个季各自识别，交换资源顺序也只保留正确身份的季。
    """
    torrents = [
        TorrentInfo(
            title=f"Evidence S{season:02d} 2026 1080p WEB-DL H264 AAC",
            site_name="测试站点",
            category=MediaType.TV.value,
        )
        for season in seasons
    ]
    other = MediaInfo(
        source="themoviedb",
        media_id="330828",
        tmdb_id=330828,
        title="证词",
        type=MediaType.TV,
        year="2026",
    )

    def recognize_season(meta, *, obtain_images):
        """
        模拟媒体源根据请求季号返回不同身份。
        """
        assert not obtain_images
        return yearless_evidence if meta.begin_season == 1 else other

    contexts, recognize = _parse_resources(
        monkeypatch, yearless_evidence, torrents, recognize_side_effect=recognize_season,
    )

    assert [context.meta_info.begin_season for context in contexts] == [1]
    assert [call.args[0].begin_season for call in recognize.call_args_list] == list(seasons)


def test_exact_search_skips_disambiguation_without_year_evidence(monkeypatch, yearless_evidence):
    """
    订阅和资源均无年份时继续按标题匹配，不因同名查询引入反证。
    """
    torrent = TorrentInfo(
        title="Evidence S01 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )

    contexts, recognize = _parse_resources(monkeypatch, yearless_evidence, [torrent])

    assert len(contexts) == 1
    recognize.assert_not_called()


def test_exact_search_skips_disambiguation_for_primary_title_match(monkeypatch, yearless_evidence):
    """
    通过主标题命中时无需二次消歧。
    """
    torrent = TorrentInfo(
        title="铁证 S01 2026 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )

    contexts, recognize = _parse_resources(monkeypatch, yearless_evidence, [torrent])

    assert len(contexts) == 1
    recognize.assert_not_called()
