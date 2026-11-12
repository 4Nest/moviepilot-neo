"""副标题中日番「第N期」季号、方括号集数区间与国产综艺「第N期」单集的消歧回归测试。"""

from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.metainfo import MetaInfo, clear_rust_parse_options_cache
from app.schemas.types import MediaType


@pytest.mark.parametrize("use_rust", [True, False])
def test_japanese_anime_period_with_fin_or_stripped_bracket_range(monkeypatch, use_rust: bool):
    """日番副标题含「第N期」与 [01-12Fin]（含被自定义识别词剥离 Fin 后的 [01-12]）应识别为整季集数范围。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", use_rust)
    clear_rust_parse_options_cache()

    title = "The Angel Next Door Spoils Me Rotten S02 2026 1080p BluRay x265 10bit FLAC 2.0-ADE"
    subtitle = (
        "关于邻家的天使大人不知不觉把我惯成了废人这档子事 第二季 / "
        "お隣の天使様にいつの間にか駄目人間にされていた件 第2期 [01-12Fin] [简繁中日双语字幕]"
    )

    # 1. 无自定义识别词
    meta_raw = MetaInfo(title=title, subtitle=subtitle, custom_words=[])
    assert meta_raw.type == MediaType.TV
    assert meta_raw.begin_season == 2
    assert meta_raw.end_season is None
    assert meta_raw.begin_episode == 1
    assert meta_raw.end_episode == 12
    assert meta_raw.total_episode == 12

    # 2. 启用社区常见剥离 Fin/END 的自定义识别词后，[01-12Fin] 变为 [01-12]，仍应识别为 E01-E12
    strip_fin_word = r"[【\[](Fin|END)[】\]]|(?:|\s|\s-\s)(Fin|END)(?=\])|(?<=\d{1,2})_?(Fin|END)"
    meta_stripped = MetaInfo(title=title, subtitle=subtitle, custom_words=[strip_fin_word])
    assert meta_stripped.type == MediaType.TV
    assert meta_stripped.begin_season == 2
    assert meta_stripped.begin_episode == 1
    assert meta_stripped.end_episode == 12
    assert meta_stripped.total_episode == 12


@pytest.mark.parametrize("use_rust", [True, False])
def test_japanese_anime_season_pack_without_episode_range_is_not_single_episode(
    monkeypatch,
    use_rust: bool,
):
    """日番整季合集副标题仅有「第N期」而无集数范围时，应按季处理而非单集 E0N。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", use_rust)
    clear_rust_parse_options_cache()

    # 主标题含 S02，副标题含中文第二季 + 日文第2期
    meta_dual = MetaInfo(
        title="The Angel Next Door Spoils Me Rotten S02 2026 1080p BluRay x265-ADE",
        subtitle="关于邻家的天使大人 第二季 / お隣の天使様にいつの間にか駄目人間にされていた件 第2期 [简繁日字幕]",
        custom_words=[],
    )
    assert meta_dual.type == MediaType.TV
    assert meta_dual.begin_season == 2
    assert meta_dual.begin_episode is None
    assert meta_dual.end_episode is None
    assert meta_dual.total_episode == 0

    # 主标题未带 S02，仅副标题含日文原名 + 第2期（或第弐期）时，应从副标题提取出 S02 且无单集号
    meta_jp_only = MetaInfo(
        title="Otonari no Tenshi-sama 2026 1080p BluRay x265-ADE",
        subtitle="お隣の天使様にいつの間にか駄目人間にされていた件 第弐期 [简繁日字幕]",
        custom_words=[],
    )
    assert meta_jp_only.type == MediaType.TV
    assert meta_jp_only.begin_season == 2
    assert meta_jp_only.begin_episode is None
    assert meta_jp_only.end_episode is None

    # 平行 `/` 译名中无假名但前序片段已声明「第五季」时，后续平行片段的「第5期」同样视为季号对译
    meta_parallel = MetaInfo(
        title="Kingdom S05 2024 1080p BluRay x265-GRP",
        subtitle="王者天下 第五季 / Kingdom 第5期",
        custom_words=[],
    )
    assert meta_parallel.begin_season == 5
    assert meta_parallel.begin_episode is None


@pytest.mark.parametrize("use_rust", [True, False])
def test_chinese_variety_show_period_remains_episode_even_when_matching_season(
    monkeypatch,
    use_rust: bool,
):
    """国产综艺的「第N期」（包括第N季的第N期单集）仍必须识别为集数，不得误判为整季包。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", use_rust)
    clear_rust_parse_options_cache()

    # 刁钻边界：第二季的第2期（季号 == 期号 == 2，且主标题未写 E02）
    variety_same_num = MetaInfo(
        title="Keep Running S02 20180420 1080p WEB-DL H264 AAC-HDSWEB",
        subtitle="奔跑吧 第二季 第2期 嘉宾：特辑",
        custom_words=[],
    )
    assert variety_same_num.type == MediaType.TV
    assert variety_same_num.begin_season == 2
    assert variety_same_num.begin_episode == 2
    assert variety_same_num.end_episode is None
    assert variety_same_num.total_episode == 1

    # 常规综艺单期：第18期
    variety_ep18 = MetaInfo(
        title="Hello Saturday 20260502 1080p WEB-DL H264 AAC",
        subtitle="你好星期六 第18期",
        custom_words=[],
    )
    assert variety_ep18.type == MediaType.TV
    assert variety_ep18.begin_episode == 18
    assert variety_ep18.total_episode == 1


def test_rust_fast_path_reconciles_without_python_builder_fallback(monkeypatch):
    """启用 Rust 时应在快路径完成副标题校正，不回退重型 _build_meta_info。"""
    from app.utils import rust_accel

    if not rust_accel.is_available():
        pytest.skip("当前环境未安装 moviepilot_rust")
    monkeypatch.setattr(settings, "RUST_ACCEL", True)
    clear_rust_parse_options_cache()

    with patch("app.core.metainfo._build_meta_info", side_effect=AssertionError("不应回退 Python")):
        meta = MetaInfo(
            title="The Angel Next Door Spoils Me Rotten S02 2026 1080p BluRay x265-ADE",
            subtitle="关于邻家的天使大人 第二季 / お隣の天使様にいつの間にか駄目人間にされていた件 第2期 [01-12]",
            custom_words=[],
        )
    assert meta.begin_season == 2
    assert meta.begin_episode == 1
    assert meta.end_episode == 12
    assert meta.total_episode == 12
