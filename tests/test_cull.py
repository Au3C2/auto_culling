"""
tests/test_cull.py — pytest suite for F1 photo culling pipeline.

The truth lives in tests/baselines/deterministic.json (CULL_DETERMINISTIC=1,
CPU-only, software decode).  Rating expectations are derived from the jpg
section there so this gate and the deterministic gate cannot diverge.
"""

import os
import subprocess
import sys
import shutil
from pathlib import Path
import csv
import pytest

sys.path.append(os.getcwd())

from conftest import baseline_jpg_ratings  # noqa: E402


@pytest.fixture
def test_env(tmp_path):
    """Fixture to set up a clean test environment with sample images."""
    src_dir = Path("tests/test_img")
    for f in src_dir.glob("*.jpg"):
        shutil.copy(f, tmp_path)
    return tmp_path


def run_cull(input_dir: Path, backend: str, csv_path: Path | None = None, workers: int = 4):
    """Helper to run the cull_photos script."""
    env = os.environ.copy()
    env["CULL_BACKEND"] = backend
    env["PYTHONPATH"] = os.getcwd()

    cmd = [
        sys.executable, "cull_photos.py",
        "--input-dir", str(input_dir),
        "--workers", str(workers),
        "--force",
        # Guards pin the P4 policy: the user-facing CLI default is `never`,
        # but the committed baselines were locked with P4 active.
        "--p4-policy", "always"
    ]
    if csv_path is not None:
        cmd += ["--dump-scores", str(csv_path)]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def read_scores_csv(csv_path: Path) -> dict[str, int]:
    """Parse the --dump-scores CSV into {filename: rating}."""
    scores = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            scores[row["filename"]] = int(row["rating"])
    return scores


@pytest.mark.parametrize("backend", ["onnx"] + (["coreml"] if sys.platform == "darwin" else []))
def test_cull_execution(test_env, backend):
    """Test that the script executes successfully and produces a scores CSV."""
    csv_path = test_env / "scores.csv"
    proc = run_cull(test_env, backend, csv_path)
    assert proc.returncode == 0, f"Script failed with stderr: {proc.stderr}"

    jpgs = list(test_env.glob("*.jpg"))
    scores = read_scores_csv(csv_path)
    assert len(scores) == len(jpgs), f"Expected {len(jpgs)} score rows, found {len(scores)}"


def test_labels_correctness(test_env):
    """Verify that 15th images are rejected (Rating -1) and kept images come from the 14th."""
    csv_path = test_env / "scores.csv"
    run_cull(test_env, "onnx", csv_path)
    scores = read_scores_csv(csv_path)

    for name, rating in scores.items():
        if "20260315" in name:
            assert rating == -1, f"Image {name} should be REJECTED (Rating -1)"

    kept = [name for name, rating in scores.items() if rating > 0]
    assert kept, "expected at least one kept image"
    assert all("20260314" in name for name in kept), \
        f"kept images must come from the 14th: {kept}"


@pytest.mark.precision
@pytest.mark.parametrize("workers", [1, 4, 6])
def test_rating_precision_matches_baseline(test_env, workers, deterministic_env):
    """Compare per-image ratings against deterministic truth across worker counts (1, 4, 6).

    Runs under CULL_DETERMINISTIC=1 so the expectation is the committed
    truth in tests/baselines/deterministic.json (jpg section).  The
    determinism guarantee is cross-platform (macOS ANE / Windows differ
    otherwise — IMG_20260314_160318_240.jpg is a 0.016-logit knife-edge).
    """
    baseline = baseline_jpg_ratings()
    csv_path = test_env / f"scores_w{workers}.csv"
    proc = run_cull(test_env, "onnx", csv_path, workers=workers)
    assert proc.returncode == 0, f"Script failed with stderr: {proc.stderr}"
    actual = read_scores_csv(csv_path)

    for name, expected in baseline.items():
        assert actual.get(name) == expected, \
            f"{name} (workers={workers}): expected rating {expected}, got {actual.get(name)}"
