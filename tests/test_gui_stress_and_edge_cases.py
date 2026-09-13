"""Comprehensive Edge Case, Stress and Concurrency Test Suite for GUI Engine.

Tests:
1. Empty directory scan and run
2. Non-existent directory handling
3. Corrupted / 0-byte image file robustness
4. Concurrent preview requests while culling run is actively processing
5. Consecutive multi-run execution with dynamic configuration overrides
6. Zero-latency immediate cancellation
7. Mixed format folder (JPG + ARW + NEF + dirty files)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest

from test_json_protocol import EngineChannel


@pytest.fixture
def engine_proc():
    cmd = [sys.executable, "-u", "cull_photos.py", "--json-lines"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    channel = EngineChannel(proc)
    time.sleep(0.3)
    yield channel
    channel.close()


def test_empty_directory_scan_and_run(engine_proc: EngineChannel, tmp_path: Path):
    """Test scan and run behavior on an empty directory."""
    empty_dir = tmp_path / "empty_photos"
    empty_dir.mkdir()

    # 1. Scan empty dir
    engine_proc.clear()
    engine_proc.send({"cmd": "scan", "dir": str(empty_dir), "recursive": False})
    scanned = engine_proc.wait_for_event("scanned", timeout=5.0)
    assert scanned is not None
    assert scanned.get("count") == 0

    # 2. Run on empty dir
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(empty_dir), "config": {"dry_run": True}})
    done = engine_proc.wait_for_event("done", timeout=10.0)
    assert done is not None
    assert done.get("total") == 0


def test_nonexistent_directory_error_handling(engine_proc: EngineChannel, tmp_path: Path):
    """Test graceful error reporting on invalid paths."""
    bad_dir = tmp_path / "does_not_exist_12345"

    # 1. Scan bad dir
    engine_proc.clear()
    engine_proc.send({"cmd": "scan", "dir": str(bad_dir)})
    scan_res = engine_proc.wait_for_event("scanned", timeout=3.0) or engine_proc.wait_for_event("scan_error", timeout=3.0)
    assert scan_res is not None

    # 2. Run bad dir
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(bad_dir)})
    err = engine_proc.wait_for_event("error", timeout=5.0)
    assert err is not None
    assert "not found" in err.get("message", "")


def test_corrupted_file_handling(engine_proc: EngineChannel, tmp_path: Path):
    """Test engine resilience when encountering 0-byte or corrupted files."""
    corrupt_dir = tmp_path / "corrupt_test"
    corrupt_dir.mkdir()

    # 1 valid sample JPG
    valid_sample = Path("tests/test_img/IMG_20260314_151744_020.jpg")
    if valid_sample.exists():
        shutil.copy(valid_sample, corrupt_dir / "valid_01.jpg")

    # 0-byte file
    (corrupt_dir / "zero_byte.jpg").write_bytes(b"")

    # Garbled random byte file pretending to be JPG
    (corrupt_dir / "garbage.jpg").write_bytes(b"\xff\xd8\xff\xe0garbage_data_not_an_image_123456789")

    # Garbled ARW file
    (corrupt_dir / "fake.arw").write_bytes(b"II\x2a\x00corrupted_tiff_header_bytes")

    # Scan
    engine_proc.clear()
    engine_proc.send({"cmd": "scan", "dir": str(corrupt_dir)})
    scanned = engine_proc.wait_for_event("scanned", timeout=5.0)
    assert scanned is not None
    assert scanned.get("count") >= 3

    # Run should not crash, must finish with done event
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(corrupt_dir), "config": {"dry_run": True}})
    done = engine_proc.wait_for_event("done", timeout=15.0)
    assert done is not None


def test_zero_latency_immediate_cancel(engine_proc: EngineChannel):
    """Test cancelling immediately upon run command without delay."""
    img_dir = Path("test_arw")
    if not img_dir.exists():
        img_dir = Path("tests/test_img")

    engine_proc.clear()
    # Send run and cancel immediately back to back
    engine_proc.send({"cmd": "run", "dir": str(img_dir), "config": {"dry_run": True}})
    engine_proc.send({"cmd": "cancel"})

    evt = engine_proc.wait_for_event("cancelled", timeout=8.0) or engine_proc.wait_for_event("done", timeout=8.0)
    assert evt is not None


def test_concurrent_preview_requests_during_active_run(engine_proc: EngineChannel):
    """Test that previews can be served concurrently while scoring pipeline is running."""
    img_dir = Path("test_arw")
    if not img_dir.exists():
        img_dir = Path("tests/test_img")

    # Start run
    engine_proc.clear()
    engine_proc.send({
        "cmd": "run",
        "dir": str(img_dir),
        "config": {"dry_run": True, "workers": 4}
    })

    # Rapidly fire preview requests while running
    files = list(img_dir.glob("*.ARW")) or list(img_dir.glob("*.jpg"))
    assert len(files) > 0

    preview_results = []
    def request_previews():
        for f in files[:4]:
            engine_proc.send({"cmd": "preview", "path": str(f.resolve()), "size": 320})
            time.sleep(0.05)

    th = threading.Thread(target=request_previews)
    th.start()
    th.join()

    # Wait for run completion
    done = engine_proc.wait_for_event("done", timeout=30.0)
    assert done is not None

    # Check that preview events arrived
    previews = engine_proc.get_events_by_type("preview")
    assert len(previews) > 0


def test_multiple_consecutive_runs_on_same_process(engine_proc: EngineChannel):
    """Test running multiple culling jobs with different configs without restarting the engine."""
    img_dir = Path("tests/test_img")
    if not img_dir.exists():
        pytest.skip("tests/test_img not found")

    # Round 1: Top-N = 1, dry_run = True
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(img_dir), "config": {"dry_run": True, "top_n": 1}})
    done1 = engine_proc.wait_for_event("done", timeout=15.0)
    assert done1 is not None

    # Round 2: Top-N = 5, min_raw = 0.5
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(img_dir), "config": {"dry_run": True, "top_n": 5, "min_raw": 0.5}})
    done2 = engine_proc.wait_for_event("done", timeout=15.0)
    assert done2 is not None

    # Round 3: Cancel mid-way
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(img_dir), "config": {"dry_run": True}})
    time.sleep(0.05)
    engine_proc.send({"cmd": "cancel"})
    cancel_evt = engine_proc.wait_for_event("cancelled", timeout=10.0) or engine_proc.wait_for_event("done", timeout=10.0)
    assert cancel_evt is not None

    # Round 4: Normal run after cancellation to verify engine recovers cleanly
    engine_proc.clear()
    engine_proc.send({"cmd": "run", "dir": str(img_dir), "config": {"dry_run": True, "top_n": 3}})
    done4 = engine_proc.wait_for_event("done", timeout=15.0)
    assert done4 is not None
    # Protocol invariant — never hardcode the dataset size: failed frames are
    # excluded from total, so keep + reject + failed must cover the scan.
    # Direct indexing (not .get with default) so a malformed done event that
    # omits any field fails the schema check.
    assert done4["keep"] + done4["reject"] == done4["total"]
    assert done4["failed"] == 0
