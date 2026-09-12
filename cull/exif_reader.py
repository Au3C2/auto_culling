"""
exif_reader.py — Read EXIF metadata and group burst sequences.

Supports:
  - Sony A7C2 (HIF):  SequenceImageNumber resets to 1 → new burst group.
  - Nikon Z6 III (NEF/JPG): BurstGroupID changes → new burst group.
  - Generic fallback: time gap > 2 s between adjacent frames → new group.

Requires ``exiftool`` to be installed on the system PATH.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

def get_resource_path(relative_path: str) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        # Assuming this file is in cull/exif_reader.py, 
        # project root is parent.parent
        base_path = Path(__file__).parent.parent.resolve()
    return base_path / relative_path

def _find_exiftool_path() -> list[str]:
    """Return command list for exiftool (bundled or system-wide).

    The win32-only perl core/XS modules live in ``lib-win32/``, split from
    ``lib/`` (the pure-Perl exiftool dist: Image::ExifTool & co). Branch 2
    runs the launcher through the SYSTEM perl with ``-I lib`` only — the
    win32 tree must never land on that @INC or its ``.xs.dll`` files shadow
    system core modules and dlopen-fail on macOS (silently killed EXIF
    there, 2026-08-31). Keep in sync with cull/loader.py.
    """
    # 1. Check for bundled Perl script + Bundled Perl Interpreter (Self-contained)
    ext = ".exe" if sys.platform == "win32" else ""
    bundled_perl = get_resource_path(f"external/exiftool/perl{ext}")
    bundled_pl = get_resource_path("external/exiftool/exiftool.pl")
    lib_path = get_resource_path("external/exiftool/lib")
    win32_lib = get_resource_path("external/exiftool/lib-win32")

    if bundled_perl.exists() and bundled_pl.exists() and lib_path.exists():
        cmd = [str(bundled_perl), "-I", str(lib_path)]
        if win32_lib.exists():
            cmd += ["-I", str(win32_lib)]
        return [*cmd, str(bundled_pl)]

    # 2. Check for bundled binary/launcher
    bundled_bin = get_resource_path(f"external/exiftool/exiftool{ext}")
    if bundled_bin.exists():
        # If it's the perl launcher (with a lib folder), but we didn't find perl.exe 
        # above, we still need to hope 'perl' is in the path OR that it's a standalone exe.
        if lib_path.exists():
            return ["perl", "-I", str(lib_path), str(bundled_bin)]
        return [str(bundled_bin)]

    # 3. Fallback to system-wide
    return ["exiftool"]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExifData:
    """Parsed EXIF fields for a single image file."""

    path: Path
    datetime_original: datetime | None = None  # SubSecDateTimeOriginal parsed
    sequence_image_number: int | None = None   # Sony A7C2
    burst_group_id: int | None = None          # Nikon Z6III
    release_mode: str | None = None            # e.g. "Continuous", "Single"
    image_width: int | None = None
    image_height: int | None = None
    rating: int | None = None          # XMP:Rating
    pick: int | None = None            # XMP-xmpDM:Pick
    # Raw dict for any extra fields callers may need
    raw: dict = field(default_factory=dict)


@dataclass
class BurstGroup:
    """A set of frames belonging to the same burst / single shot."""

    group_id: str           # human-readable identifier, e.g. "burst_001"
    frames: list[Path]      # ordered by capture time (or filename)
    is_burst: bool = True   # False for single shots


# ---------------------------------------------------------------------------
# exiftool subprocess helper
# ---------------------------------------------------------------------------

_EXIFTOOL_FIELDS = [
    "SubSecDateTimeOriginal",
    "DateTimeOriginal",
    "SequenceImageNumber",
    "BurstGroupID",
    "ReleaseMode2",          # Sony
    "ShootingMode",          # Nikon
    "ImageWidth",
    "ImageHeight",
    "ExifImageWidth",
    "ExifImageHeight",
    "Rating",
    "XMP-xmpDM:Pick",
]


def _run_exiftool(paths: list[Path]) -> list[dict]:
    """Run exiftool -json on *paths* and return the parsed list of dicts.

    Shards into parallel subprocesses with argv file lists (measured 8.1
    ms/file vs 18.5 ms/file for the -@ - stdin protocol on Apple M4,
    2026-08-24); very large batches fall back to the -@ - stdin protocol to
    stay under the Windows command-line length limit (~32 KB). ExifData
    lookup is by path, so shard ordering does not affect results.
    """
    if not paths:
        return []

    cmd_base = _find_exiftool_path()
    args = [
        *cmd_base,
        "-json",
        "-n",                   # numeric values (no units/text decoration)
        *[f"-{f}" for f in _EXIFTOOL_FIELDS],
    ]

    try:
        if len(paths) <= 400:
            import os
            from concurrent.futures import ThreadPoolExecutor
            nproc = max(1, min(4, (os.cpu_count() or 1) // 2))
            chunks = [paths[i::nproc] for i in range(nproc)]
            extra_kwargs: dict = {"stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            def _run_shard(shard: list[Path]) -> list[dict]:
                result = subprocess.run(
                    [*args, *[str(p) for p in shard]],
                    capture_output=True, text=True, check=True,
                    **extra_kwargs,
                )
                return json.loads(result.stdout)

            with ThreadPoolExecutor(max_workers=nproc) as pool:
                shard_results = list(pool.map(_run_shard, chunks))
            return [entry for shard in shard_results for entry in shard]
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # fall through to the -@ - path, which raises properly

    # Fallback: very large batches via -@ - (read filenames from stdin).
    cmd = [*args, "-@", "-"]

    # Build newline-separated file list for stdin
    file_list = "\n".join(str(p) for p in paths) + "\n"
    fb_kwargs: dict = {}
    if sys.platform == "win32":
        fb_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        result = subprocess.run(
            cmd,
            input=file_list,
            capture_output=True,
            text=True,
            check=True,
            **fb_kwargs,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "exiftool not found. Install it with:\n"
            "  Ubuntu/Debian:  sudo apt install libimage-exiftool-perl\n"
            "  macOS:          brew install exiftool\n"
            "  Windows:        https://exiftool.org/"
        )
    except subprocess.CalledProcessError as exc:
        # If exiftool returns code 1 or 2 (e.g. minor warnings or unreadable single corrupt files),
        # check if stdout still contains valid JSON for the valid files.
        if exc.stdout and exc.stdout.strip().startswith("["):
            try:
                return json.loads(exc.stdout)
            except Exception:
                pass

        # exiftool failed fatally (e.g. broken perl environment/dlopen failure)
        stderr_tail = (exc.stderr or "").strip()[-400:]
        raise RuntimeError(
            f"exiftool exited with code {exc.returncode} (broken install?): {stderr_tail}"
        ) from exc

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("Failed to parse exiftool JSON output: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------

_DT_FORMATS = [
    "%Y:%m:%d %H:%M:%S.%f",   # with sub-seconds, no timezone
    "%Y:%m:%d %H:%M:%S",      # without sub-seconds, no timezone
]

# Regex to strip a trailing timezone offset like "+08:00" or "-05:30"
import re as _re
_TZ_SUFFIX = _re.compile(r"[+-]\d{2}:\d{2}$")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # Strip timezone suffix if present (exiftool may include it, e.g. Sony HIF)
    cleaned = _TZ_SUFFIX.sub("", value).strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    log.debug("Could not parse datetime: %r", value)
    return None


# ---------------------------------------------------------------------------
# Public API: read_exif
# ---------------------------------------------------------------------------


def read_exif(paths: list[Path]) -> list[ExifData]:
    """Read EXIF metadata for a list of image paths via exiftool.
    Processes in batches of 500 to provide progress feedback.
    """
    batch_size = 500
    all_raw: list[dict] = []
    
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        log.info("  Reading metadata: %d/%d images...", i + len(batch), len(paths))
        raw_list = _run_exiftool(batch)
        all_raw.extend(raw_list)

    raw_list = all_raw

    # Build lookups: prioritize exact string match of the absolute path
    raw_by_path: dict[str, dict] = {}
    raw_by_name: dict[str, dict] = {}
    for entry in raw_list:
        src = entry.get("SourceFile", "")
        if src:
            # Standardize path separators for consistent matching on Windows
            norm_src = str(Path(src)).lower()
            raw_by_path[norm_src] = entry
            raw_by_name[Path(src).name.lower()] = entry

    results: list[ExifData] = []
    for path in paths:
        # Standardize search path
        norm_path = str(path).lower()
        
        # Fast lookup in memory without disk IO
        raw = raw_by_path.get(norm_path) or raw_by_name.get(path.name.lower()) or {}

        dt_str = raw.get("SubSecDateTimeOriginal") or raw.get("DateTimeOriginal")
        seq_num = raw.get("SequenceImageNumber")
        burst_id_raw = raw.get("BurstGroupID")

        # BurstGroupID may come as a hex string or integer
        burst_id: int | None = None
        if burst_id_raw is not None:
            try:
                burst_id = int(burst_id_raw)
            except (ValueError, TypeError):
                try:
                    burst_id = int(str(burst_id_raw), 16)
                except (ValueError, TypeError):
                    pass

        # Width / height: prefer Exif tags, fall back to file tags
        width = raw.get("ExifImageWidth") or raw.get("ImageWidth")
        height = raw.get("ExifImageHeight") or raw.get("ImageHeight")

        release = raw.get("ReleaseMode2") or raw.get("ShootingMode")

        results.append(ExifData(
            path=path,
            datetime_original=_parse_datetime(dt_str),
            sequence_image_number=seq_num,
            burst_group_id=burst_id,
            release_mode=release,
            image_width=int(width) if width is not None else None,
            image_height=int(height) if height is not None else None,
            rating=int(raw.get("Rating")) if raw.get("Rating") is not None else None,
            pick=int(raw.get("Pick")) if raw.get("Pick") is not None else None,
            raw=raw,
        ))

    return results


# ---------------------------------------------------------------------------
# Public API: group_bursts
# ---------------------------------------------------------------------------

_GAP_SECONDS = 2.0   # frames separated by more than this are in different groups


def group_bursts(exif_list: list[ExifData]) -> list[BurstGroup]:
    """Group a list of ExifData entries into burst sequences.

    Detection strategy (in priority order):
    1. **Sony A7C2** — ``SequenceImageNumber`` resets to 1 → new burst starts.
    2. **Nikon Z6III** — ``BurstGroupID`` changes → new burst starts.
    3. **Generic** — time gap between adjacent frames > ``_GAP_SECONDS``.

    The input list should be sorted by filename (or capture time) before
    calling this function.

    Parameters
    ----------
    exif_list:
        Ordered list of ExifData for all images to be grouped.

    Returns
    -------
    list[BurstGroup]
        Each group contains the file paths of its member frames in order.
    """
    if not exif_list:
        return []

    groups: list[BurstGroup] = []
    current_frames: list[Path] = []
    group_counter = 0

    def _flush(frames: list[Path]) -> None:
        nonlocal group_counter
        if not frames:
            return
        group_counter += 1
        is_burst = len(frames) > 1
        groups.append(BurstGroup(
            group_id=f"burst_{group_counter:04d}",
            frames=list(frames),
            is_burst=is_burst,
        ))

    prev = exif_list[0]
    current_frames = [prev.path]

    for curr in exif_list[1:]:
        new_group = False

        # --- Strategy 1: Sony A7C2 SequenceImageNumber ---
        # SequenceImageNumber resets to 1 at the start of every new burst (or
        # single shot).  Treat *any* reset to 1 as a new group boundary —
        # including the case where the previous frame was also a single shot
        # with seq == 1.  Exception: the very first frame in the list always
        # opens the first group rather than closing a non-existent previous one.
        if (
            prev.sequence_image_number is not None
            and curr.sequence_image_number is not None
        ):
            if curr.sequence_image_number == 1:
                new_group = True

        # --- Strategy 2: Nikon BurstGroupID ---
        elif (
            prev.burst_group_id is not None
            and curr.burst_group_id is not None
        ):
            if curr.burst_group_id != prev.burst_group_id:
                new_group = True

        # --- Strategy 3: Time gap fallback ---
        else:
            if (
                prev.datetime_original is not None
                and curr.datetime_original is not None
            ):
                gap = (curr.datetime_original - prev.datetime_original).total_seconds()
                if gap > _GAP_SECONDS:
                    new_group = True
            else:
                # No timing info at all → treat each file as its own group
                new_group = True

        if new_group:
            _flush(current_frames)
            current_frames = [curr.path]
        else:
            current_frames.append(curr.path)

        prev = curr

    _flush(current_frames)
    return groups
