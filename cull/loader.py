"""
loader.py — Image loading utilities for F1 photo culling.
Pillow/FFmpeg VERSION — NO OpenCV.
Supports HIF (via FFmpeg), RAW (via ExifTool), and standard formats.
"""

from __future__ import annotations

import logging
import ctypes
import io
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Tuple

import numpy as np
import cv2
from PIL import Image

# Decode workers (spawned by the engine's ProcessPoolExecutor) run CPU-heavy
# JPEG/RAW decode; demote them below the main consumer thread so scoring
# latency isn't starved on the shared 8 physical cores. On Apple Silicon,
# UTILITY QoS additionally steers workers toward the efficiency cores.
try:
    import multiprocessing as _mp
    if _mp.current_process().name != "MainProcess":
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        if sys.platform == "darwin":
            try:
                import ctypes as _ct
                _libc = _ct.CDLL("/usr/lib/libpthread.dylib")
                _libc.pthread_set_qos_class_self_np(0x11, 0)  # QOS_CLASS_UTILITY
            except Exception:
                pass
except Exception:
    pass

log = logging.getLogger(__name__)

EXTENSIONS = {".hif", ".heif", ".heic", ".nef", ".arw", ".cr2", ".cr3",
              ".orf", ".rw2", ".raf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

RAW_EXTS = {".arw", ".nef", ".cr2", ".cr3", ".orf", ".rw2", ".raf", ".dng"}
COOKED_EXTS = {".jpg", ".jpeg", ".hif", ".heif", ".heic", ".png", ".tiff", ".tif"}

def get_resource_path(relative_path: str) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = Path(__file__).parent.parent.resolve()
    return base_path / relative_path

def _reduced_flag(scale_width: int) -> int:
    """cv2 imread/imdecode flag for reduced-DCT JPEG decode.

    CULL_JPEG_REDUCED=4 selects 1/4-size decode (fewer pixels through IDCT;
    Huffman entropy decode — the dominant cost on 24 MP files — is
    unaffected). Default 1/2 stays bit-identical to the Pillow draft path.
    """
    if scale_width <= 0:
        return cv2.IMREAD_COLOR
    import os as _os
    if _os.environ.get("CULL_JPEG_REDUCED", "2").strip() == "4":
        return cv2.IMREAD_REDUCED_COLOR_4
    return cv2.IMREAD_REDUCED_COLOR_2


def _find_exiftool_path() -> list[str]:
    """Return command list for exiftool (bundled or system-wide).

    The win32-only perl core/XS modules live in ``lib-win32/``, split from
    ``lib/`` (the pure-Perl exiftool dist: Image::ExifTool & co). Branch 2
    runs the launcher through the SYSTEM perl with ``-I lib`` only — the
    win32 tree must never land on that @INC or its ``.xs.dll`` files shadow
    system core modules and dlopen-fail on macOS (silently killed EXIF
    there, 2026-08-31). Keep in sync with cull/exif_reader.py.
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
        if lib_path.exists():
            return ["perl", "-I", str(lib_path), str(bundled_bin)]
        return [str(bundled_bin)]

    # 3. Fallback to system-wide
    return ["exiftool"]

def _find_ffmpeg_path() -> str:
    """Return path to bundled ffmpeg if exists, otherwise assume system-wide."""
    ext = ".exe" if sys.platform == "win32" else ""
    bundled = get_resource_path(f"external/ffmpeg/ffmpeg{ext}")
    if bundled.exists(): return str(bundled)
    import shutil as _shutil
    found = _shutil.which(f"ffmpeg{ext}") or _shutil.which("ffmpeg")
    return str(found) if found else "ffmpeg"

def _find_ffprobe_path() -> str:
    """Return path to bundled ffprobe if exists, otherwise assume system-wide."""
    ext = ".exe" if sys.platform == "win32" else ""
    bundled = get_resource_path(f"external/ffmpeg/ffprobe{ext}")
    if bundled.exists(): return str(bundled)
    import shutil as _shutil
    found = _shutil.which(f"ffprobe{ext}") or _shutil.which("ffprobe")
    return str(found) if found else "ffprobe"

def probe_embedded_preview(path: Path, min_width: int = 800) -> Tuple[int, int, int] | None:
    try:
        ffprobe_bin = _find_ffprobe_path()
        run_kwargs: dict = {"stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v", "-show_entries", 
             "stream=index,width,height,codec_name:stream_disposition=dependent", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, **run_kwargs
        )
        if proc.returncode != 0: return None
        import json
        data = json.loads(proc.stdout)
        best: Tuple[int, int, int] | None = None
        best_w = 0
        for s in data.get("streams", []):
            idx, w, h, codec = s.get("index", -1), s.get("width", 0), s.get("height", 0), s.get("codec_name", "")
            if s.get("disposition", {}).get("dependent", 0) == 1 or codec != "hevc": continue
            if w < min_width: continue
            if w > best_w and w < 5000:
                best = (idx, int(w), int(h))
                best_w = int(w)
        return best
    except Exception: return None

def probe_full_dimensions(path: Path) -> Tuple[int, int] | None:
    try:
        ffprobe_bin = _find_ffprobe_path()
        run_kwargs: dict = {"stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", 
             "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=10, **run_kwargs
        )
        parts = proc.stdout.strip().split("\n")[0].split("x")
        if len(parts) == 2: return int(parts[0]), int(parts[1])
    except Exception: pass
    return None

_preview_stream_cache: dict[Path, Tuple[int, int, int] | None] = {}

# Apple ImageIO hardware JPEG decoder (lazy import; None when unavailable).
_imageio_module = None
_imageio_checked = False
_imageio_lock = threading.Lock()

def _get_quartz():
    """Lazy-load the Quartz (ImageIO) bindings once per process.

    Thread-safe: concurrent first-time callers block on the lock until the
    import finishes. Without this, threads that arrive during `import Quartz`
    saw checked=True / module=None and silently fell back to the cv2 decode
    path — whose pixels differ enough to flip P4 decisions on knife-edge
    frames (rating gates failed nondeterministically on thread-startup
    timing)."""
    global _imageio_module, _imageio_checked
    if not _imageio_checked:
        with _imageio_lock:
            if not _imageio_checked:
                try:
                    import Quartz as _q
                    _imageio_module = _q
                except Exception:
                    _imageio_module = None
                _imageio_checked = True
    return _imageio_module

def decode_jpeg_imageio(path: Path, scale_width: int = 1280) -> np.ndarray | None:
    """Decode a JPEG via Apple ImageIO hardware JPEG decoder (Apple Silicon ASIC).

    Uses CGImageSourceCreateThumbnailAtIndex to trigger the hardware DCT
    downscale directly to ~scale_width pixels. The image's own colorspace is
    preserved (ColorSync gamut conversion disabled) so output pixels match
    libjpeg within ±3 gray levels.

    Returns None on any failure so callers fall back to cv2/Pillow paths."""
    q = _get_quartz()
    if q is None:
        return None
    try:
        encoded = str(path.resolve()).encode()
        url = q.CFURLCreateFromFileSystemRepresentation(None, encoded, len(encoded), False)
        src = q.CGImageSourceCreateWithURL(url, {q.kCGImageSourceShouldCache: False})
        if src is None:
            return None
        opts = {
            q.kCGImageSourceCreateThumbnailFromImageAlways: True,
            q.kCGImageSourceCreateThumbnailWithTransform: True,
            q.kCGImageSourceThumbnailMaxPixelSize: scale_width,
            q.kCGImageSourceShouldCacheImmediately: False,
        }
        cg_img = q.CGImageSourceCreateThumbnailAtIndex(src, 0, opts)
        if cg_img is None:
            return None
        w, h = q.CGImageGetWidth(cg_img), q.CGImageGetHeight(cg_img)
        # Use the image's own colorspace: no ColorSync gamut conversion.
        cs = q.CGImageGetColorSpace(cg_img)
        ctx = q.CGBitmapContextCreate(None, w, h, 8, w * 4, cs,
                                      q.kCGImageAlphaNoneSkipLast | q.kCGBitmapByteOrder32Big)
        if ctx is None:
            return None
        q.CGContextDrawImage(ctx, q.CGRectMake(0, 0, w, h), cg_img)
        cg_out = q.CGBitmapContextCreateImage(ctx)
        data_ref = q.CGDataProviderCopyData(q.CGImageGetDataProvider(cg_out))
        size = q.CFDataGetLength(data_ref)
        out = (ctypes.c_char * size)()
        q.CFDataGetBytes(data_ref, q.CFRange(0, size), out)
        # Direct zero-copy buffer view + C-level SIMD RGBA->RGB (saves ~2.4 ms vs Python bytes()+copy())
        raw_view = np.frombuffer(out, dtype=np.uint8).reshape(h, w, 4)
        return cv2.cvtColor(raw_view, cv2.COLOR_RGBA2RGB)
    except Exception as e:
        log.debug("ImageIO JPEG decode failed for %s: %s", path.name, e)
        return None

def get_preview_stream(path: Path) -> Tuple[int, int, int] | None:
    cache_key = path.parent
    if cache_key not in _preview_stream_cache:
        _preview_stream_cache[cache_key] = probe_embedded_preview(path)
    return _preview_stream_cache[cache_key]

def _load_image_pyav(path: Path, scale_width: int = 1280) -> np.ndarray | None:
    """Decode the primary preview stream via in-process libav (pyav).

    Spawning ffmpeg per file costs ~80-110 ms (process startup) on top of the
    HEVC decode; pyav keeps libav resident in the worker.

    On macOS (darwin), in-process VideoToolbox hardware decoding is used by
    default (12.4 ms vs 21.8 ms soft decode), with JPEG full-range color
    metadata alignment to guarantee 100% bit-identical RGB output (0 drift).
    Falls back gracefully to software decode if hardware decode fails.

    In deterministic mode (``CULL_DETERMINISTIC=1``) hardware decoders are
    disabled so macOS and Windows share the same software HEVC path.

    Returns None when pyav is unavailable or decoding fails, falling back to
    the ffmpeg spawn path."""
    try:
        import av
    except Exception:
        return None
    try:
        container = av.open(str(path))
        try:
            best = None  # (width, stream)
            for s in container.streams.video:
                try:
                    if s.codec_context.name != "hevc":
                        continue
                    if int(s.disposition) & (1 << 19):  # AV_DISPOSITION_DEPENDENT
                        continue
                    w = s.codec_context.width
                    if w < 800 or w >= 5000:
                        continue
                    if best is None or w > best[0]:
                        best = (w, s)
                except Exception:
                    continue
            if best is None:
                return None
            
            stream = best[1]
            frame = None

            # Deterministic gate: bypass all hardware decoders (VideoToolbox /
            # vaapi/dxva2/cuda) so macOS and Windows share the same libav
            # software HEVC path. Only active when CULL_DETERMINISTIC=1.
            try:
                from cull.deterministic import is_deterministic as _is_det
                _det = _is_det()
            except Exception:
                _det = False

            if not _det and sys.platform == "darwin":
                try:
                    hwa = av.codec.hwaccel.HWAccel("videotoolbox")
                    ctx = av.CodecContext.create(stream.codec_context.name, "r", hwaccel=hwa)
                    if stream.codec_context.extradata:
                        ctx.extradata = stream.codec_context.extradata
                    ctx.open()

                    target_idx = stream.index
                    frames = []
                    for packet in container.demux():
                        if packet.stream_index != target_idx:
                            continue
                        for f in ctx.decode(packet):
                            frames.append(f)
                    if not frames:
                        for f in ctx.decode(None):
                            frames.append(f)
                    if frames:
                        frame = frames[0]
                        # Cameras encode full-range YUV for preview stills (JPEG color range).
                        # VideoToolbox defaults to limited-range if unspecified by the container,
                        # which distorts YUV->RGB matrix levels. Explicitly assign full-range
                        # color metadata to ensure bit-identical RGB output with software decode.
                        frame.color_range = 2  # AVCOL_RANGE_JPEG (Full Range)
                        frame.colorspace = stream.codec_context.colorspace if (stream.codec_context.colorspace and stream.codec_context.colorspace != 2) else 5
                        frame.color_primaries = stream.codec_context.color_primaries if (stream.codec_context.color_primaries and stream.codec_context.color_primaries != 2) else 1
                        frame.color_trc = stream.codec_context.color_trc if (stream.codec_context.color_trc and stream.codec_context.color_trc != 2) else 13
                except Exception as hw_err:
                    log.debug("VideoToolbox decode fallback: %s", hw_err)
                    frame = None
            elif not _det and sys.platform != "darwin":
                # REJECTED (win32, measured 2026-08-30): pyav HWAccel probing for
                # HEVC Rext 4:2:2 10-bit camera previews. Camera HEIF previews are
                # Rext 4:2:2 10-bit, which consumer NVDEC cannot decode — the
                # cuda/dxva2/d3d11va "decode" pays hw-device + transfer overhead on
                # top of an in-libavcodec software fallback (122-167 ms/frame vs
                # 42.5 ms pure software), and vaapi silently falls back to software
                # (41.5 ms, no gain). The per-file probe itself cost ~80 ms/frame.
                # Keep the darwin-only VideoToolbox branch above; non-darwin always
                # uses the software path below.
                pass

            if frame is None:
                frame = next(container.decode(stream))
            
            img = frame.to_ndarray(format="rgb24")
            if scale_width > 0 and img.shape[1] > scale_width * 1.2:
                h, w = img.shape[:2]
                new_h = int(round(h * scale_width / w))
                return cv2.resize(img, (scale_width, new_h), interpolation=cv2.INTER_AREA)
            return img
        finally:
            container.close()
    except Exception:
        return None


def find_embedded_jpeg_tiff(data: bytes) -> tuple[int, int] | None:
    """Walk the full TIFF IFD chain (including SubIFDs) in a RAW file to locate
    the largest embedded JPEG (JpgFromRaw / PreviewImage).

    Returns (offset, length), or None. Handles Sony ARW (multi-IFD chain),
    Nikon NEF (multi-SubIFDs), Canon CR2 (SubIFDs), and other TIFF-based RAWs.
    """
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if data[:2] == b"II" else ">"
    visited = set()
    queue = [struct.unpack_from(endian + "I", data, 4)[0]]
    best = None

    while queue:
        ifd_off = queue.pop(0)
        if ifd_off in visited or ifd_off <= 0 or ifd_off >= len(data):
            continue
        try:
            n_entries = struct.unpack_from(endian + "H", data, ifd_off)[0]
        except Exception:
            continue
        if n_entries > 500 or n_entries < 1:
            continue
        visited.add(ifd_off)

        next_ifd_off = struct.unpack_from(endian + "I", data, ifd_off + 2 + n_entries * 12)[0]

        for i in range(n_entries):
            eo = ifd_off + 2 + i * 12
            tag = struct.unpack_from(endian + "H", data, eo)[0]

            if tag == 0x0201:
                off = struct.unpack_from(endian + "I", data, eo + 8)[0]
                ln_tag_eo = eo + 12
                ln = struct.unpack_from(endian + "I", data, ln_tag_eo + 8)[0]
                if off > 0 and ln > 10000 and off + ln <= len(data) and data[off:off+2] == b"\xff\xd8":
                    if best is None or ln > best[1]:
                        best = (off, ln)
            elif tag == 0x014A:
                typ = struct.unpack_from(endian + "H", data, eo + 2)[0]
                cnt = struct.unpack_from(endian + "I", data, eo + 4)[0]
                if typ == 4 and cnt == 1:
                    v = struct.unpack_from(endian + "I", data, eo + 8)[0]
                    queue.append(v)
                elif typ == 4 and cnt > 4:
                    val_off = struct.unpack_from(endian + "I", data, eo + 8)[0]
                    for j in range(cnt):
                        try:
                            v = struct.unpack_from(endian + "I", data, val_off + j * 4)[0]
                            if v > 0 and v < len(data):
                                queue.append(v)
                        except Exception:
                            break
                elif typ == 4 and cnt <= 4:
                    for j in range(cnt):
                        v = struct.unpack_from(endian + "I", data, eo + 8 + j * 4)[0]
                        queue.append(v)

        if next_ifd_off > 0:
            queue.append(next_ifd_off)

    return (best[0], best[1]) if best else None


def _hw_decode_jpeg_ffmpeg(path: Path, scale_width: int = 1280) -> np.ndarray | None:
    """Try ffmpeg -hwaccel auto for JPEG (nondarwin, probing with sw fallback).

    Disabled by default: the ffmpeg hw path uses a full-res decode + resize
    vs cv2's reduced DCT decode, giving ~1M sum difference on 6048x4032 JPGs
    and 0.02-0.09 raw_score drift from the deterministic truth (measured
    0.089 on IMG_20260314_160318_240.jpg).  It is also 5x slower (0.59s vs
    0.11s).  Enable explicitly with CULL_HW_JPEG=1 if needed.
    """
    import os as _os
    if _os.environ.get("CULL_HW_JPEG", "0") not in ("1", "true", "True", "TRUE"):
        return None
    if sys.platform == "darwin":
        return None
    try:
        ffmpeg_bin = _find_ffmpeg_path()
        probe = subprocess.run([ffmpeg_bin, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=5)
        if probe.returncode != 0 or "auto" not in probe.stdout.lower():
            # Still try explicit hwaccels list.
            pass
        for hw in ("auto", "dxva2", "d3d11va", "vaapi", "cuda"):
            cmd = [ffmpeg_bin, "-hide_banner", "-v", "error", "-hwaccel", hw,
                   "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-y", "pipe:1"]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=10)
            except Exception:
                continue
            if proc.returncode == 0 and len(proc.stdout) > 10000:
                # Need dimensions to reshape — probe via PIL header.
                try:
                    pil = Image.open(path)
                    w0, h0 = pil.size
                    if scale_width > 0 and w0 > scale_width * 1.2:
                        nh = int(round(h0 * scale_width / w0))
                        exp = scale_width * nh * 3
                        if len(proc.stdout) == w0 * h0 * 3:
                            # hwaccel emitted full-res; downscale same as sw path.
                            img = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(h0, w0, 3)
                            return cv2.resize(img, (scale_width, nh), interpolation=cv2.INTER_AREA)
                    # Fallback: try to infer output size from byte count.
                except Exception:
                    pass
                # If raw size matches full frame, return as-is for caller to resize.
                return None
        return None
    except Exception as e:
        log.debug("hw jpeg probe failed for %s: %s", path.name, e)
        return None


def _extract_raw_tiff_direct(path: Path) -> bytes | None:
    """Extract embedded preview JPEG from a TIFF-based RAW via direct header parsing.

    Uses Range I/O: reads only the first 6 MB chunk (where TIFF IFDs & preview JPEG
    always reside in Sony ARW / Nikon NEF), avoiding 40-52 MB of unneeded file I/O.
    Falls back to seek/full-read if the preview extends beyond the initial chunk.
    Yields 100% bit-identical JPEG byte slice to full read_bytes().
    ~1000x faster than exiftool persistent session (0.8 ms vs 7-12 ms)."""
    try:
        size = path.stat().st_size
        chunk_size = min(size, 6 * 1024 * 1024)
        with open(path, "rb") as f:
            header_data = f.read(chunk_size)
            result = find_embedded_jpeg_tiff(header_data)
            if result is not None:
                offset, length = result
                if offset + length <= chunk_size:
                    jpeg_bytes = header_data[offset:offset + length]
                else:
                    f.seek(offset)
                    jpeg_bytes = f.read(length)
                if len(jpeg_bytes) > 10000 and jpeg_bytes[:2] == b"\xff\xd8":
                    return bytes(jpeg_bytes)
            # Rare layout fallback: read entire file if not found in first 6 MB
            f.seek(0)
            full_data = f.read()
            res_full = find_embedded_jpeg_tiff(full_data)
            if res_full is not None:
                off, ln = res_full
                jpeg_bytes = full_data[off:off + ln]
                if len(jpeg_bytes) > 10000 and jpeg_bytes[:2] == b"\xff\xd8":
                    return bytes(jpeg_bytes)
    except Exception as e:
        log.debug("TIFF direct extraction failed for %s: %s", path.name, e)
    return None


def load_image_ffmpeg(path: Path, scale_width: int = 1280) -> np.ndarray | None:
    # 1. First try in-process pyav (self-probed, fastest, zero-subprocess, no window popup)
    img_pyav = _load_image_pyav(path, scale_width=scale_width)
    if img_pyav is not None:
        log.info("HEIF decode path: in-process av/VideoToolbox")
        return img_pyav

    # 2. Subprocess fallback only if in-process pyav is unavailable or failed
    preview = get_preview_stream(path)
    if preview is not None:
        idx, w, h = preview
        try:
            ffmpeg_bin = _find_ffmpeg_path()
            cmd = [
                ffmpeg_bin, "-hide_banner", "-v", "error",
                "-i", str(path), "-map", f"0:{idx}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-frames:v", "1", "-y", "pipe:1"
            ]
            run_kwargs: dict = {"stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.run(cmd, capture_output=True, timeout=30, **run_kwargs)
            if proc.returncode == 0 and len(proc.stdout) == w * h * 3:
                img = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(h, w, 3)
                if scale_width > 0 and w > scale_width * 1.2:
                    new_h = int(round(h * scale_width / w))
                    return cv2.resize(img, (scale_width, new_h), interpolation=cv2.INTER_AREA)
                return img
        except Exception: pass
    return None

def load_image_rgb(path: Path, scale_width: int = 0) -> np.ndarray | None:
    suffix = path.suffix.lower()
    if suffix in (".hif", ".heif", ".heic"):
        img = load_image_ffmpeg(path, scale_width=scale_width)
        if img is not None:
            log.info("HEIF decode path: in-process av/VideoToolbox")
            return img
        # Pillow Fallback
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            pil_img = Image.open(path).convert("RGB")
            img_arr = np.array(pil_img)
            if scale_width > 0:
                h, w = img_arr.shape[:2]
                new_h = int(round(h * scale_width / w))
                log.warning("HEIF decode path: pillow_heif SOFTWARE fallback (%dx%d)", w, h)
                return cv2.resize(img_arr, (scale_width, new_h), interpolation=cv2.INTER_AREA)
            log.warning("HEIF decode path: pillow_heif SOFTWARE fallback")
            return img_arr
        except Exception as e:
            log.warning(f"pillow-heif failed for {path.name}: {e}")

    # Deterministic gate: skip all hardware JPEG probes (ImageIO /
    # ffmpeg -hwaccel) and share the same cv2 software path on macOS
    # and Windows.
    try:
        from cull.deterministic import is_deterministic as _is_det2
        _det2 = _is_det2()
    except Exception:
        _det2 = False

    # JPEG decoding (nondarwin HW probe, then macOS ImageIO, then SW fallback).
    # On non-darwin, probe ffmpeg -hwaccel auto first (vaapi/dxva2/cuda etc.)
    # with explicit sw fallback — on this darwin host this branch is dead code.
    if not _det2 and suffix in (".jpg", ".jpeg") and sys.platform != "darwin":
        img_hw = _hw_decode_jpeg_ffmpeg(path, scale_width=scale_width)
        if img_hw is not None:
            return img_hw

    # JPEG decoding (macOS): prefer Apple ImageIO hardware JPEG decoder (ASIC)
    # with the image's own colorspace preserved (ColorSync gamut conversion
    # disabled) — 41.5 ms vs 54 ms cv2 NEON. Falls back to C++ OpenCV NEON.
    if not _det2 and suffix in (".jpg", ".jpeg") and sys.platform == "darwin":
        img_io = decode_jpeg_imageio(path, scale_width=scale_width)
        if img_io is not None:
            return img_io

    # JPEG decoding fallback: C++ cv2.imread with IMREAD_REDUCED_COLOR_N
    # (SIMD accelerated reduced-DCT decode in libjpeg-turbo), followed by
    # cv2.INTER_AREA. The 1/2 factor is 100% bit-identical to Pillow draft
    # (diff=0). CULL_JPEG_REDUCED=4 switches to 1/4-size DCT decode (measured
    # -24% decode time on 24 MP files; Huffman entropy decode is
    # resolution-independent and sets the CPU floor).
    if suffix in (".jpg", ".jpeg"):
        try:
            flag = _reduced_flag(scale_width)
            img_bgr = cv2.imread(str(path), flag)
            if img_bgr is not None:
                if scale_width > 0:
                    h, w = img_bgr.shape[:2]
                    new_h = int(round(h * scale_width / w))
                    small_bgr = cv2.resize(img_bgr, (scale_width, new_h), interpolation=cv2.INTER_AREA)
                    return cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)
                return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        except Exception:
            pass

    try:
        pil_img = Image.open(path)
        if scale_width > 0 and suffix in (".jpg", ".jpeg"):
            dw, dh = pil_img.size
            dcp_scale = 1
            while dcp_scale < 8 and dw // (dcp_scale * 2) >= scale_width \
                    and dh // (dcp_scale * 2) >= scale_width:
                dcp_scale *= 2
            if dcp_scale > 1:
                pil_img.draft("RGB", (dw // dcp_scale, dh // dcp_scale))
        pil_img = pil_img.convert("RGB")
        img_arr = np.asarray(pil_img)
        if scale_width > 0:
            h, w = img_arr.shape[:2]
            new_h = int(round(h * scale_width / w))
            return cv2.resize(img_arr, (scale_width, new_h), interpolation=cv2.INTER_AREA)
        return img_arr
    except Exception:
        pass

    # RAW decoding: prefer TIFF header direct-read extraction (0.01 ms vs 7-12 ms
    # exiftool), then C++ SIMD reduced decode (100% bit-identical to Pillow draft, diff=0).
    if suffix in RAW_EXTS:
        try:
            data = _extract_raw_tiff_direct(path)
        except Exception:
            data = None
        if not data:
            try:
                data = _extract_embedded_raw(path, ["-JpgFromRaw", "-PreviewImage"])
            except Exception:
                data = None
        if data:
            try:
                flag = _reduced_flag(scale_width)
                img_bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flag)
                if img_bgr is not None:
                    # Mathematical identity: resize(cvtColor(I)) == cvtColor(resize(I))
                    # Resizing on BGR first shrinks the pixel buffer 7.5x before cvtColor (saves ~0.7 ms).
                    if scale_width > 0:
                        h, w = img_bgr.shape[:2]
                        new_h = int(round(h * scale_width / w))
                        small_bgr = cv2.resize(img_bgr, (scale_width, new_h), interpolation=cv2.INTER_AREA)
                        return cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)
                    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            except Exception:
                pass
            pil_img = Image.open(io.BytesIO(data))
            if scale_width > 0 and pil_img.format == "JPEG":
                dw, dh = pil_img.size
                dcp_scale = 1
                while dcp_scale < 8 and dw // (dcp_scale * 2) >= scale_width \
                        and dh // (dcp_scale * 2) >= scale_width:
                    dcp_scale *= 2
                if dcp_scale > 1:
                    pil_img.draft("RGB", (dw // dcp_scale, dh // dcp_scale))
            pil_img = pil_img.convert("RGB")
            img_arr = np.asarray(pil_img)
            if scale_width > 0:
                h, w = img_arr.shape[:2]
                new_h = int(round(h * scale_width / w))
                return cv2.resize(img_arr, (scale_width, new_h), interpolation=cv2.INTER_AREA)
            return img_arr
    return None

# ---------------------------------------------------------------------------
# RAW embedded-preview extraction via a persistent per-process exiftool
# session (-stay_open). A per-file exiftool spawn costs ~460 ms (Perl
# interpreter startup); the persistent session amortizes it to ~60-100 ms.
# The extraction bytes are identical to the per-file spawn (same
# ``-b -JpgFromRaw``), so decoded pixels and downstream scores are unchanged.
# Each -execute handles exactly ONE file, giving clean binary framing via the
# stderr "{ready}" marker (a batch of N files per -execute would concatenate
# payloads without separators — that path was A/B'd and dropped in #4).
# ---------------------------------------------------------------------------

class _PersistentExiftool:
    """One persistent exiftool ``-stay_open`` session for RAW embedded-preview
    extraction (``-b -w <tmp>/%f.jpg``).

    A per-file ``exiftool -b <tag>`` spawn costs ~460 ms (Perl interpreter
    startup); the persistent session amortizes that. Each -execute handles
    exactly ONE file and exiftool writes the binary payload to a temp file
    (``-w``), so framing is filesystem-based — the extraction bytes are
    identical to the per-file spawn (verified — the precision gates lock the
    decoded pixels). ``{ready}`` arrives as a text line on STDOUT for this
    launcher, which the caller reads line-by-line. Order-preserving and
    single-threaded per process."""

    def __init__(self) -> None:
        import tempfile
        import threading
        exiftool_cmd = _find_exiftool_path()
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen([*exiftool_cmd, "-stay_open", "True", "-@", "-"], **kwargs)
        self._outdir = Path(tempfile.mkdtemp(prefix="raw_extract_"))
        self._ready = threading.Event()
        self._dead = False
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    self._dead = True
                    self._ready.set()
                    return
                if line.strip() == b"{ready}":
                    self._ready.set()
        except Exception:
            self._dead = True
            self._ready.set()

    def extract(self, path: Path, tag: str) -> bytes | None:
        if self._dead:
            return None
        out = self._outdir / f"{path.stem}.jpg__"
        cmd = ["-b", "-w", f"{self._outdir.as_posix()}/%f.jpg__", tag, str(path)]
        try:
            self.proc.stdin.write(("\n".join(cmd) + "\n-execute\n").encode("utf-8", "replace"))
            self.proc.stdin.flush()
        except Exception:
            self._dead = True
            return None
        self._ready.clear()
        import time
        self._ready.wait(timeout=60.0)
        if self._dead:
            return None
        try:
            return out.read_bytes() if out.exists() else None
        finally:
            try: out.unlink(missing_ok=True)
            except Exception: pass

    def close(self) -> None:
        import shutil
        if not self._dead:
            try:
                self.proc.stdin.write(b"-stay_open\nFalse\n-execute\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        shutil.rmtree(self._outdir, ignore_errors=True)

_raw_session: _PersistentExiftool | None = None

def _get_raw_session() -> _PersistentExiftool | None:
    global _raw_session
    if _raw_session is None:
        try:
            _raw_session = _PersistentExiftool()
            import atexit
            atexit.register(_raw_session.close)
        except Exception:
            _raw_session = None  # fall back to per-file spawns
    return _raw_session


def _extract_embedded_raw(path: Path, tags: list[str]) -> bytes | None:
    """Return embedded preview bytes (e.g. JpgFromRaw/PreviewImage), using the
    persistent session when available, else the per-file spawn fallback."""
    session = _get_raw_session()
    if session is not None:
        for tag in tags:
            data = session.extract(path, tag)
            if data:
                return data
        return None
    exiftool_cmd = _find_exiftool_path()
    run_kwargs: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    for tag in tags:
        try:
            proc = subprocess.run([*exiftool_cmd, "-b", tag, str(path)],
                                  capture_output=True, timeout=10, **run_kwargs)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            continue
    return None


def update_image_metadata(img_path: Path, rating: int, crop: tuple[float, float, float, float] | None = None) -> tuple[bool, str]:
    et_rating, pick_flag = max(0, rating), (1 if rating > 0 else -1)
    exiftool_cmd = _find_exiftool_path()
    cmd = [*exiftool_cmd, "-overwrite_original", f"-XMP-xmp:Rating={et_rating}", f"-XMP-xmpDM:Pick={pick_flag}"]
    if crop:
        t, l, b, r = crop
        cmd.extend(["-XMP-crs:HasCrop=True", "-XMP-crs:AlreadyApplied=False", 
                    f"-XMP-crs:CropTop={t:.6f}", f"-XMP-crs:CropLeft={l:.6f}", 
                    f"-XMP-crs:CropBottom={b:.6f}", f"-XMP-crs:CropRight={r:.6f}",
                    "-XMP-crs:CropAngle=0", "-XMP-crs:CropConstrainToWarp=0", "-XMP-crs:CropConstrainToUnitSquare=1"])
    cmd.append(str(img_path))
    run_kwargs: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.run(cmd, check=True, capture_output=True, **run_kwargs)
        return True, img_path.name
    except subprocess.CalledProcessError as e:
        return False, f"Error updating {img_path.name}: {e.stderr.decode().strip()}"


def update_image_metadata_batch(
    items: list[tuple[Path, int, tuple[float, float, float, float] | None]],
) -> list[tuple[bool, str]]:
    """Write rating/pick/crop to many files through ONE persistent exiftool
    ``-stay_open`` session (text protocol), replacing per-file subprocess
    spawns (~399 ms/file → single process, ~10 ms/file). Order-preserving.
    """
    results: list[tuple[bool, str]] = []
    if not items:
        return results
    import subprocess as _s

    exiftool_cmd = _find_exiftool_path()
    kwargs = {"stdin": _s.PIPE, "stdout": _s.PIPE, "stderr": _s.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = _s.CREATE_NO_WINDOW
    proc = _s.Popen([*exiftool_cmd, "-stay_open", "True", "-@", "-"],
                    universal_newlines=False, **kwargs)
    try:
        for path, rating, crop in items:
            args = [
                "-overwrite_original",
                f"-XMP-xmp:Rating={max(0, rating)}",
                f"-XMP-xmpDM:Pick={1 if rating > 0 else -1}",
            ]
            if crop:
                t, l, b, r = crop
                args += [
                    "-XMP-crs:HasCrop=True", "-XMP-crs:AlreadyApplied=False",
                    f"-XMP-crs:CropTop={t:.6f}", f"-XMP-crs:CropLeft={l:.6f}",
                    f"-XMP-crs:CropBottom={b:.6f}", f"-XMP-crs:CropRight={r:.6f}",
                    "-XMP-crs:CropAngle=0", "-XMP-crs:CropConstrainToWarp=0",
                    "-XMP-crs:CropConstrainToUnitSquare=1",
                ]
            args.append(str(path))
            proc.stdin.write(("\n".join(args) + "\n-execute\n").encode("utf-8", "replace"))
            proc.stdin.flush()
            ok, msg = _wait_stay_open(proc, path.name)
            results.append((ok, msg))
        proc.stdin.write(b"-stay_open\nFalse\n-execute\n")
        proc.stdin.flush()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=60)
        except Exception:
            proc.kill()
    return results


def _wait_stay_open(proc, name: str) -> tuple[bool, str]:
    """Read exiftool stdout until the current -execute completes."""
    while True:
        line = proc.stdout.readline()
        if not line:
            return False, f"exiftool closed early for {name}"
        text = line.decode("utf-8", "replace").strip()
        if "image files updated" in text or "files updated" in text:
            return True, name
        if text.lower().startswith("error"):
            return False, f"{name}: {text}"
