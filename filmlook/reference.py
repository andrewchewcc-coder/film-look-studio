"""Turn a reference photo into a look: copy its colour and tonal feel onto your RAWs."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import pipeline, raw, recipes
from .color import linear_to_oklab, srgb_decode


def load_display(path, edge=800):
    """Load a reference image (JPEG/PNG/TIFF, or a RAW) as sRGB display values 0..1."""
    path = Path(path)
    if raw.is_raw(path):
        lin = raw.decode(path, max_edge=edge)
        return pipeline.base(lin, raw.meter(lin))
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((edge, edge))
    return np.asarray(img, dtype=np.float32) / 255.0


def looks_monochrome(display):
    """True for neutral or toned (sepia, gold, selenium) black & white images.

    In toned B&W every pixel's colour is a function of its brightness alone, so a and b
    are well predicted by a straight-line fit on L; a colour photo leaves large residuals.
    """
    lab = linear_to_oklab(srgb_decode(display)).reshape(-1, 3)[::3]
    L = lab[:, 0]
    if np.ptp(L) < 1e-3:
        return False
    worst = 0.0
    for ch in (1, 2):
        fit = np.polyval(np.polyfit(L, lab[:, ch], 2), L)
        worst = max(worst, float(np.std(lab[:, ch] - fit)))
    tint = float(np.hypot(lab[:, 1].mean(), lab[:, 2].mean()))
    return worst < 0.008 and tint < 0.06


def make_look(ref_path, name, base_look=None, amount=0.8, mono=None, label=None):
    """Create and save a user look that matches `ref_path`, optionally on top of `base_look`.

    A black & white reference automatically produces a B&W look unless `mono` says otherwise.
    """
    label = label or Path(ref_path).name
    display = load_display(ref_path)
    stats = pipeline.reference_stats(display)
    tint = float(np.hypot(stats["mean"][1], stats["mean"][2]))
    is_mono = looks_monochrome(display) if mono is None else mono
    recipe = {
        "name": name,
        "description": f"Matched to reference {label}" + (f" on top of {base_look}" if base_look else ""),
        "match": {**stats, "amount": amount, "reference": label},
    }
    if base_look:
        recipe["extends"] = base_look
    if is_mono:
        recipe["mono"] = {"enabled": True}
        # Keep the reference's paper tone (e.g. warm or selenium) as a split tone.
        if tint > 0.004:
            hue = float(np.degrees(np.arctan2(stats["mean"][2], stats["mean"][1])) % 360)
            recipe["color"] = {"split": {"midtones": {"hue": hue, "amount": min(tint / 0.04 * 1.5, 1.5)}}}
    path = recipes.save_user_look(name, recipe)
    return path.stem, path
