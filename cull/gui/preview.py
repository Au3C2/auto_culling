"""preview.py — fast thumbnail rendering with detection/crop overlays for the GUI.

Decoding and overlay drawing happen in background thread; safe for cross-platform.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from cull.loader import load_image_rgb
from cull.scorer import ImageScore

log = logging.getLogger(__name__)

MAX_PREVIEW = 640
_CACHE_SIZE = 32

_DETECTION_COLOR = (46, 204, 113)  # green #2ecc71
_CROP_COLOR = (243, 156, 18)       # orange #f39c12


def _fit(img: np.ndarray, max_size: int) -> np.ndarray:
    """Downscale img so longest side is <= max_size."""
    h, w = img.shape[:2]
    scale = max_size / max(h, w)
    if scale >= 1.0:
        return img
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)


@lru_cache(maxsize=_CACHE_SIZE)
def _load_cached(path_str: str, max_size: int) -> np.ndarray | None:
    try:
        path = Path(path_str)
        img = load_image_rgb(path, scale_width=max_size)
    except Exception as e:
        log.warning("Preview decode failed for %s: %s", path_str, e)
        return None
    if img is None:
        return None
    return _fit(img, max_size)


_GLOBAL_F1 = None
_GLOBAL_COCO = None


def _get_preview_detectors():
    global _GLOBAL_F1, _GLOBAL_COCO
    if _GLOBAL_F1 is None or _GLOBAL_COCO is None:
        try:
            from cull.detector import load_coco_model, load_f1_model
            model_path = Path("models/f1_yolov8n.onnx")
            _GLOBAL_F1 = load_f1_model(model_path) if model_path.exists() else None
            _GLOBAL_COCO = load_coco_model()
        except Exception as e:
            log.debug("Preview detector init failed: %s", e)
    return _GLOBAL_F1, _GLOBAL_COCO


def render_pil(score: ImageScore, max_size: int = MAX_PREVIEW) -> Image.Image | None:
    """Render score.path with bounding box and crop overlays."""
    resolved_path_str = str(Path(score.path).resolve())
    img = _load_cached(resolved_path_str, max_size)
    if img is None:
        # Fallback to direct path string if resolve differs
        img = _load_cached(str(score.path), max_size)
    if img is None:
        return None

    h, w = img.shape[:2]
    pil = Image.fromarray(img).convert("RGB")

    detections = getattr(score, "detections", None)
    crop = getattr(score, "crop", None)

    # Only draw bounding boxes and crops if the image was actually evaluated by culling!
    if detections or crop:
        draw = ImageDraw.Draw(pil)
        fx = w / max(1, getattr(score, "img_w", w) or w)
        fy = h / max(1, getattr(score, "img_h", h) or h)
        if detections:
            for det in detections:
                draw.rectangle(
                    [det.x1 * fx, det.y1 * fy, det.x2 * fx, det.y2 * fy],
                    outline=_DETECTION_COLOR, width=3,
                )
                label = getattr(det, "label", "car")
                conf = getattr(det, "conf", 0.0)
                txt = f"{label} {conf:.2f}" if conf > 0 else label
                draw.text((det.x1 * fx + 3, det.y1 * fy + 3), txt, fill=_DETECTION_COLOR)
        if crop is not None:
            top, left, bottom, right = crop
            draw.rectangle(
                [left * w, top * h, right * w, bottom * h],
                outline=_CROP_COLOR, width=2,
            )

    return pil
