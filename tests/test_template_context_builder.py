#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
TemplateContextBuilder 的并发安全单元测试。

历史上 builder 持有 ``self._context`` 实例字段，``build()`` 内 ``clear()`` →
``_add_*`` → 推导式返回这一序列在 ``TRANSFER_THREADS > 1`` 下会被多线程相互
覆盖，导致同一 builder 实例并发调用产生互相串味的 rename_dict。本测试在多
线程下连续调用 ``build()``，校验每个线程拿到的字典只反映自己的入参。
"""
import threading

import pytest
from jinja2 import Template

from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.message import TemplateContextBuilder
from app.schemas.types import MediaType
from app.schemas.tmdb import TmdbEpisode


THREAD_COUNT = 8
ITERATIONS_PER_THREAD = 200


@pytest.mark.parametrize("prefix, resource_team, expected", [
    ("[三明治摆烂组&LoliHouse]", "三明治摆烂组@LoliHouse", "三明治摆烂组&LoliHouse"),
    ("[LoliHouse&三明治摆烂组]", "LoliHouse@三明治摆烂组", "LoliHouse&三明治摆烂组"),
    ("[三明治摆烂组 & LoliHouse]", "三明治摆烂组@LoliHouse", "三明治摆烂组 & LoliHouse"),
    ("【三明治摆烂组&LoliHouse】", "三明治摆烂组@LoliHouse", "三明治摆烂组&LoliHouse"),
    ("[DMG&VCB-Studio&LoliHouse]", "DMG@VCB-Studio@LoliHouse", "DMG&VCB-Studio&LoliHouse"),
    ("[A+B&组(1)]", "A+B@组(1)", "A+B&组(1)"),
    ("[三明治摆烂组@LoliHouse]", "三明治摆烂组@LoliHouse", "三明治摆烂组@LoliHouse"),
    ("[三明治摆烂组][LoliHouse]", "三明治摆烂组@LoliHouse", "三明治摆烂组@LoliHouse"),
    ("[LoliHouse]", "LoliHouse", "LoliHouse"),
    ("", "三明治摆烂组@LoliHouse", "三明治摆烂组@LoliHouse"),
    ("[不相关组&LoliHouse]", "三明治摆烂组@LoliHouse", "三明治摆烂组@LoliHouse"),
    ("[三明治摆烂组&LoliHouse&未知组]", "三明治摆烂组@LoliHouse", "三明治摆烂组&LoliHouse&未知组"),
    ("[未知字幕组&LoliHouse]", "LoliHouse", "未知字幕组&LoliHouse"),
    ("[CHS&CHT]", "LoliHouse", "LoliHouse"),
    ("[../未知组&LoliHouse]", "LoliHouse", "LoliHouse"),
])
def test_rename_template_preserves_original_joint_group(prefix, resource_team, expected):
    """实际渲染的文件名保留原联合组分隔符，订阅使用的内部组名保持不变。"""
    meta = _build_fake_meta()
    meta.title = f"{prefix} Anime S01E01 [1080p].mkv"
    meta.resource_team = resource_team
    context = TemplateContextBuilder().build(meta=meta, file_extension=".mkv")

    renamed = Template("Anime - S01E01 - {{releaseGroup}}{{fileExt}}").render(context)

    assert renamed == f"Anime - S01E01 - {expected}.mkv"
    assert meta.resource_team == resource_team
    assert context["original_name"] == meta.title


@pytest.mark.parametrize("rust_enabled", [False, True])
def test_rename_template_preserves_joint_group_from_real_metadata(monkeypatch, rust_enabled):
    """从真实动漫解析到模板渲染的流程应保留 &，无需调整重命名模板。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUST_ACCEL", rust_enabled)
    meta = MetaInfo(
        "[三明治摆烂组&LoliHouse] Kyouran Reijou Nia Liston - 01 "
        "[WebRip 1080p HEVC-10bit AAC][简繁日内封字幕].mkv"
    )
    context = TemplateContextBuilder().build(meta=meta, file_extension=".mkv")
    renamed = Template("{{name}} - {{season_episode}} - {{releaseGroup}}{{fileExt}}").render(context)

    assert context["releaseGroup"] == "三明治摆烂组&LoliHouse"
    assert renamed.endswith(" - 三明治摆烂组&LoliHouse.mkv")
    assert "LoliHouse" in meta.resource_team
    assert meta.resource_team == "三明治摆烂组&LoliHouse"


