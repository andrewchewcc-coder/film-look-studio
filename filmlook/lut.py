"""Bake a look's per-pixel stage into a .cube 3D LUT (sRGB in, sRGB out) for ON1 and others."""
from pathlib import Path

import numpy as np

from . import pipeline


def bake(recipe, size=33, match_src=None, blend=True):
    """Return the look as an (size**3, 3) table, red varying fastest.

    blend=False leaves out `strength` (the renderer blends with the neutral image itself).
    """
    g = np.linspace(0, 1, size, dtype=np.float32)
    # .cube order: red changes fastest, then green, then blue.
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    grid = np.stack([r, gg, b], axis=-1).reshape(1, -1, 3)
    if match_src is None:
        # A reference match maps from a photo's own statistics; without a photo it can't be baked.
        recipe = {**recipe, "match": None}
    out = pipeline.look(grid, recipe, match_src)
    if blend:
        out = grid + recipe.get("strength", 1.0) * (out - grid)
    return np.clip(out.reshape(-1, 3), 0, 1).astype(np.float32)


def apply(img, table):
    """Trilinear lookup of an (H, W, 3) image in a baked table."""
    size = round(len(table) ** (1 / 3))
    x = np.clip(img, 0, 1) * np.float32(size - 1)
    i0 = np.minimum(x.astype(np.int32), size - 2)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    base = (b0 * size + g0) * size + r0
    out = None
    for db, wb in ((0, 1 - fb), (1, fb)):
        for dg, wg in ((0, 1 - fg), (1, fg)):
            for dr, wr in ((0, 1 - fr), (1, fr)):
                term = table[base + (db * size + dg) * size + dr] * (wb * wg * wr)
                out = term if out is None else out + term
    return out


def write_cube(recipe, path, size=33, match_src=None):
    table = bake(recipe, size, match_src)
    lines = [f'TITLE "{recipe.get("name", "Film Look")}"',
             "# Made with Film Look Studio. Grain and vignette are not part of a LUT.",
             f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in table]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
