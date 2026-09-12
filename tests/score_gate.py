"""Shared helpers for the score-precision gates (HEIF/ARW/NEF).

Runs the CLI against a copied subset of real-camera files and returns the
CSV scores, so the run_*_precision tests can assert per-file rating and
raw_score stability across pipeline changes (decode path, postprocessing,
parallelism).

Not collected by pytest (no `test_` prefix).
"""
import os
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path
import csv

sys.path.append(os.getcwd())


def _cull_command(tmp: str, csv_path: str, workers: int) -> list[str]:
    """Command line for the culling pipeline.

    Honors the CULL_EXE env var (packaged single-file binary from
    packaging/build.py); without it the source CLI under the current
    interpreter is used. Source runs keep PYTHONPATH so the ``cull`` package
    resolves from the repo.
    """
    exe = os.environ.get("CULL_EXE")
    if exe:
        return [exe, "--input-dir", tmp, "--workers", str(workers), "--force",
                "--p4-policy", "always",
                "--dry-run", "--dump-scores", str(csv_path)]
    return [sys.executable, "cull_photos.py",
            "--input-dir", tmp, "--workers", str(workers), "--force",
            "--p4-policy", "always",
            "--dry-run", "--dump-scores", str(csv_path)]


def run_cull_on_copies(src_files: list[Path], workers: int = 4) -> dict[str, tuple[int, float]]:
    """Copy *src_files* to a temp dir, run the CLI, return filename -> (rating, raw_score).

    The CLI runs with ``--force --dry-run --dump-scores``; raw_score is
    rounded to 3 decimals to match the locked baselines.

    ``workers`` defaults to 4: since the engine moved to a decode process pool
    with single-consumer inference (optimization #3), concurrent workers no
    longer share a CUDA session and runs are deterministic. The previous
    shared-session concurrency intermittently dropped detections; that is why
    gates originally pinned workers=1 (see performance_baseline.md).
    """
    tmp = tempfile.mkdtemp(prefix="score_gate_")
    try:
        for p in src_files:
            shutil.copy(p, Path(tmp) / p.name)
        csv_path = Path(tmp) / "scores.csv"
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        proc = subprocess.run(
            _cull_command(tmp, str(csv_path), workers),
            capture_output=True, text=True, env=env, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cull_photos.py failed rc={proc.returncode}: {(proc.stderr or '')[-400:]}")
        rows = {}
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["filename"]] = (
                    int(row["rating"]), round(float(row["raw_score"]), 3),
                    row.get("veto_reason", ""), row.get("n_detections", ""),
                )
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def assert_scores_match(actual: dict[str, tuple[int, float]],
                        baseline: dict[str, tuple[int, float]],
                        what: str,
                        raw_tol: float = 0.005) -> None:
    """Assert per-file rating equality and raw_score within *raw_tol*.

    Rating is always strict. The raw window is per caller: the deterministic
    cross-platform gate passes the platform window (mac ±0.05 / win ±0.005 —
    ARM-vs-x86 decode SIMD cannot be bit-identical), local regression gates
    keep the default 0.005.
    """
    assert set(actual) == set(baseline), \
        f"{what}: file sets differ — missing={set(baseline)-set(actual)} extra={set(actual)-set(baseline)}"
    for name, (exp_rating, exp_raw, *_) in baseline.items():
        act_rating, act_raw, veto, ndet = actual[name]
        assert act_rating == exp_rating, \
            f"{what}::{name}: rating {act_rating} != baseline {exp_rating} (raw {act_raw} vs {exp_raw}, veto={veto!r}, n_det={ndet})"
        assert abs(act_raw - exp_raw) <= raw_tol, \
            f"{what}::{name}: raw_score {act_raw} drifted from baseline {exp_raw} " \
            f"by {abs(act_raw-exp_raw):.4f} > {raw_tol}"