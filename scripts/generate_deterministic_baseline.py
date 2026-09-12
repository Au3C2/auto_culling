#!/usr/bin/env python3
"""
scripts/generate_deterministic_baseline.py — Regenerate tests/baselines/deterministic.json.

Runs the full pipeline with CULL_DETERMINISTIC=1 (CPU-only ORT, software
decode, single-threaded FFT) on all precision gate datasets and writes the
per-file (rating, raw_score) truth.  Any platform can run this — the output
must be identical across macOS and Windows when deterministic mode is correct.

Datasets (when present):
  tests/test_img/*.jpg  (6 JPG)
  test_import/*.heif    (24 HEIF)
  test_arw/*.ARW        (20 ARW)
  test_nef/*.nef        (20 NEF)

Usage:
    python scripts/generate_deterministic_baseline.py
    python scripts/generate_deterministic_baseline.py --out tests/baselines/deterministic.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep in sync with tests/test_precision_heif.py etc. so the baseline covers
# exactly the gate files.
JPG_NAMES = [
    "IMG_20260314_151744_020.jpg",
    "IMG_20260314_160317_680.jpg",
    "IMG_20260314_160318_240.jpg",
    "IMG_20260314_160343_870.jpg",
    "IMG_20260314_160344_380.jpg",
    "IMG_20260315_150404_550.jpg",
]
HEIF_NAMES = [
    "DSC00827.heif", "DSC00845.heif", "DSC00849.heif", "DSC00851.heif",
    "DSC00879.heif", "DSC00880.heif", "DSC00886.heif", "DSC00887.heif",
    "DSC00888.heif", "DSC00890.heif", "DSC00892.heif", "DSC00893.heif",
    "DSC00894.heif", "DSC00895.heif", "DSC00896.heif", "DSC00897.heif",
    "DSC00942.heif", "DSC00951.heif", "DSC00952.heif", "DSC00958.heif",
    "DSC00959.heif", "DSC00960.heif", "DSC00961.heif", "DSC00962.heif",
]
ARW_NAMES = [
    "DSC00827.ARW", "DSC00845.ARW", "DSC00849.ARW", "DSC00851.ARW",
    "DSC00879.ARW", "DSC00880.ARW", "DSC00886.ARW", "DSC00887.ARW",
    "DSC00888.ARW", "DSC00890.ARW", "DSC00892.ARW", "DSC00893.ARW",
    "DSC00894.ARW", "DSC00895.ARW", "DSC00896.ARW", "DSC00897.ARW",
    "DSC00942.ARW", "DSC00951.ARW", "DSC00952.ARW", "DSC00958.ARW",
]
NEF_NAMES = [
    "IMG_20260315_164102_480.nef", "IMG_20260315_164102_540.nef",
    "IMG_20260315_164102_600.nef", "IMG_20260315_164102_660.nef",
    "IMG_20260315_164102_730.nef", "IMG_20260315_164102_790.nef",
    "IMG_20260315_164133_610.nef", "IMG_20260315_164133_680.nef",
    "IMG_20260315_164133_750.nef", "IMG_20260315_164133_810.nef",
    "IMG_20260315_164133_870.nef", "IMG_20260315_164133_930.nef",
    "IMG_20260315_164133_990.nef", "IMG_20260315_164134_050.nef",
    "IMG_20260315_164134_110.nef", "IMG_20260315_164136_090.nef",
    "IMG_20260315_164136_160.nef", "IMG_20260315_164136_220.nef",
    "IMG_20260315_164136_280.nef", "IMG_20260315_164136_340.nef",
]


def _collect(src_files: list[Path]) -> dict[str, tuple[int, float]]:
    tmp = Path(tempfile.mkdtemp(prefix="det_bl_"))
    try:
        for p in src_files:
            shutil.copy(p, tmp / p.name)
        csv_path = tmp / "scores.csv"
        env = os.environ.copy()
        env["CULL_DETERMINISTIC"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        cmd = [sys.executable, str(ROOT / "cull_photos.py"),
               "--input-dir", str(tmp), "--workers", "4",
               "--force", "--p4-policy", "always",
               "--dry-run", "--dump-scores", str(csv_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"cull_photos failed: {(proc.stderr or '')[-800:]}")
        rows: dict[str, tuple[int, float]] = {}
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["filename"]] = (int(row["rating"]), round(float(row["raw_score"]), 3))
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "tests/baselines/deterministic.json",
                    help="output JSON path")
    args = ap.parse_args()

    try:
        import onnxruntime  # noqa: F401
        ort_ver = __import__("onnxruntime").__version__
    except Exception:
        ort_ver = "unknown"

    out: dict = {
        "_note": "Deterministic baseline — CULL_DETERMINISTIC=1 (CPU-only ORT, software decode, single-threaded kernels). Re-generate with scripts/generate_deterministic_baseline.py after intentional scoring changes.",
        "meta": {
            "generated": __import__("datetime").date.today().isoformat(),
            "platform": sys.platform,
            "onnxruntime": ort_ver,
            "deterministic": True,
            "scale_width": 1280,
            "tolerance_rating": "exact",
            "tolerance_raw": 0.005,
        },
    }

    datasets = [
        ("jpg", ROOT / "tests/test_img", JPG_NAMES),
        ("heif", ROOT / "test_import", HEIF_NAMES),
        ("arw", ROOT / "test_arw", ARW_NAMES),
        ("nef", ROOT / "test_nef", NEF_NAMES),
    ]
    for key, dirpath, names in datasets:
        srcs = [dirpath / n for n in names if (dirpath / n).exists()]
        if not srcs:
            print(f"[skip] {key}: no files in {dirpath}")
            continue
        print(f"[collect] {key}: {len(srcs)} files ...", flush=True)
        rows = _collect(srcs)
        out[key] = {fn: [r, s] for fn, (r, s) in sorted(rows.items())}
        print(f"  -> {len(rows)} rows")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
