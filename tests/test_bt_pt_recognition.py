"""验证真实 BT/PT 样本通过后端入口的行为及新旧 Rust 能力兼容。"""

import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.metainfo import MetaInfo, MetaInfoPath, _meta_from_rust
from app.utils import rust_accel

_SAMPLES = json.loads(
    (Path(__file__).parent / "cases" / "bt_pt_recognition.json").read_text(encoding="utf-8")
)["samples"]
_EXPANDED_SAMPLES = json.loads(
    (Path(__file__).parent / "cases" / "bt_pt_expanded.json").read_text(encoding="utf-8")
)["samples"]
_VOLUME_PAYLOAD = gzip.decompress(
    (Path(__file__).parent / "cases" / "bt_pt_10k.jsonl.gz").read_bytes()
)
_VOLUME_SAMPLES = [json.loads(line) for line in _VOLUME_PAYLOAD.splitlines()]


def test_volume_corpus_integrity() -> None:
    """后端复放与扩展相同的一万条唯一输入，防止语料复制遗漏。"""
    manifest = json.loads(
        (Path(__file__).parent / "cases" / "bt_pt_10k_manifest.json").read_text(encoding="utf-8")
    )
    assert len(_VOLUME_SAMPLES) == manifest["records"] == 10_000
    assert len({(s["entrypoint"], s["title"], s.get("subtitle")) for s in _VOLUME_SAMPLES}) == 10_000
    assert hashlib.sha256(_VOLUME_PAYLOAD).hexdigest() == manifest["sha256_uncompressed"]


@pytest.mark.parametrize("sample", _VOLUME_SAMPLES, ids=lambda sample: sample["id"])
def test_volume_bt_pt_samples_through_backend(sample: dict) -> None:
    """完整入口验证万条固定标注，并禁止静默回退 Python。"""
    if not rust_accel.is_enabled() or getattr(
        rust_accel._moviepilot_rust, "BT_PT_RECOGNITION_REVISION", 0
    ) < 3:
        pytest.skip("当前扩展尚未包含万条语料回归修复")
    with patch("app.core.metainfo._build_meta_info", side_effect=AssertionError("意外回退 Python")):
        if sample["entrypoint"] == "path":
            meta = MetaInfoPath(Path(sample["title"]), custom_words=["#"])
        else:
            meta = MetaInfo(sample["title"], sample.get("subtitle"), custom_words=["#"])
    actual = {key: getattr(meta, key) for key in sample["expected"]}
    if "type" in actual:
        actual["type"] = actual["type"].value
    assert actual == sample["expected"]


@pytest.mark.parametrize("sample", _SAMPLES, ids=lambda sample: sample["id"])
def test_bt_pt_samples_with_updated_rust(sample: dict) -> None:
    """新扩展必须直接解析真实发布名，不能悄悄回退 Python 掩盖错误。"""
    if not rust_accel.supports_anime_episode_revision():
        pytest.skip("当前 Rust 尚未包含 BT/PT 识别改进")
    with patch("app.core.metainfo._build_meta_info", side_effect=AssertionError("意外回退 Python")):
        meta = MetaInfo(sample["title"], sample.get("subtitle"), custom_words=["#"])
    assert {key: getattr(meta, key) for key in sample["expected"]} == sample["expected"]


@pytest.mark.parametrize("sample", _EXPANDED_SAMPLES, ids=lambda sample: sample["id"])
def test_expanded_bt_pt_samples_through_backend(sample: dict) -> None:
    """完整入口复放独立标注，防止 Python 适配层覆盖 Rust 已修复字段。"""
    if not rust_accel.is_enabled() or getattr(
        rust_accel._moviepilot_rust, "BT_PT_RECOGNITION_REVISION", 0
    ) < 2:
        pytest.skip("当前扩展尚未包含第二轮语料回归修复")
    with patch("app.core.metainfo._build_meta_info", side_effect=AssertionError("意外回退 Python")):
        meta = MetaInfo(sample["title"], sample.get("subtitle"), custom_words=["#"])
    actual = {key: getattr(meta, key) for key in sample["expected"]}
    if "type" in actual:
        actual["type"] = actual["type"].value
    assert actual == sample["expected"]


def test_resolved_aliases_skip_python_reparsing() -> None:
    """Rust 已确认的别名直接使用，避免重复解析覆盖完整中文名。"""
    parsed = {
        "kind": "anime", "title": "[Group] 中文名 / English Title [01][简中]",
        "cn_name": "中文名", "en_name": "English Title", "type": "电视剧",
        "begin_episode": 1, "anime_names_resolved": True,
    }
    with patch("app.core.metainfo.anitopy.parse", side_effect=AssertionError("重复解析")), patch(
        "app.core.metainfo.extract_multilingual_anime_names", side_effect=AssertionError("重复解析")
    ):
        meta = _meta_from_rust(parsed)
    assert meta.cn_name == "中文名"
    assert meta.en_name == "English Title"


@pytest.mark.parametrize("revision, expected", [(None, False), (0, False), (1, True)])
def test_revision_capability_is_explicit(monkeypatch, revision, expected: bool) -> None:
    """未声明能力的旧扩展不能启用修订版快路径。"""
    module = SimpleNamespace()
    if revision is not None:
        module.ANIME_EPISODE_REVISION = revision
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", module)
    monkeypatch.setattr(rust_accel, "is_enabled", lambda: True)
    assert rust_accel.supports_anime_episode_revision() is expected


def test_old_rust_keeps_revision_fallback() -> None:
    """旧扩展仍使用 Python 识别修订集号，不把 v2 当片名或集号。"""
    with patch("app.core.metainfo.rust_accel.supports_anime_episode_revision", return_value=False), patch(
        "app.core.metainfo.rust_accel.parse_metainfo", side_effect=AssertionError("调用旧扩展")
    ):
        meta = MetaInfo("[字幕组] 测试动画 第三季 [01v2][1080p]")
    assert meta.cn_name == "测试动画"
    assert meta.begin_episode == 1
    assert meta.begin_season == 3
