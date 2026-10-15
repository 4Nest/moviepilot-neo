"""副标题专项回归：验证新扩展完整入口及 Python 回退路径。"""

import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.metainfo import MetaInfo
from app.utils import rust_accel

ROOT = Path(__file__).parent / "cases"
PAYLOAD = gzip.decompress((ROOT / "subtitle_10k.jsonl.gz").read_bytes())
SAMPLES = [json.loads(line) for line in PAYLOAD.splitlines()]


def test_subtitle_corpus_integrity() -> None:
    """确保后端使用与 Rust 相同的一万条独立输入。"""
    manifest = json.loads((ROOT / "subtitle_10k_manifest.json").read_text())
    assert len(SAMPLES) == manifest["records"] == 10_000
    assert len({(row["title"], row["subtitle"]) for row in SAMPLES}) == 10_000
    assert hashlib.sha256(PAYLOAD).hexdigest() == manifest["sha256_uncompressed"]


def _check_sample(sample: dict) -> None:
    meta = MetaInfo(sample["title"], sample["subtitle"], custom_words=["#"])
    actual = {key: getattr(meta, key) for key in sample["expected"]}
    if "type" in actual:
        actual["type"] = actual["type"].value
    assert actual == sample["expected"]
    assert meta.subtitle == sample["subtitle"].strip()


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda row: row["id"])
def test_subtitle_with_rust(sample: dict) -> None:
    """新扩展必须直接通过全部副标题样本，不能靠静默回退掩盖错误。"""
    if not rust_accel.is_enabled() or getattr(rust_accel._moviepilot_rust, "BT_PT_RECOGNITION_REVISION", 0) < 4:
        pytest.skip("当前扩展尚未包含副标题专项修复")
    with patch("app.core.metainfo._build_meta_info", side_effect=AssertionError("意外回退 Python")):
        _check_sample(sample)


@pytest.mark.parametrize("sample", [row for row in SAMPLES if row["python_compatible"]], ids=lambda row: row["id"])
def test_subtitle_with_python(sample: dict) -> None:
    """关闭 Rust 后仍保护字段优先级、范围有效性和描述隔离。"""
    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        _check_sample(sample)
