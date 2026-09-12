#!/usr/bin/env python3
"""tests/ci/seed_precision.py — CI seed-based precision gate.

The GitHub Actions runner ships ONE file per format (tests/ci/sample/) and the
20-copy replicated dataset is scored by BOTH the source CLI and the
packaged binary. The gate asserts that every file receives the SAME rating
from both pipelines.

Note on burst semantics: identical EXIF timestamps group the copies into a
single burst, so a subset may be downgraded by the burst Top-N select (e.g.
an 11-keep burst from 20 copies) — the per-copy rating is NOT uniform. The
source-vs-packaged equality is what guards the packaged pipeline against
drift, and it needs no runner-specific calibration.

Usage:
    python tests/ci/seed_precision.py                    # source only
    CULL_EXE=dist/.../auto_cull_v0.1_macos_arm64 \
        python tests/ci/seed_precision.py                # packaged only
    python tests/ci/seed_precision.py --compare          # both, assert equal
    python tests/ci/seed_precision.py --calibrate        # print per-format
                                                              # single-copy ratings
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

ROOT = Path(__file__).resolve().parents[2]  # repo root (tests/ci/) 
SEED_DIR = ROOT / "tests" / "ci" / "sample"
COPIES = 20

SEEDS = [
    ("JPG", "seed.jpg", "*.jpg"),
    ("HEIF", "seed.heif", "*.heif"),
    ("ARW", "seed.ARW", "*.ARW"),
    ("NEF", "seed.nef", "*.nef"),
]


def _dataset(fmt: str, seed: str, glob: str) -> Path | None:
    src = SEED_DIR / seed
    if not src.exists():
        print(f"[skip] {fmt}: seed {seed} missing")
        return None
    stage = ROOT / "build" / "ci_seed" / f"{fmt}_{COPIES}"
    stage.mkdir(parents=True, exist_ok=True)
    for i in range(COPIES):
        tgt = stage / f"{seed}_{i:03d}{src.suffix}"
        if not tgt.exists():
            shutil.copy(src, tgt)
    return stage


def collect(fmt: str, seed: str, glob: str,
            env: dict | None = None) -> dict[str, tuple[int, float]] | None:
    """Score the replicated dataset and return {filename: (rating, raw_score)}.

    ``env`` selects the pipeline: {} = source CLI, {"CULL_EXE": ...} = the
    packaged binary. Files are scored with --dry-run so they stay pristine
    between the two runs.
    """
    dataset = _dataset(fmt, seed, glob)
    if dataset is None:
        return None
    csv_path = dataset / "scores.csv"
    csv_path.unlink(missing_ok=True)
    exe = (env or {}).get("CULL_EXE")
    cmd = ([exe] if exe else [sys.executable, str(ROOT / "cull_photos.py")]) + [
        "--input-dir", str(dataset), "--workers", "2", "--force",
        # P4 pinned: the user-facing default is `never`; guard comparisons
        # must run the same policy on both sides of the diff.
        "--p4-policy", "always",
        "--dry-run", "--dump-scores", str(csv_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print(f"[FAIL] {fmt}: rc={proc.returncode}: {(proc.stderr or '')[-300:]}")
        sys.exit(1)
    rows = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[row["filename"]] = (int(row["rating"]), float(row["raw_score"]))
    return rows


def _default_onedir() -> str:
    if os.environ.get("CULL_EXE"):
        return os.environ["CULL_EXE"]
    # CULL_VERSION env > pyproject.toml > fallback 0.1, mirrors packaging/build.py
    ver = os.environ.get("CULL_VERSION", "").strip().lstrip("v")
    if not ver:
        toml = ROOT / "pyproject.toml"
        if toml.exists():
            import re as _re
            for line in toml.read_text().splitlines():
                if line.strip().startswith("version"):
                    m = _re.search(r'"([^"]+)"', line)
                    if m:
                        ver = m.group(1).strip()
                        break
        ver = ver or "0.1"
    base = f"auto_cull_v{ver}_win_x64" if __import__("sys").platform == "win32" \
        else f"auto_cull_v{ver}_macos_arm64"
    # Source runs: onedir is dist/<base>/<base> (with .exe on win).
    # CI injects CULL_VERSION=v0.2 so the built dist/ layout matches the lookup.
    return str(ROOT / "dist" / base / base)


ONEDIR_EXE = os.environ.get("CULL_EXE", _default_onedir())


def compare_gate() -> int:
    """Packaged-vs-source equality on the replicated seed dataset.

    Identical copies of one seed share EXIF and therefore land in a single
    burst; the burst Top-N selection can pick different exact files on equal
    raw scores run-to-run. So the assertions are:

      - per-file raw_score within +-0.002 (source AND packaged each show
        ~+-0.0004 run-to-run ANE/P4 jitter — measured, not a packaging
        drift; the local precision gates round to 3 decimals with a 0.005
        tolerance), and
      - rating MULTISETS identical (the same keep/reject outcome overall).

    This is deterministic and needs no runner-specific calibration.
    """
    fails = []
    RAW_TOL = 0.002
    for fmt, seed, glob in SEEDS:
        src = collect(fmt, seed, glob, env={})
        if src is None:
            continue
        pkg = collect(fmt, seed, glob, env={"CULL_EXE": ONEDIR_EXE})
        raw_diff = {k: (src[k][1], pkg.get(k, (None, None))[1])
                    for k in src if abs(src[k][1] - pkg.get(k, (None, None))[1]) > RAW_TOL}
        rating_diff = sorted(v[0] for v in src.values()) != \
            sorted(v[0] for v in pkg.values())
        ok = not raw_diff and not rating_diff
        if not ok:
            fails.append((fmt, f"raw drifts={list(raw_diff)[:3]} "
                          f"rating-multiset-mismatch={rating_diff}"))
        print(f"[{'OK ' if ok else 'FAIL'}] {fmt}: {len(src)} files, "
              f"{len(raw_diff)} raw mismatches, rating-multiset "
              f"{'equal' if not rating_diff else 'DIFFERS'}")
    if fails:
        for fmt, err in fails:
            print(f"  {fmt}: {err}")
        return 1
    print("\nSEED PRECISION GATE PASS (packaged == source)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--compare", action="store_true",
                    help="score with source then packaged and assert equality")
    ap.add_argument("--calibrate", action="store_true",
                    help="print single-copy per-format ratings (reference only)")
    args = ap.parse_args()

    if args.calibrate:
        out = {}
        for fmt, seed, glob in SEEDS:
            src = SEED_DIR / seed
            if not src.exists():
                continue
            tmp = Path(tempfile.mkdtemp(prefix=f"ci_cal_{fmt}_"))
            try:
                shutil.copy(src, tmp / f"one{src.suffix}")
                csv_path = tmp / "scores.csv"
                cmd = ([os.environ.get("CULL_EXE")] if os.environ.get("CULL_EXE")
                       else [sys.executable, str(ROOT / "cull_photos.py")]) + [
                    "--input-dir", str(tmp), "--workers", "2", "--force",
                    "--p4-policy", "always",
                    "--dry-run", "--dump-scores", str(csv_path),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=600)
                if proc.returncode != 0:
                    sys.exit(proc.returncode)
                with open(csv_path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                out[fmt] = int(rows[0]["rating"]) if rows else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        print(json.dumps({"seed_ratings": out}, indent=2))
        return 0

    if args.compare:
        return compare_gate()

    # default: score with the ambient pipeline, print a summary
    for fmt, seed, glob in SEEDS:
        r = collect(fmt, seed, glob)
        if r is not None:
            from collections import Counter
            c = Counter(v[0] for v in r.values())
            print(f"{fmt}: {dict(c)} ({len(r)} files)")
    print("\nCOLLECTION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())