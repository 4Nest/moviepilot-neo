"""离线测量固定万条语料通过后端完整入口的正确性和耗时。"""

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import statistics
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.testing.bootstrap import prepare_backend


def main() -> None:
    """在临时配置中预热并重复复放标题与路径，可单独比较 Python 兜底。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-only", action="store_true")
    parser.add_argument("--loops", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if min(args.loops, args.repeats) < 1:
        parser.error("loops 和 repeats 必须大于零")
    prepare_backend()
    # 独立脚本必须先完成临时配置隔离，再加载会读取配置的后端入口。
    from app.core.metainfo import MetaInfo, MetaInfoPath

    payload = gzip.decompress(
        (Path(__file__).resolve().parents[1] / "tests/cases/bt_pt_10k.jsonl.gz").read_bytes()
    )
    rows = [json.loads(line) for line in payload.splitlines()]

    def parse(row: dict):
        if row["entrypoint"] == "path":
            return MetaInfoPath(Path(row["title"]), custom_words=["#"])
        return MetaInfo(row["title"], row.get("subtitle"), custom_words=["#"])

    failures = []
    timings = []
    with ExitStack() as stack:
        if args.python_only:
            stack.enter_context(patch("app.core.metainfo.rust_accel.is_enabled", new=lambda: False))
        else:
            stack.enter_context(patch(
                "app.core.metainfo._build_meta_info", side_effect=AssertionError("意外回退 Python")
            ))
        for row in rows:
            actual = parse(row)
            differences = {}
            for key, expected in row["expected"].items():
                value = getattr(actual, key)
                if key == "type":
                    value = value.value
                if value != expected:
                    differences[key] = {"expected": expected, "actual": value}
            if differences:
                failures.append({"id": row["id"], "differences": differences})
        for _ in range(args.repeats):
            start = time.perf_counter()
            for _ in range(args.loops):
                for row in rows:
                    parse(row)
            timings.append((time.perf_counter() - start) * 1000 / args.loops)
    report = {
        "mode": "python" if args.python_only else "rust",
        "version": importlib.metadata.version("moviepilot-rust"),
        "sha256_uncompressed": hashlib.sha256(payload).hexdigest(),
        "total": len(rows), "passed": len(rows) - len(failures),
        "median_ms": statistics.median(timings), "timings_ms": timings,
        "loops": args.loops, "repeats": args.repeats,
    }
    args.output.write_text(
        json.dumps({**report, "failures": failures}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
