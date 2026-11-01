"""Noise reduction and sharpening (Grade's Detail sliders).

Runs on the neutral display image, before the look, so film grain added later isn't smoothed
away or sharpened. Strengths are set in full-resolution pixels: `scale` = rendered pixels per
full-resolution pixel, so a small preview shows (almost) what the export will have — noise
and fine sharpening only really show when you zoom to 100%.

    noise        0..1  luminance noise: edge-preserving smoothing (guided filter on lightness)
    color_noise  0..1  colour blotches: blurs only the colour, never the detail
    sharpen      0..1  unsharp mask on lightness, with a threshold so flat noise isn't sharpened

Spatial filters need neighbouring pixels: `margin` says how many rows/columns a strip or tile
must borrow from around it so the result is seamless.
"""
import math

import numpy as np
from scipy import ndimage

from .color import linear_to_oklab, oklab_to_linear, srgb_decode, srgb_encode

KEYS = ("noise", "color_noise", "sharpen")


def _params(recipe, scale):
    d = recipe.get("detail") or {}
    nr, cn, sh = (max(0.0, float(d.get(k, 0.0) or 0.0)) for k in KEYS)
    radius = (1 + 3 * nr) * scale if nr else 0.0          # luminance smoothing window (pixels here)
    sigma_c = 5 * cn * scale if cn else 0.0              # colour blur
    sigma_s = 0.9 * scale if sh else 0.0                 # sharpening radius
    return nr, radius, sigma_c, sh, sigma_s


def active(recipe, scale=1.0):
    nr, radius, sigma_c, sh, sigma_s = _params(recipe, scale)
    return radius >= 0.5 or sigma_c >= 0.3 or sigma_s >= 0.3


def margin(recipe, scale=1.0):
    """Rows / columns of context a strip needs on each side."""
    if not active(recipe, scale):
        return 0
    nr, radius, sigma_c, sh, sigma_s = _params(recipe, scale)
    return int(math.ceil(max(2 * radius + 1, 3 * sigma_c, 3 * sigma_s))) + 1


def _box(x, r):
    return ndimage.uniform_filter(x, size=2 * r + 1, mode="reflect")


def apply(d, recipe, scale=1.0):
    """d: display RGB (h, w, 3) float32 -> the same with noise reduction and sharpening."""
    if not active(recipe, scale):
        return d
    nr, radius, sigma_c, sh, sigma_s = _params(recipe, scale)
    lab = linear_to_oklab(srgb_decode(np.clip(d, 0, 1)))
    L = lab[..., 0]
    if radius >= 0.5:                                    # guided filter, guided by itself
        r = max(1, int(round(radius)))
        eps = (0.006 + 0.05 * nr) ** 2
        mean = _box(L, r)
        var = np.maximum(_box(L * L, r) - mean * mean, 0)
        a = var / (var + eps)
        b = mean - a * mean
        L = _box(a, r) * L + _box(b, r)
    if sigma_c >= 0.3:
        for i in (1, 2):
            lab[..., i] = ndimage.gaussian_filter(lab[..., i], sigma_c, mode="reflect")
    if sigma_s >= 0.3:
        detail = L - ndimage.gaussian_filter(L, sigma_s, mode="reflect")
        gate = np.clip(np.abs(detail) / 0.008, 0, 1)      # leave flat, noisy areas alone
        L = L + 1.6 * sh * detail * gate
    lab[..., 0] = L
    return np.clip(srgb_encode(np.clip(oklab_to_linear(lab), 0, 1)), 0, 1).astype(np.float32)
