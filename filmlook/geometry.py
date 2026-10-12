"""Crop and straighten: non-destructive geometry applied after decoding, before the grade.

Order: (perspective — reserved) -> flip / 90-degree turns -> fine rotation -> crop.

Everything is resolution independent, so one stored edit gives the same framing on the
1600 px preview, a 512 px thumbnail and the full-resolution export:
  - `rot90`  : 0 / 90 / 180 / 270 clockwise, after optional `flip_h` / `flip_v`
  - `angle`  : fine rotation in degrees, positive = clockwise, about the image centre;
               the canvas keeps the turned image's size (corners outside are empty)
  - `crop`   : [x, y, w, h] as fractions of that canvas, or None for the whole frame
  - `aspect` : the ratio label the crop was made with (for the UI), e.g. "3:2", "free"
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .dng import _bilinear

DEFAULT = {"rot90": 0, "flip_h": False, "flip_v": False, "angle": 0.0, "crop": None,
           "aspect": "original", "perspective": None}
MAX_ANGLE = 45.0


def normalise(g):
    """Validate an edit coming from the UI or the database; returns a clean dict."""
    g = {**DEFAULT, **(g or {})}
    out = {"rot90": int(g["rot90"]) % 360 // 90 * 90, "flip_h": bool(g["flip_h"]), "flip_v": bool(g["flip_v"]),
           "angle": float(max(-MAX_ANGLE, min(MAX_ANGLE, float(g["angle"] or 0.0)))),
           "aspect": str(g.get("aspect") or "original")[:20], "perspective": None, "crop": None}
    c = g.get("crop")
    if c:
        x, y, w, h = (float(v) for v in c)
        w, h = max(min(w, 1.0), 1e-3), max(min(h, 1.0), 1e-3)
        x, y = max(0.0, min(x, 1.0 - w)), max(0.0, min(y, 1.0 - h))
        if not (abs(x) < 1e-6 and abs(y) < 1e-6 and abs(w - 1) < 1e-6 and abs(h - 1) < 1e-6):
            out["crop"] = [round(x, 6), round(y, 6), round(w, 6), round(h, 6)]
    return out


def is_identity(g):
    g = normalise(g)
    return not (g["rot90"] or g["flip_h"] or g["flip_v"] or abs(g["angle"]) > 1e-6 or g["crop"])


def turned_size(w, h, g):
    """Canvas size after flips and 90-degree turns."""
    return (h, w) if normalise(g)["rot90"] in (90, 270) else (w, h)


def output_size(w, h, g):
    """Pixel size of the final cropped image for a source of w x h (oriented)."""
    g = normalise(g)
    cw, ch = turned_size(w, h, g)
    if not g["crop"]:
        return cw, ch
    return max(1, round(g["crop"][2] * cw)), max(1, round(g["crop"][3] * ch))


def _turn(img, g):
    if g["flip_h"]:
        img = img[:, ::-1]
    if g["flip_v"]:
        img = img[::-1]
    k = g["rot90"] // 90
    if k:
        img = np.rot90(img, -k)          # clockwise
    return np.ascontiguousarray(img)


def valid(x, y, cw, ch, angle):
    """Is canvas point (x, y) inside the rotated image? (pixel units, canvas cw x ch)"""
    t = math.radians(angle)
    dx, dy = x - cw / 2, y - ch / 2
    sx = cw / 2 + dx * math.cos(t) + dy * math.sin(t)   # inverse of a clockwise turn
    sy = ch / 2 - dx * math.sin(t) + dy * math.cos(t)
    return -1e-6 <= sx <= cw + 1e-6 and -1e-6 <= sy <= ch + 1e-6


def max_crop(cw, ch, angle, ratio=None, center=(0.5, 0.5)):
    """Largest crop of `ratio` (w/h in pixels; None = canvas ratio) around `center` that has no
    empty corners after rotating by `angle`. Returns [x, y, w, h] as fractions."""
    ratio = ratio or cw / ch
    lo, hi = 0.0, 1.0
    cx, cy = center[0] * cw, center[1] * ch
    for _ in range(40):                 # bisect on scale: corners inside the rotated image?
        s = (lo + hi) / 2
        w = s * min(cw, ch * ratio)
        h = w / ratio
        corners = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2)]
        ok = all(0 <= px <= cw and 0 <= py <= ch and valid(px, py, cw, ch, angle) for px, py in corners)
        lo, hi = (s, hi) if ok else (lo, s)
    w = lo * min(cw, ch * ratio)
    h = w / ratio
    return [(cx - w / 2) / cw, (cy - h / 2) / ch, w / cw, h / ch]


def apply(img, g, rows=256):
    """Apply an edit to an oriented image (H x W x 3, uint16 or float32)."""
    g = normalise(g)
    if is_identity(g):
        return img
    img = _turn(img, g)
    ch, cw = img.shape[:2]
    x, y, w, h = g["crop"] or (0.0, 0.0, 1.0, 1.0)
    ox, oy = x * cw, y * ch
    ow, oh = max(1, round(w * cw)), max(1, round(h * ch))
    if abs(g["angle"]) < 1e-6:          # straight crop: exact pixels, no resampling
        x0, y0 = int(round(ox)), int(round(oy))
        return np.ascontiguousarray(img[y0:y0 + oh, x0:x0 + ow])
    t = math.radians(g["angle"])
    cos, sin = np.float32(math.cos(t)), np.float32(math.sin(t))
    out = np.empty((oh, ow) + img.shape[2:], dtype=img.dtype)
    xs = (np.arange(ow, dtype=np.float32) + np.float32(ox + 0.5 - cw / 2))[None, :]

    def strip(r0):
        r1 = min(oh, r0 + rows)
        ys = (np.arange(r0, r1, dtype=np.float32) + np.float32(oy + 0.5 - ch / 2))[:, None]
        sx = cw / 2 + xs * cos + ys * sin - 0.5
        sy = ch / 2 - xs * sin + ys * cos - 0.5
        for c in range(img.shape[2]):
            vals = _bilinear(img[..., c], sx, sy)
            if img.dtype.kind == "u":
                vals = np.clip(vals + 0.5, 0, np.iinfo(img.dtype).max)
            out[r0:r1, :, c] = vals

    with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1))) as pool:
        list(pool.map(strip, range(0, oh, rows)))
    return out


def with_ratio(g, w, h, ratio, label):
    """Give an edit the largest centred crop of `ratio` (width / height in pixels) for a
    w x h (oriented) photo, keeping its own turns and straightening."""
    g = normalise(g)
    cw, ch = turned_size(w, h, g)
    g["crop"] = max_crop(cw, ch, g["angle"], ratio=float(ratio))
    g["aspect"] = label
    return normalise(g)


def key(g):
    """Short stable id of an edit (for cache keys and thumbnail names); '' when untouched."""
    if is_identity(g):
        return ""
    import hashlib
    import json
    return hashlib.sha1(json.dumps(normalise(g), sort_keys=True).encode()).hexdigest()[:8]
