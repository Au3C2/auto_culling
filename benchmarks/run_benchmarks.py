#!/usr/bin/env python3
"""benchmarks/run_benchmarks.py — performance regression gate (macOS).

Splits the gate into two independent measurements per format:

  - **setup tax**: wall time from process start to ``[90%] Analyzing
    images...`` (binary startup, code-signature verification on first run,
    imports, model loading, EXIF scan + burst grouping). Reported and
    guarded with a wide ceiling (2x baseline) — it is machine/platform
    state, not pipeline throughput.
  - **format steady-state E2E**: files / (t[95%] Saving metadata — t[90%]),
    i.e. the pure processing window with all setup excluded. This is the
    guarded metric, locked at ``baseline x 0.9`` (10% downward tolerance).

Protocol (locked 2026-08-27 on Apple M4): ~500 files per format built by
hard-linking the real-camera sources, ``--dry-run --force --workers N``,
two measurements per (format, workers) interleaved, baselines taken from
idle-machine interleaved runs. Packaged (onedir) runs prewarm once so the
kernel code-signature cache is warm (the first-run tax is a platform
property, not a regression signal).

Usage:
    python benchmarks/run_benchmarks.py                # source CLI, workers=4
    CULL_EXE=dist/.../auto_cull_v0.1_macos_arm64 \
        python benchmarks/run_benchmarks.py --workers 6
    python benchmarks/run_benchmarks.py --count 100    # quick smoke (stable
                                                       # numbers need ~500)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYS = sys.executable

# (src_dir, glob, n_sources, copies) — 500-file scale per format.
DATASETS = {
    "JPG":  ("tests/test_img", "*.jpg", 6, 84),   # 504
    "HEIF": ("test_import", "*.heif", 24, 21),    # 504
    "ARW":  ("test_arw", "*.ARW", 20, 25),        # 500
    "NEF":  ("test_nef", "*.nef", 20, 25),        # 500
}

# Steady-state baselines (img/s) — per platform, workers=4, 500-file
# protocol (interleaved, idle, --dry-run --force). Gate = baseline * 0.9.
# Tables are selected by sys.platform (darwin vs win32/linux).  macOS
# entries locked 2026-08-27 on Apple M4; Windows entries locked 2026-08-28
# on the win32 dev box (auto_culling, workers=4, 2-sample median).
# See results/performance_baseline.md "500-Image Production Steady-State Matrix".
STEADY_BASELINES_BY_PLATFORM: dict[str, dict[str, dict[str, float]]] = {
    "darwin": {
        "source": {"JPG": 83.5, "HEIF": 65.5, "ARW": 49.9, "NEF": 70.0},
        "onedir": {"JPG": 82.4, "HEIF": 62.6, "ARW": 48.7, "NEF": 67.9},
    },
    "win32": {
        # Re-locked 2026-08-30 on the win32 dev box (RTX 4070 Ti, workers=4,
        # 500-file protocol, interleaved) AFTER: HEIF HWAccel per-file probe
        # removal (+215% HEIF), win32 static-graph YOLO, DirectML EP
        # (onnxruntime-directml 1.23.0). Session drift is ±10-15% — floors
        # sit just below the worst observed sample.
        "source": {"JPG": 26.0, "HEIF": 31.0, "ARW": 28.0, "NEF": 38.0},
        "onedir": {"JPG": 26.0, "HEIF": 31.0, "ARW": 28.0, "NEF": 38.0},
    },
    "linux": {
        "source": {"JPG": 21.4, "HEIF": 8.0, "ARW": 23.4, "NEF": 31.6},
    },
}
# Compat alias: the default table (macOS) for callers that reference the old name.
STEADY_BASELINES = STEADY_BASELINES_BY_PLATFORM["darwin"]

# Setup-tax ceilings (seconds): per platform, generous 2x — guards gross
# regressions without flagging platform noise.
SETUP_CEILINGS_BY_PLATFORM: dict[str, dict[str, dict[str, float]]] = {
    "darwin": {
        "source": {"JPG": 8.0, "HEIF": 8.0, "ARW": 8.0, "NEF": 8.0},
        "onedir": {"JPG": 12.0, "HEIF": 12.0, "ARW": 12.0, "NEF": 12.0},
    },
    "win32": {
        "source": {"JPG": 16.0, "HEIF": 16.0, "ARW": 20.0, "NEF": 16.0},
        "onedir": {"JPG": 16.0, "HEIF": 16.0, "ARW": 20.0, "NEF": 16.0},
    },
    "linux": {
        "source": {"JPG": 16.0, "HEIF": 16.0, "ARW": 20.0, "NEF": 16.0},
        "onedir": {"JPG": 16.0, "HEIF": 16.0, "ARW": 20.0, "NEF": 16.0},
    },
}
SETUP_CEILINGS = SETUP_CEILINGS_BY_PLATFORM["darwin"]

TOLERANCE = 0.90  # steady-state floor (local default; CI may widen via --tolerance)


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return "linux"


def _load_baselines(path: Path | None) -> dict[str, dict[str, float]]:
    """Return a {pipeline: {fmt: baseline}} map.

    Without *path*, the per-platform built-in baselines are used (selected
    by sys.platform — darwin vs win32/linux). A JSON file with the shape
    {"baselines": {"source": {...}, "onedir": {...}}} replaces the whole
    table — the CI workflow calibrates its own runner-specific baselines
    through ci_config.json.

    When the file also carries a per-platform block
    {"platforms": {"win32": {"baselines": {...}}}}, the entry matching
    ``_platform_key()`` wins. This lets macOS and Windows CI gates share
    one ci_config.json without their calibrations clobbering each other.
    """
    if path is None:
        plat = _platform_key()
        return dict(STEADY_BASELINES_BY_PLATFORM.get(plat, STEADY_BASELINES))
    data = json.loads(path.read_text())
    plat_block = (data.get("platforms") or {}).get(_platform_key()) or {}
    entries = plat_block.get("baselines") or data.get("baselines", data)
    out = {}
    for pipe in ("source", "onedir"):
        out[pipe] = {str(f): float(v)
                     for f, v in (entries.get(pipe) or {}).items()}
    return out


def _exe_name() -> str:
    exe = os.environ.get("CULL_EXE")
    return "onedir" if exe else "source"


TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s")
PCT_RE = re.compile(r"\[(\d+)%\]\s+(.*)$")


def _parse_log(log_path: Path) -> dict[int, float]:
    marks: dict[int, float] = {}
    for line in log_path.read_text(errors="replace").splitlines():
        m = TS_RE.match(line)
        if not m:
            continue
        pm = PCT_RE.search(line)
        if pm:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
            marks[int(pm.group(1))] = ts.timestamp()
    return marks


def build_dataset(fmt: str, count: int, seed_dir: Path | None = None) -> Path:
    """Stage a (hard-)linked dataset of ~*count* files.

    Sources come from the repo datasets (full 6/24/20/20-source sets) unless
    *seed_dir* is given — the CI protocol ships ONE seed file per format and
    replicates it (``copies = count``) on the runner, so datasets stay out of
    the git repo.
    """
    subdir, glob, n_src, _copies = DATASETS[fmt]
    if seed_dir is not None:
        n_src = 1
        copies = count
    else:
        copies = max(1, -(-count // n_src))  # ceil
    n_total = n_src * copies
    tag = f"{fmt}_{n_total}" + (f"_seed" if seed_dir is not None else "")
    stage = ROOT / "build" / "bench_datasets" / tag
    if stage.is_dir() and len(list(stage.iterdir())) >= n_total:
        return stage
    stage.mkdir(parents=True, exist_ok=True)
    src_base = seed_dir if seed_dir is not None else ROOT / subdir
    srcs = sorted(src_base.glob(glob))[:n_src]
    if len(srcs) < n_src:
        raise RuntimeError(f"{fmt}: found {len(srcs)} seed files in {src_base}")
    for s in srcs:
        for i in range(copies):
            tgt = stage / f"{s.stem}_{i:03d}{s.suffix}"
            if not tgt.exists() and not tgt.is_symlink():
                try:
                    os.link(s, tgt)
                except OSError:
                    # Windows cross-volume or CI filesystems without hardlink
                    # support — copy fallback (identical bytes).
                    shutil.copy2(s, tgt)
    return stage


def _command(tmp: Path, workers: int) -> list[str]:
    exe = os.environ.get("CULL_EXE")
    if exe:
        return [exe, "--input-dir", str(tmp), "--workers", str(workers),
                "--force", "--p4-policy", "always", "--dry-run"]
    return [SYS, str(ROOT / "cull_photos.py"), "--input-dir", str(tmp),
            "--workers", str(workers), "--force", "--p4-policy", "always",
            "--dry-run"]


RUN_TIMEOUT = 600  # per engine call; local gate runs in ~3 min per format,
                   # CI VMs are slower — 10 min is a generous hard ceiling so a
                   # hung engine fails loudly instead of stalling the gate


def measure(fmt: str, workers: int, count: int,
            seed_dir: Path | None = None) -> tuple[float, float]:
    """Return (steady_ips, setup_s) for one run on ~*count* files."""
    dataset = build_dataset(fmt, count, seed_dir)
    files = sorted(dataset.glob(DATASETS[fmt][1]))
    n = len(files)
    if n < count:
        # dataset used by the earlier step may not have enough unique copies
        raise RuntimeError(f"{fmt}: only {n} files (need {count})")
    t0 = time.perf_counter()
    wall_epoch0 = time.time()
    try:
        proc = subprocess.run(_command(dataset, workers), capture_output=True,
                              text=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{fmt} TIMED OUT after {RUN_TIMEOUT}s — engine hung "
            f"(workers={workers}, files={n})")
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"{fmt} failed rc={proc.returncode}: {(proc.stderr or '')[-400:]}")
    logs = sorted((dataset / "logs").glob("cull_*.log"),
                  key=lambda p: p.stat().st_mtime)
    marks = _parse_log(logs[-1]) if logs else {}
    if 90 not in marks or 95 not in marks:
        raise RuntimeError(f"{fmt}: no [90%]/[95%] marks (log files={len(logs)})")
    steady = n / (marks[95] - marks[90])
    return steady, marks[90] - wall_epoch0


def _prewarm(workers: int, seed_dir: Path | None = None) -> None:
    picks = [sorted(build_dataset(fmt, DATASETS[fmt][2] * DATASETS[fmt][3], seed_dir)
                    .glob(DATASETS[fmt][1]))[0]
             for fmt in DATASETS]
    tmp = Path(tempfile.mkdtemp(prefix="bench_prewarm_"))
    try:
        for p in picks[:4]:
            shutil.copy(p, tmp / p.name)
        subprocess.run(_command(tmp, workers), capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=600)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--count", type=int, default=None,
                    help="files per dataset (default: full 500-file scale)")
    ap.add_argument("--format", choices=list(DATASETS) + ["ALL"], default="ALL")
    ap.add_argument("--no-prewarm", action="store_true",
                    help="skip packaged-binary dylib/codesign warm-up")
    ap.add_argument("--cooldown", type=int, default=20,
                    help="seconds of idle between formats (thermal drift guard; "
                         "keeps the full 4-format gate under ~5 minutes)")
    ap.add_argument("--samples", type=int, default=3,
                    help="interleaved measurements per format; median is used "
                         "(thermal-drift resistant; CI uses 3-5)")
    ap.add_argument("--seed-dir", type=Path, default=None,
                    help="CI protocol: replicate ONE file per format from this "
                         "directory instead of the full local datasets")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE,
                    help="steady-state floor as a fraction of baseline "
                         "(CI runners may need 0.80)")
    ap.add_argument("--baseline-file", type=Path, default=None,
                    help="JSON calibrating per-pipeline baselines on this runner "
                         "(produced by the CI baseline-calibration job)")
    ap.add_argument("--no-guard", action="store_true",
                    help="measure and print values only — do not assert against "
                         "baselines (used by the CI calibration job)")
    ap.add_argument("--json", type=Path, default=None, dest="json_out",
                    help="write measured values to a JSON file")
    args = ap.parse_args()

    who = _exe_name()
    if not args.no_prewarm:
        _prewarm(args.workers, args.seed_dir)

    base_table = _load_baselines(args.baseline_file)
    if who not in base_table:
        who = "source"
    base = base_table[who]
    ceilings = SETUP_CEILINGS_BY_PLATFORM.get(_platform_key(), SETUP_CEILINGS)[who]
    fmt_list = [args.format] if args.format != "ALL" else list(DATASETS)
    failures = []
    results = {}

    print(f"Performance gate — {who} pipeline, workers={args.workers} "
          f"(steady floor {int(args.tolerance*100)}% of baseline, "
          f"{args.samples} samples/format, median)\n")
    t_gate0 = time.perf_counter()
    for idx, fmt in enumerate(fmt_list):
        if idx:
            time.sleep(args.cooldown)
        n_target = args.count or (
            (500 if args.seed_dir else DATASETS[fmt][2] * DATASETS[fmt][3]))
        # interleaved samples, median aggregates (drift-resistant)
        samples = []
        for s in range(1, args.samples + 1):
            print(f"[measure] {fmt} sample {s}/{args.samples} "
                  f"(workers={args.workers}, ~{n_target} files) ... ", end="",
                  flush=True)
            try:
                steady, setup = measure(fmt, args.workers, n_target, args.seed_dir)
            except RuntimeError as e:
                print("FAILED")
                print(f"[ERR] {fmt}: {e}")
                failures.append((fmt, 0.0, 0.0))
                continue
            print(f"steady {steady:.1f} img/s, setup {setup:.1f}s")
            samples.append((steady, setup))
            time.sleep(2)
        if not samples:
            continue
        samples.sort()
        steady, setup = samples[len(samples) // 2]
        results[fmt] = {"steady": round(steady, 2), "setup": round(setup, 2),
                        "samples": [round(s, 2) for s, _ in samples]}
        floor = base[fmt] * args.tolerance
        flag = "OK " if steady >= floor else "FAIL"
        if steady < floor:
            failures.append((fmt, steady, floor))
        print(f"[{flag}] {fmt:<4} steady {steady:6.2f} img/s "
              f"(floor {floor:5.2f}) | setup {setup:6.2f}s "
              f"(ceiling {ceilings[fmt]:.1f}s)")
        if setup > ceilings[fmt]:
            failures.append((fmt, -setup, -ceilings[fmt]))
            print(f"      setup tax above ceiling!")

    payload = {"pipeline": who, "workers": args.workers,
               "count": args.count or (500 if args.seed_dir else
                                  max(DATASETS[f][2]*DATASETS[f][3] for f in DATASETS)),
               "wall_s": round(time.perf_counter() - t_gate0, 1),
               "platform": sys.platform,
               "results": results}
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2))
    else:
        # Always leave a timestamped artifact for trend tracking (non-guarded
        # --count 100 runs too).  Mirrors the requested explicit --json path.
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            auto_path = ROOT / "build" / f"perf_{ts}_{who}.json"
            auto_path.parent.mkdir(parents=True, exist_ok=True)
            auto_path.write_text(json.dumps(payload, indent=2))
            print(f"(artifact: {auto_path.relative_to(ROOT)})")
        except Exception:
            pass

    gate_s = time.perf_counter() - t_gate0
    print(f"\nGate wall time: {gate_s:.0f}s ({gate_s/60:.1f} min)")
    if gate_s > 300:
        print("NOTE: over the 5-minute budget — lower --count for quicker gates "
              "or re-check machine load.")

    if args.no_guard:
        # calibration mode: measure and report only, never assert baselines
        print("\nNo guard asserted (--no-guard calibration run).")
        return 0
    if failures:
        print("\nREGRESSION — " + ", ".join(
            f"{f} {v:.2f}<{t:.2f}" for f, v, t in failures))
        return 1
    print("\nAll formats within baseline tolerance. Pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())