"""The grading pipeline.

Stage A  `base`     linear camera RGB -> neutral display image (exposure, highlight roll-off).
Stage B  `look`     display -> display, purely per-pixel (white balance, B&W mix, tone curve,
                    colour, reference match, split toning). This stage is what gets baked
                    into a .cube LUT.
Stage C  `finish`   position-dependent effects (grain, vignette).

`render` runs all three and blends with the neutral image by `strength`. It works on
horizontal strips (via `y0` / `full_size`) so 60 MP files never need gigabytes of RAM.
"""
import math

import numpy as np

from .color import LUMA, linear_to_oklab, oklab_to_linear, srgb_decode, srgb_encode
from .recipes import HSL_BANDS

_BAND_SRGB = {
    "red": (1.0, 0.0, 0.0), "orange": (1.0, 0.5, 0.0), "yellow": (1.0, 1.0, 0.0),
    "green": (0.0, 1.0, 0.0), "aqua": (0.0, 1.0, 1.0), "blue": (0.0, 0.0, 1.0),
    "purple": (0.5, 0.0, 1.0), "magenta": (1.0, 0.0, 1.0),
}
_lab = linear_to_oklab(np.array([_BAND_SRGB[b] for b in HSL_BANDS], dtype=np.float32))
BAND_HUES = np.degrees(np.arctan2(_lab[:, 2], _lab[:, 1])) % 360
BAND_WIDTH = 25.0


def _scurve(x, contrast, pivot):
    """Pivoted power S-curve: 0->0, 1->1, slope `contrast` at `pivot`."""
    if contrast == 1.0:
        return x
    x = np.clip(x, 0.0, 1.0)
    lo = pivot * (x / pivot) ** contrast
    hi = 1.0 - (1.0 - pivot) * ((1.0 - x) / (1.0 - pivot)) ** contrast
    return np.where(x < pivot, lo, hi).astype(np.float32)


# ---------------------------------------------------------------- stage A

def base(linear, gain, exposure=0.0, display=False):
    """`display=True` for already-finished images (JPG): no shoulder or contrast boost, so with
    no exposure change the result is the original image."""
    x = linear * np.float32(gain * 2.0 ** exposure)
    if display and exposure <= 0:
        return srgb_encode(np.clip(x, 0, 1)).astype(np.float32)
    k = 0.7  # highlight shoulder: linear below k, smooth roll-off to 1.0 above
    x = np.where(x < k, x, k + (1 - k) * (1 - np.exp(-(x - k) / (1 - k))))
    if display:
        return srgb_encode(x).astype(np.float32)
    return _scurve(srgb_encode(x), 1.08, 0.45)


# ---------------------------------------------------------------- stage B

