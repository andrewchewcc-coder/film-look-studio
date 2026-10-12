"""Technical check for the second-look finder: focus, motion blur, exposure, burst groups.

Measured once per photo from the camera's embedded preview (~1200 px; a thumbnail is too
small to judge focus) while the drive is plugged in, then stored, so the finder works
offline. Only the sharpest part of the frame counts: a sharp subject on a soft background
passes. Exposure only rejects frames beyond rescue; dark night shots are fine in RAW.
"""
import io

import numpy as np
import rawpy
from PIL import Image, ImageFilter, ImageOps

EDGE = 1200
GRID = 8                 # tiles per side for "sharpest area"
TOP_TILES = 4            # judge focus on the best few tiles

# Verdict thresholds, calibrated on Andrew's 2025-2026 folders (1:1 crops: 0.30-0.40 were
# blurred, 0.45+ sharp). Blur is judged against the same camera's typical sharpness, since
# cameras and phones process detail differently.
BLUR_SPREADS = 3.0       # blurry if this many spreads below the camera's median sharpness
BLUR_FALLBACK = 0.43     # cameras with too few photos to know their typical sharpness
MIN_CAMERA_PHOTOS = 30
MOTION_ANISOTROPY = 0.60 # blurred and the remaining detail runs one way -> motion blur
DARK_MEAN, DARK_FRAC = 0.035, 0.90
BRIGHT_FRAC = 0.70

BURST_GAP = 3.0          # seconds between frames in a burst
BURST_SIMILARITY = 0.85  # whole-photo embedding similarity between neighbours


def _preview(path, kind):
    """Grey preview around EDGE px on the long side, upright not required (we only measure)."""
    if kind == "raw":
        with rawpy.imread(str(path)) as raw:
            try:
                th = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                th = None
            if th is not None and th.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(th.data))
            elif th is not None and th.format == rawpy.ThumbFormat.BITMAP:
                im = Image.fromarray(th.data)
            else:
                im = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
    else:
        im = Image.open(path)
    if im.format == "JPEG":
        im.draft("L", (EDGE, EDGE))
    im = im.convert("L")
    im.thumbnail((EDGE, EDGE), Image.LANCZOS)
    return im


def _detail(a):
    """Per-pixel fine-detail energy (Laplacian squared)."""
    lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:])
    return lap * lap


def _tiles(e, n=GRID):
    h, w = e.shape
    th, tw = h // n, w // n
    return e[:th * n, :tw * n].reshape(n, th, n, tw).mean(axis=(1, 3))


def measure_image(im):
    """Technical numbers for a greyscale PIL image."""
    a = np.asarray(im, np.float32) / 255.0
    # Fine detail vs detail after halving resolution: focused images lose a lot when
    # halved, blurred ones barely change. A light pre-blur keeps sensor noise from
    # counting as detail.
    base = np.asarray(im.filter(ImageFilter.GaussianBlur(0.6)), np.float32) / 255.0
    half = np.asarray(im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
                      .resize(im.size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6)), np.float32) / 255.0
    fine, coarse = _tiles(_detail(base)), _tiles(_detail(half))
    contrast = _tiles(_detail(np.asarray(im.resize((im.width // 4, im.height // 4)).resize(im.size),
                                         np.float32) / 255.0))
    order = np.argsort(-(fine + coarse).ravel())[:TOP_TILES]       # the busiest (subject) tiles
    f, c = fine.ravel()[order].sum(), coarse.ravel()[order].sum()
    sharpness = float((f - c) / max(f, 1e-9))                       # 0 = blurred, ~1 = crisp
    # Direction of remaining detail in those tiles (structure tensor).
    gy, gx = np.gradient(base)
    n = GRID
    h, w = a.shape
    th, tw = h // n, w // n
    sxx = (gx * gx)[:th * n, :tw * n].reshape(n, th, n, tw).mean(axis=(1, 3)).ravel()[order].sum()
    syy = (gy * gy)[:th * n, :tw * n].reshape(n, th, n, tw).mean(axis=(1, 3)).ravel()[order].sum()
    sxy = (gx * gy)[:th * n, :tw * n].reshape(n, th, n, tw).mean(axis=(1, 3)).ravel()[order].sum()
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(tr * tr / 4 - det, 0.0) ** 0.5
    l1, l2 = tr / 2 + disc, tr / 2 - disc
    anisotropy = float((l1 - l2) / max(l1 + l2, 1e-12))
    return {"sharpness": round(sharpness, 4), "motion": round(anisotropy, 4),
            "mean_lum": round(float(a.mean()), 4), "dark_frac": round(float((a < 0.03).mean()), 4),
            "bright_frac": round(float((a > 0.97).mean()), 4),
            "contrast": round(float(contrast.ravel()[order].mean()), 6)}


def blur_cutoffs(rows):
    """rows: [(camera, sharpness)] -> {camera: cutoff}; None key = fallback."""
    by = {}
    for cam, s in rows:
        by.setdefault(cam, []).append(s)
    out = {None: BLUR_FALLBACK}
    for cam, vals in by.items():
        if len(vals) >= MIN_CAMERA_PHOTOS:
            v = np.array(vals)
            med = float(np.median(v))
            spread = float(np.median(np.abs(v - med))) * 1.4826
            out[cam] = med - BLUR_SPREADS * spread
    return out


def verdict(m, cutoff=BLUR_FALLBACK):
    if m["mean_lum"] < DARK_MEAN and m["dark_frac"] > DARK_FRAC:
        return "too_dark"
    if m["bright_frac"] > BRIGHT_FRAC:
        return "too_bright"
    if m["sharpness"] < cutoff:
        return "motion" if m["motion"] > MOTION_ANISOTROPY else "blurry"
    return "ok"


def measure(path, kind):
    return measure_image(_preview(path, kind))


def group_bursts(photos):
    """photos: [(id, folder_id, capture_time datetime|None, vector)] -> {id: burst_id}.
    Consecutive frames in one folder, a few seconds apart and nearly identical, share a
    burst; the burst id is its first photo's id."""
    out = {}
    by_folder = {}
    for p in photos:
        by_folder.setdefault(p[1], []).append(p)
    for items in by_folder.values():
        items = sorted(items, key=lambda p: (p[2] is None, p[2] or 0, p[0]))
        prev = None
        for p in items:
            if (prev is not None and p[2] is not None and prev[2] is not None
                    and (p[2] - prev[2]).total_seconds() <= BURST_GAP
                    and float(np.dot(p[3], prev[3])) >= BURST_SIMILARITY):
                out[p[0]] = out[prev[0]]
            else:
                out[p[0]] = p[0]
            prev = p
    return out
