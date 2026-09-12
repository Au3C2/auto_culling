"""JSON Lines protocol handler and event serialization for the Tauri GUI engine.

Outputs line-delimited JSON to stdout with strict mutex synchronization
to prevent interleaved stdout corruption across worker threads.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from typing import Any

_STDOUT_LOCK = threading.Lock()


def _default_serializer(obj: Any) -> Any:
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "__float__"):
        return float(obj)
    if hasattr(obj, "__int__"):
        return int(obj)
    return str(obj)


def emit(payload: dict[str, Any]) -> None:
    """Thread-safe serialized line-delimited JSON emitter."""
    with _STDOUT_LOCK:
        try:
            line = json.dumps(payload, ensure_ascii=False, default=_default_serializer) + "\n"
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


class JsonLinesHandler(logging.Handler):
    """Logging handler that parses structured engine logs and emits protocol events."""

    _RE_GROUP = re.compile(r"Processing Group (\d+)/(\d+) \((\d+) frames\)")
    _RE_FRAME = re.compile(
        r"\[([^\]]+)\]\s+sharp=([0-9.]+)\s+comp=([0-9.]+)\s+raw=([0-9.]+)\s+Rating=([+-]?\d+)(?:\s+(?:\(([^)]+)\)|\[veto:([^\]]+)\]))?"
    )
    _RE_MANUAL = re.compile(r"\[([^\]]+)\]\s+Manual metadata:\s+Rating=([+-]?\d+)")
    _RE_DECODE_ERR = re.compile(r"\[([^\]]+)\]\s+Decode failed")

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()

        # Check burst group event
        mg = self._RE_GROUP.search(msg)
        if mg:
            emit({
                "type": "group",
                "done": int(mg.group(1)),
                "total": int(mg.group(2)),
                "frames": int(mg.group(3)),
            })
            return

        # Check scored frame event. In GUI mode the engine logs the FULL path
        # so photos are keyed unambiguously (duplicate basenames across
        # recursive dirs); CLI mode logs the bare filename.
        mf = self._RE_FRAME.search(msg)
        if mf:
            name_raw, sharp, comp, raw, rating, veto1, veto2 = mf.groups()
            veto = veto1 or veto2 or ""
            if "/" in name_raw or "\\" in name_raw:
                path = name_raw
                name = name_raw.replace("\\", "/").rsplit("/", 1)[-1]
            else:
                path = None
                name = name_raw
            status = {
                "decode_failed": "decode_failed",
                "manual_metadata": "manual_metadata",
                "topn_final": "topn_final",
            }.get(veto, "scored")
            emit({
                "type": "frame",
                "name": name,
                "path": path,
                "rating": int(rating),
                "sharp": float(sharp),
                "comp": float(comp),
                "raw": float(raw),
                "veto": "" if status == "topn_final" else veto,
                "status": status,
            })
            return

        # Check manual metadata keep/skip
        mm = self._RE_MANUAL.search(msg)
        if mm:
            name, rating = mm.groups()
            emit({
                "type": "frame",
                "name": name,
                "rating": int(rating),
                "sharp": 0.0,
                "comp": 0.0,
                "raw": 0.0,
                "veto": "",
                "status": "manual_metadata",
            })
            return

        # Check decode failed
        md = self._RE_DECODE_ERR.search(msg)
        if md:
            name = md.group(1)
            emit({
                "type": "frame",
                "name": name,
                "rating": -1,
                "sharp": 0.0,
                "comp": 0.0,
                "raw": 0.0,
                "veto": "decode_failed",
                "status": "decode_failed",
            })
            return

        # Fallback general log line
        emit({
            "type": "log",
            "line": msg,
        })