def look_lab(d, recipe):
    """Display image -> OKLab after WB, mono mix, tone and colour (before match/split)."""
    color, tone, mono = recipe["color"], recipe["tone"], recipe["mono"]
    lin = srgb_decode(d)

    t, g = color.get("temp", 0.0) / 100.0, color.get("tint", 0.0) / 100.0
    if t or g:
        gains = np.array([1 + 0.25 * t, 1 - 0.2 * g, 1 - 0.25 * t], dtype=np.float32)
        lin = lin * (gains / float(gains @ LUMA))

    if mono.get("enabled"):
        mix = np.asarray(mono.get("mix", [0.3, 0.59, 0.11]), dtype=np.float32)
        y = lin @ (mix / mix.sum())
        lin = np.repeat(y[..., None], 3, axis=-1)

    d = srgb_encode(lin)
    d = _scurve(d, tone.get("contrast", 1.0), tone.get("pivot", 0.45))
    sh, hl = tone.get("shadows", 0.0), tone.get("highlights", 0.0)
    if sh or hl:
        d = d + 2.4 * (sh * d * (1 - d) ** 3 + hl * d ** 3 * (1 - d))
    black, white = tone.get("black", 0.0), tone.get("white", 1.0)
    d = black + np.clip(d, 0, 1) * (white - black)

    lab = linear_to_oklab(srgb_decode(d))
    if mono.get("enabled"):
        lab[..., 1:] = 0.0
        return lab

    a, b = lab[..., 1], lab[..., 2]
    c = np.hypot(a, b)
    h = np.degrees(np.arctan2(b, a))
    hsl = color.get("hsl") or {}
    if hsl:
        diff = (h[..., None] - BAND_HUES + 180) % 360 - 180
        w = np.exp(-0.5 * (diff / BAND_WIDTH) ** 2)
        w /= w.sum(axis=-1, keepdims=True) + 1e-6
        dh = np.zeros_like(c); ds = np.zeros_like(c); dl = np.zeros_like(c)
        for i, band in enumerate(HSL_BANDS):
            adj = hsl.get(band)
            if not adj:
                continue
            dh += w[..., i] * adj.get("hue", 0.0)
            ds += w[..., i] * adj.get("sat", 0.0)
            dl += w[..., i] * adj.get("lum", 0.0)
        h = h + dh
        c = c * np.maximum(1 + ds, 0)
        lab[..., 0] += dl * 0.12 * np.minimum(c / 0.1, 1.0)

    c = c * color.get("saturation", 1.0)
    vib = color.get("vibrance", 0.0)
    if vib:
        c = c * (1 + vib * (1 - np.minimum(c / 0.2, 1.0)))
    hr = np.radians(h)
    lab[..., 1], lab[..., 2] = c * np.cos(hr), c * np.sin(hr)
    return lab


def lab_stats(lab):
    flat = lab.reshape(-1, 3)
    return {"mean": flat.mean(axis=0).tolist(), "std": (flat.std(axis=0) + 1e-5).tolist()}


def look_finish(lab, recipe, match_src=None):
    """Apply reference match and split toning, return display RGB."""
    m = recipe.get("match")
    if m and m.get("amount", 0) > 0:
        src = match_src or lab_stats(lab)
        mono = recipe["mono"].get("enabled")
        out = lab.copy()
        for ch in ([0] if mono else [0, 1, 2]):
            ratio = np.clip(m["std"][ch] / src["std"][ch], 0.5, 2.0)
            out[..., ch] = (lab[..., ch] - src["mean"][ch]) * ratio + m["mean"][ch]
        lab = lab + m["amount"] * (out - lab)

    split = recipe["color"].get("split") or {}
    if split:
        L = np.clip(lab[..., 0], 0, 1)
        weights = {"shadows": (1 - L) ** 2, "midtones": 4 * L * (1 - L), "highlights": L ** 2}
        for zone, w in weights.items():
            z = split.get(zone)
            if z and z.get("amount"):
                hr = math.radians(z.get("hue", 0.0))
                lab[..., 1] += z["amount"] * 0.04 * w * math.cos(hr)
                lab[..., 2] += z["amount"] * 0.04 * w * math.sin(hr)
    return srgb_encode(oklab_to_linear(lab))


def look(d, recipe, match_src=None):
    return look_finish(look_lab(d, recipe), recipe, match_src)


# ---------------------------------------------------------------- stage C

def _hash_uniform(y0, h, w, seed, x0=0):
    ys = np.arange(y0, y0 + h, dtype=np.int64).astype(np.uint32)[:, None]
    xs = np.arange(x0, x0 + w, dtype=np.int64).astype(np.uint32)[None, :]
    with np.errstate(over="ignore"):
        n = xs * np.uint32(374761393) + ys * np.uint32(668265263) + np.uint32(seed) * np.uint32(2246822519)
        n = (n ^ (n >> np.uint32(13))) * np.uint32(1274126177)
        n = n ^ (n >> np.uint32(16))
    return n.astype(np.float32) / np.float32(2 ** 32) - 0.5


