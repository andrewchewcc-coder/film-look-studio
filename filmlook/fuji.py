"""Fujifilm RAF lens corrections that LibRaw leaves out.

Each RAF stores Fuji's own correction tables in its maker notes: distortion, vignetting
and lateral chromatic aberration, sampled at nine radii from the image centre (radius 1 =
half the diagonal of Fuji's crop). This module reads them with exiftool and applies
distortion and vignetting the way the camera's own JPEG does, then scales so the
corrected image fills the frame. (CA: see fix_distortion.)
Checked against the camera JPEG embedded in the RAF (edge alignment improves markedly at
16mm on the XF16-80).
"""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .dng import _bilinear

EXIFTOOL = next((p for p in ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool") if os.path.exists(p)),
                "exiftool")
TAGS = ["GeometricDistortionParams", "VignettingParams", "ChromaticAberrationParams",
        "RawImageCropTopLeft", "RawImageCroppedSize"]


@dataclass
class FujiInfo:
    crop_origin: tuple = None      # (x, y) in raw-sensor pixels
    crop_size: tuple = None        # (w, h)
    knots: np.ndarray = None       # radii, 1 = half-diagonal
    distortion: np.ndarray = None  # % radial displacement at each knot (negative = barrel)
    vignetting: np.ndarray = None  # brightness in % of the centre at each knot
    ca_red: np.ndarray = None      # extra radial scale for red / blue at each knot
    ca_blue: np.ndarray = None


def _nums(v):
    return [float(x) for x in str(v).replace("x", " ").split()] if v not in (None, "") else []


def parse(tags):
    """Build FujiInfo from exiftool -n values."""
    info = FujiInfo()
    tl, size = _nums(tags.get("RawImageCropTopLeft")), _nums(tags.get("RawImageCroppedSize"))
    if len(tl) == 2 and len(size) == 2:
        info.crop_origin, info.crop_size = (int(tl[0]), int(tl[1])), (int(size[0]), int(size[1]))
    # Tables: [scale, 9 knots, 9 values (, 9 more values), ...]; prepend the centre.
    g = _nums(tags.get("GeometricDistortionParams"))
    if len(g) >= 19:
        info.knots = np.array([0.0] + g[1:10])
        info.distortion = np.array([0.0] + g[10:19])
    v = _nums(tags.get("VignettingParams"))
    if len(v) >= 19 and np.allclose(v[1:10], g[1:10] if len(g) >= 19 else v[1:10]):
        info.knots = info.knots if info.knots is not None else np.array([0.0] + v[1:10])
        info.vignetting = np.array([100.0] + v[10:19])
    c = _nums(tags.get("ChromaticAberrationParams"))
    if len(c) >= 28 and info.knots is not None:
        info.ca_red = np.array([0.0] + c[10:19])
        info.ca_blue = np.array([0.0] + c[19:28])
    return info


@lru_cache(maxsize=256)
def _read_cached(path, mtime):
    try:
        out = subprocess.run([EXIFTOOL, "-j", "-n", *[f"-{t}" for t in TAGS], path],
                             capture_output=True, timeout=30).stdout
        return parse(json.loads(out)[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return FujiInfo()


def read_info(path):
    path = str(path)
    return _read_cached(path, os.path.getmtime(path))


def fill_scale(info, w, h):
    """Enlargement after distortion correction so no edge samples fall outside the frame
    (the binding point is the middle of the nearest edge, as in Fuji's own JPEGs)."""
    half_diag = np.hypot(w, h) / 2
    worst = 0.0
    for r in (h / 2 / half_diag, w / 2 / half_diag, 1.0):   # mid short edge, mid long edge, corner
        f = 1 + np.interp(r, info.knots, info.distortion) / 100
        worst = max(worst, f)
    return 1.0 / worst


def fix_vignette(img, info, rows=256):
    h, w = img.shape[:2]
    cx, cy, half_diag = (w - 1) / 2, (h - 1) / 2, np.hypot(w, h) / 2
    xs = ((np.arange(w) - cx) / half_diag) ** 2
    top = np.iinfo(img.dtype).max if img.dtype.kind == "u" else None
    for y0 in range(0, h, rows):
        y1 = min(h, y0 + rows)
        r = np.sqrt(xs[None, :] + (((np.arange(y0, y1) - cy) / half_diag) ** 2)[:, None])
        gain = 100.0 / np.interp(r, info.knots, info.vignetting)
        img[y0:y1] = np.clip(img[y0:y1] * gain[..., None], 0, top).astype(img.dtype)
    return img


def fix_distortion(img, info, rows=256):
    """Radial distortion, scaled to fill the frame.

    The CA tables are parsed but not applied: measured on X-T5 files, the red and blue
    channels already line up with green in the corners (< 0.05 px), and applying the
    tables as radial scale offsets made the corners worse (0.6-1.4 px).
    """
    h, w = img.shape[:2]
    cx, cy, half_diag = (w - 1) / 2, (h - 1) / 2, np.hypot(w, h) / 2
    s = fill_scale(info, w, h)
    dxs = (np.arange(w, dtype=np.float32) - cx)[None, :]
    base = info.distortion / 100
    tables = [base, base, base]
    out = np.empty_like(img)

    def strip(y0):
        y1 = min(h, y0 + rows)
        dys = (np.arange(y0, y1, dtype=np.float32) - cy)[:, None]
        r = np.sqrt(dxs * dxs + dys * dys) / half_diag
        for ch in range(img.shape[2]):
            f = (1 + np.interp(r, info.knots, tables[min(ch, 2)])) * s
            vals = _bilinear(img[..., ch], cx + dxs * f, cy + dys * f)
            if img.dtype.kind == "u":
                vals = np.clip(vals + 0.5, 0, np.iinfo(img.dtype).max)
            out[y0:y1, :, ch] = vals

    with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1))) as pool:
        list(pool.map(strip, range(0, h, rows)))
    return out


def apply(img, info, scale=1.0, margins=(0, 0)):
    """Crop to Fuji's frame, then fix vignetting and distortion.

    `img` is the unrotated LibRaw output; `scale` its size relative to the full image
    (0.5 for a half-size decode); `margins` LibRaw's (left, top) margin in sensor pixels.
    """
    if info.crop_origin and info.crop_size:
        x = int(round((info.crop_origin[0] - margins[0]) * scale))
        y = int(round((info.crop_origin[1] - margins[1]) * scale))
        cw, ch = (int(round(v * scale)) for v in info.crop_size)
        x, y = max(0, x), max(0, y)
        img = np.ascontiguousarray(img[y:y + ch, x:x + cw])
    if info.knots is None:
        return img
    if info.vignetting is not None:
        img = fix_vignette(img, info)
    if info.distortion is not None:
        img = fix_distortion(img, info)
    return img