def _build_fake_meta():
    """
    构造模板上下文测试所需的最小元数据对象。
    """
    meta = type("FakeMeta", (), {})()
    meta.begin_episode = None
    meta.title = "Movie.2024.1080p.x265.10bit.mkv"
    meta.name = "Movie"
    meta.en_name = "Movie"
    meta.year = "2024"
    meta.season_seq = ""
    meta.season = ""
    meta.episode_seqs = ""
    meta.episode = ""
    meta.part = None
    meta.customization = None
    meta.fps = None
    meta.resource_type = None
    meta.resource_effect = None
    meta.edition = ""
    meta.resource_pix = "1080p"
    meta.resource_term = "1080p"
    meta.resource_team = None
    meta.video_encode = "x265 10bit"
    meta.video_bit = "10bit"
    meta.audio_encode = "AAC"
    meta.web_source = None
    return meta


def test_concurrent_build_no_cross_contamination() -> None:
    """
    使用 8 个线程并发调用同一 TemplateContextBuilder 实例的 build()，
    确保各自的 file_extension / 自定义 kwargs 不会被其它线程覆盖。
    """
    builder = TemplateContextBuilder()
    errors = []

    def worker(tag: int) -> None:
        try:
            for _ in range(ITERATIONS_PER_THREAD):
                ctx = builder.build(
                    file_extension=f".{tag}",
                    marker=tag,
                )
                assert ctx.get("fileExt") == f".{tag}"
                assert ctx.get("marker") == tag
        except AssertionError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"builder-{i}")
        for i in range(THREAD_COUNT)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"检测到并发串味，共 {len(errors)} 条；首个错误：{errors[0] if errors else ''}"


def test_build_returns_independent_dicts() -> None:
    """
    连续两次 build() 应返回相互独立的 dict 实例，避免调用方误用共享结果。
    """
    builder = TemplateContextBuilder()
    first = builder.build(file_extension=".a", marker=1)
    second = builder.build(file_extension=".b", marker=2)

    assert first is not second
    assert first.get("fileExt") == ".a"
    assert second.get("fileExt") == ".b"
    assert first.get("marker") == 1


def test_build_exposes_video_bit_from_meta() -> None:
    """
    模板上下文应提供独立 videoBit 字段，避免用户只能从 videoCodec 中手工拆位深。
    """
    context = TemplateContextBuilder().build(meta=_build_fake_meta())

    assert context.get("videoCodec") == "x265 10bit"
    assert context.get("videoBit") == "10bit"


def test_build_exposes_total_episodes_from_current_season() -> None:
    """
    模板上下文应提供当前季总集数，供入库通知模板直接引用。
    """
    context = TemplateContextBuilder().build(
        meta=_build_fake_meta(),
        episodes_info=[
            TmdbEpisode(episode_number=1, name="第一集"),
            TmdbEpisode(episode_number=2, name="第二集"),
            TmdbEpisode(episode_number=3, name="第三集"),
        ],
    )

    assert context.get("total_episodes") == 3


def test_build_preserves_special_season_context() -> None:
    """显式 S00 必须优先于媒体回退季，并使用特别季年份。"""
    meta = MetaInfo("Test Show S00E01")
    mediainfo = MediaInfo(
        title="Test Show",
        type=MediaType.TV,
        season=1,
        season_years={0: "2024", 1: "2025"},
    )

    context = TemplateContextBuilder().build(meta=meta, mediainfo=mediainfo)

    assert context["season"] == "0"
    assert context["season_fmt"] == "S00"
    assert context["season_year"] == "2024"