def _blur_1d(x, k, axis):
    r = len(k) // 2
    pad = [(0, 0)] * x.ndim
    pad[axis] = (r, r)
    xp = np.pad(x, pad, mode="reflect")
    out = np.zeros_like(x)
    n = x.shape[axis]
    for i, kv in enumerate(k):
        out += kv * np.take(xp, range(i, i + n), axis=axis)
    return out


def grain_noise(y0, h, w, full_w, size, seed=7, x0=0):
    """Unit-variance film grain noise for the block at (x0, y0) of size w x h in a frame
    full_w wide; seamless across strips and tiles (it depends only on absolute position)."""
    sigma = size * full_w / 4000.0
    if sigma < 0.5:
        # Grain finer than a pixel: what survives is the average over each pixel's
        # area (std ~ 2*sqrt(pi)*sigma), so small previews match the full-size export.
        n = _hash_uniform(y0, h, w, seed, x0) + _hash_uniform(y0, h, w, seed + 1, x0)
        return n * np.float32(min(1.0, 3.545 * sigma) / math.sqrt(1 / 6))
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2).astype(np.float32)
    k /= k.sum()
    n = (_hash_uniform(y0 - r, h + 2 * r, w + 2 * r, seed, x0 - r)
         + _hash_uniform(y0 - r, h + 2 * r, w + 2 * r, seed + 1, x0 - r))
    n = _blur_1d(_blur_1d(n, k, 1), k, 0)[r:r + h, r:r + w]
    return n / np.float32(math.sqrt(1 / 6) * float((k ** 2).sum()))


def finish(d, recipe, y0=0, full_size=None, x0=0):
    h, w = d.shape[:2]
    full_h, full_w = full_size or (h, w)
    v = recipe.get("vignette") or {}
    if v.get("amount"):
        ys = (np.arange(y0, y0 + h, dtype=np.float32) + 0.5) / full_h * 2 - 1
        xs = (np.arange(x0, x0 + w, dtype=np.float32) + 0.5) / full_w * 2 - 1
        r = np.sqrt(ys[:, None] ** 2 + xs[None, :] ** 2) / math.sqrt(2)
        mid = v.get("midpoint", 0.5)
        t = np.clip((r - mid) / max(1.0 - mid, 1e-3), 0, 1)
        d = d * (1 - v["amount"] * t * t * (3 - 2 * t))[..., None]
    gr = recipe.get("grain") or {}
    if gr.get("amount"):
        noise = grain_noise(y0, h, w, full_w, gr.get("size", 1.0), x0=x0)
        lum = d @ LUMA
        amp = gr["amount"] * 0.045 * (0.3 + 2.8 * lum * (1 - lum))
        d = d + (noise * amp)[..., None]
    return np.clip(d, 0, 1).astype(np.float32)


# ---------------------------------------------------------------- full render

def render(linear, recipe, gain, y0=0, full_size=None, match_src=None, look_fn=None, display=False, x0=0):
    """Full render. `look_fn` can replace stage B (e.g. a baked LUT for fast exports).
    (x0, y0) / full_size place a strip or tile within the whole frame (grain, vignette)."""
    neutral = base(linear, gain, recipe.get("exposure", 0.0), display)
    graded = look_fn(neutral) if look_fn else look(neutral, recipe, match_src)
    graded = finish(graded, recipe, y0, full_size, x0)
    s = recipe.get("strength", 1.0)
    if s >= 1.0:
        return graded
    return np.clip(neutral + s * (graded - neutral), 0, 1)


def match_source_stats(linear_sample, recipe, gain, display=False):
    """Image statistics the reference match should map from (computed on a small sample)."""
    if not (recipe.get("match") and recipe["match"].get("amount", 0) > 0):
        return None
    return lab_stats(look_lab(base(linear_sample, gain, recipe.get("exposure", 0.0), display), recipe))


def reference_stats(display):
    """OKLab statistics of a reference image (sRGB display values 0..1)."""
    return lab_stats(linear_to_oklab(srgb_decode(display)))
