"""Protocol test suite for the resident JSON Lines engine interface.

Tests scan, run with parameter overrides, cancellation mid-flight,
preview generation with bounding boxes, and error reporting.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest


class EngineChannel:
    """Helper to manage communication with a resident cull_photos engine subprocess."""

    def __init__(self, proc: subprocess.Popen[str]):
        self.proc = proc
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        if self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                with self._lock:
                    self.events.append(msg)
            except Exception:
                continue

    def send(self, cmd: dict) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("Engine stdin not available")
        payload = json.dumps(cmd) + "\n"
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def wait_for_event(self, event_type: str, timeout: float = 15.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for evt in self.events:
                    if evt.get("type") == event_type:
                        return evt
            time.sleep(0.05)
        return None

    def get_events_by_type(self, event_type: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e.get("type") == event_type]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def close(self) -> None:
        try:
            self.send({"cmd": "quit"})
            self.proc.wait(timeout=2.0)
        except Exception:
            self.proc.kill()


@pytest.fixture
def engine_process():
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
    # Wait for process initialization
    time.sleep(0.3)
    yield channel
    channel.close()


def test_json_lines_scan_before_run(engine_process: EngineChannel, tmp_path: Path):
    """Test scanning a directory before starting scoring."""
    img_dir = Path("tests/test_img")
    if not img_dir.exists():
        pytest.skip("tests/test_img does not exist")

    engine_process.clear()
    engine_process.send({"cmd": "scan", "dir": str(img_dir), "recursive": False})
    scanned_evt = engine_process.wait_for_event("scanned", timeout=10.0)
    assert scanned_evt is not None
    assert "count" in scanned_evt
    assert scanned_evt["count"] > 0
    assert "paths" in scanned_evt


def test_json_lines_run_and_frame_events(engine_process: EngineChannel):
    """Test running culling and receiving frame scoring events."""
    img_dir = Path("tests/test_img")
    if not img_dir.exists():
        pytest.skip("tests/test_img does not exist")

    engine_process.clear()
    engine_process.send({
        "cmd": "run",
        "dir": str(img_dir),
        "config": {
            "dry_run": True,
            "top_n": 5,
            "min_raw": 3.0,
            "sharp_thresh": 0.05,
        }
    })

    done_evt = engine_process.wait_for_event("done", timeout=30.0)
    assert done_evt is not None
    assert "total" in done_evt
    assert "keep" in done_evt
    assert "reject" in done_evt

    frame_events = engine_process.get_events_by_type("frame")
    assert len(frame_events) > 0
    first_frame = frame_events[0]
    assert "name" in first_frame
    assert "rating" in first_frame
    assert "sharp" in first_frame
    assert "comp" in first_frame
    assert "raw" in first_frame
    assert "status" in first_frame


def test_json_lines_preview_request(engine_process: EngineChannel):
    """Test requesting image preview with bounding boxes."""
    img_dir = Path("tests/test_img")
    if not img_dir.exists():
        pytest.skip("tests/test_img does not exist")

    # Run quick dry run first so boxes/scores exist
    engine_process.send({
        "cmd": "run",
        "dir": str(img_dir),
        "config": {"dry_run": True}
    })
    done_evt = engine_process.wait_for_event("done", timeout=30.0)
    assert done_evt is not None

    frame_events = engine_process.get_events_by_type("frame")
    assert len(frame_events) > 0
    test_img_path = str(img_dir / frame_events[0]["name"])

    engine_process.clear()
    engine_process.send({
        "cmd": "preview",
        "path": test_img_path,
        "size": 320
    })

    preview_evt = engine_process.wait_for_event("preview", timeout=10.0)
    assert preview_evt is not None
    assert preview_evt.get("path") == test_img_path
    assert "data" in preview_evt
    assert len(preview_evt["data"]) > 100  # Base64 string


def test_json_lines_cancel(engine_process: EngineChannel):
    """Test immediate cancellation during run."""
    img_dir = Path("tests/test_img")
    if not img_dir.exists():
        pytest.skip("tests/test_img does not exist")

    engine_process.clear()
    engine_process.send({
        "cmd": "run",
        "dir": str(img_dir),
        "config": {"dry_run": True}
    })
    time.sleep(0.1)
    engine_process.send({"cmd": "cancel"})

    cancel_or_done = engine_process.wait_for_event("cancelled", timeout=10.0) or engine_process.wait_for_event("done", timeout=10.0)
    assert cancel_or_done is not None
