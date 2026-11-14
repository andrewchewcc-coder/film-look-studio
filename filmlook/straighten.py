"""Auto-straighten: how far is the photo tilted? (Grade → Crop & straighten → Auto)

Finds straight edges near vertical (buildings, poles, door frames: Canny + a fine probabilistic
Hough on a ~1600 px preview). In a level photo taken looking up or down, verticals still lean, by
an amount that grows with their distance from the middle (keystone) — a rotation can't fix that.
So each line's lean is fitted as  tilt + k · (its offset from the centre), robustly (reweighted
least squares, long lines count more), and the tilt is the answer. Horizontals aren't used:
in street photos almost all of them are tilted by perspective. None when the evidence is thin.
Tested against the angles you set by hand (tests/test_straighten.py has the maths).
"""
import math

import numpy as np
from skimage import feature, transform

MAX_TILT = 12.0          # degrees: a bigger lean is probably intended (or a Dutch angle)
WORK_EDGE = 1600
SURE = 0.25              # confidence below this: the app says it isn't sure (missed by ~2° in tests)


def lines(gray):
    """[(lean in degrees, positive = top leans right), x offset from centre -1..1, length)]"""
    edges = feature.canny(gray, sigma=1.5, low_threshold=0.04, high_threshold=0.12)
    h, w = gray.shape
    theta = np.deg2rad(np.linspace(-MAX_TILT, MAX_TILT, 481))      # 0.05 degree steps, near-vertical only
    out = []
    for (x0, y0), (x1, y1) in transform.probabilistic_hough_line(
            edges, threshold=10, line_length=max(25, int(0.05 * max(h, w))), line_gap=3, theta=theta, rng=0):
        if y1 < y0:
            x0, y0, x1, y1 = x1, y1, x0, y0
        lean = math.degrees(math.atan2(x0 - x1, y1 - y0))          # top end right of bottom end -> positive
        out.append((lean, ((x0 + x1) / 2 - w / 2) / (w / 2), math.hypot(x1 - x0, y1 - y0)))
    return out


def fit(found, with_k=False):
    """Robust fit of lean = tilt + k * offset. Returns (tilt, share of evidence that agrees), or
    (tilt, k, agree) with with_k (k: degrees of lean per half-width away from the middle)."""
    if len(found) < 4:
        return (None, None, 0.0) if with_k else (None, 0.0)
    d, x, L = (np.array(v, np.float64) for v in zip(*found))
    wt = L.copy()
    A = np.stack([np.ones_like(x), x], axis=1)
    for _ in range(8):
        W = np.sqrt(wt)[:, None]
        coef, *_ = np.linalg.lstsq(A * W, d * W[:, 0], rcond=None)
        r = d - A @ coef
        wt = L / np.maximum(1.0, np.abs(r) / 0.5)                    # Huber-like: outliers fade
    agree = L[np.abs(r) < 0.75].sum() / L.sum()
    if with_k:
        return float(coef[0]), float(coef[1]), float(agree)
    return float(coef[0]), float(agree)


def _gray(display):
    img = np.asarray(display, np.float32)
    gray = img @ np.float32([0.2126, 0.7152, 0.0722]) if img.ndim == 3 else img
    h, w = gray.shape
    s = WORK_EDGE / max(h, w)
    if s < 1:
        gray = transform.resize(gray, (round(h * s), round(w * s)), anti_aliasing=True)
    return gray


def keystone(display):
    """Converging verticals (looking up or down). Returns (k, tilt, confidence): k = degrees of lean per
    half-width from the middle (negative: tops lean in, as when looking up), tilt as `estimate` gives it.
    (None, None, 0) when there's too little to go by."""
    gray = _gray(display)
    found = lines(gray)
    tilt, k, agree = fit(found, with_k=True)
    if tilt is None or abs(tilt) > MAX_TILT:
        return None, None, 0.0
    xs = np.array([x for _, x, _ in found]); Ls = np.array([L for *_, L in found])
    spread = float(np.sqrt(np.average(xs ** 2, weights=Ls)))          # lines only near the middle say little about k
    total = Ls.sum() / max(gray.shape)
    conf = agree * min(1.0, total / 3) * min(1.0, spread / 0.35)
    return round(k, 3), round(-tilt, 2), round(float(conf), 2)


def vertical_slider(k, w, h, k_per_unit):
    """Geometry's Vertical value that stands converging verticals of lean-slope k up straight on a
    (turned) w x h photo. Near the middle the warp moves x by a·u·v, i.e. a lean of −a·u radians at
    u (half long sides); offsets here are in half widths, so k = −degrees(a)·w/long."""
    a = -math.radians(k) * max(w, h) / w
    return a / k_per_unit


def estimate(display):
    """display: (h, w, 3) or (h, w) image 0..1 as shown. Returns (angle, confidence 0..1): the value
    for geometry's `angle` (degrees, positive = clockwise), or (None, 0) when unsure."""
    img = np.asarray(display, np.float32)
    gray = img @ np.float32([0.2126, 0.7152, 0.0722]) if img.ndim == 3 else img
    h, w = gray.shape
    s = WORK_EDGE / max(h, w)
    if s < 1:
        gray = transform.resize(gray, (round(h * s), round(w * s)), anti_aliasing=True)
    found = lines(gray)
    tilt, agree = fit(found)
    if tilt is None or abs(tilt) > MAX_TILT:
        return None, 0.0
    total = sum(L for *_, L in found) / max(gray.shape)
    conf = agree * min(1.0, total / 3)                                 # ~3 picture-heights of lines = plenty
    return round(-tilt, 2), round(conf, 2)
