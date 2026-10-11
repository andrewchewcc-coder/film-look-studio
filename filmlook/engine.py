"""Previews, contact sheets and full-resolution export."""
import hashlib
import io
import json
import shutil
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont

from . import lut, pipeline, raw

CACHE_DIR = Path.home() / ".cache" / "filmlook"
PREVIEW_EDGE = 1600
STRIP_ROWS = 256
WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))

_SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
_mem_cache = {}


def _key(path):
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}|{PREVIEW_EDGE}".encode()).hexdigest()[:16]


def preview_linear(path):
    """Linear preview (long edge PREVIEW_EDGE) and the metered gain, cached in memory and on disk."""
    key = _key(path)
    if key in _mem_cache:
        return _mem_cache[key]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    npy, meta = CACHE_DIR / f"{key}.npy", CACHE_DIR / f"{key}.json"
    if npy.exists() and meta.exists():
        lin = np.load(npy)
        gain = json.loads(meta.read_text())["gain"]
    else:
        lin = raw.decode(path, max_edge=PREVIEW_EDGE)
        gain = raw.meter(lin)
        np.save(npy, lin)
        meta.write_text(json.dumps({"gain": gain, "source": str(path)}))
    _mem_cache[key] = (lin, gain)
    return lin, gain


def to_image(display):
    return Image.fromarray((np.clip(display, 0, 1) * 255 + 0.5).astype(np.uint8))


def render_preview(path, recipe, edge=None):
    lin, gain = preview_linear(path)
    if edge and edge < max(lin.shape[:2]):
        h, w = lin.shape[:2]
        s = edge / max(h, w)
        lin = raw.resize_float(lin, (round(w * s), round(h * s)), Image.BILINEAR)
    return to_image(pipeline.render(lin, recipe, gain))


def jpeg_bytes(img, quality=88):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, icc_profile=_SRGB_ICC)
    return buf.getvalue()


def contact_sheet(path, looks, out_path, tile_edge=700):
    """Save a grid of the photo in each (look_id, recipe) pair, labelled."""
    tiles = [(name, render_preview(path, recipe, edge=tile_edge)) for name, recipe in looks]
    tw, th = tiles[0][1].size
    cols = min(3, len(tiles))
    rows = -(-len(tiles) // cols)
    label_h, pad = 34, 12
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + label_h + pad) + pad), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, img) in enumerate(tiles):
        x = pad + (i % cols) * (tw + pad)
        y = pad + (i // cols) * (th + label_h + pad)
        sheet.paste(img, (x, y))
        draw.text((x + 4, y + th + 6), f"{chr(65 + i)}  {name}", fill=(235, 235, 235), font=font)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "JPEG", quality=88, icc_profile=_SRGB_ICC)
    return out_path


def output_path(src, look_name, out_dir=None):
    src = Path(src)
    out_dir = Path(out_dir) if out_dir else src.parent / "graded"
    from .recipes import slug
    base = out_dir / f"{src.stem}_{slug(look_name)}.jpg"
    n = 2
    candidate = base
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{n}.jpg")
        n += 1
    return candidate


def copy_metadata(src, dst):
    """Copy EXIF/GPS etc. from the RAW into the JPEG with exiftool, if installed."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return False
    subprocess.run(
        [exiftool, "-q", "-q", "-overwrite_original", "-TagsFromFile", str(src),
         "-all:all", "-unsafe", "--icc_profile:all", "--PreviewImage", "--ThumbnailImage",
         "--JpgFromRaw", "--OtherImage", "-Orientation=1", "-n", str(dst)],
        check=False, capture_output=True,
    )
    return True


def export(src, recipe, out_dir=None, quality=95, look_name=None, progress=None):
    """Render the full-resolution RAW in strips and write a JPEG (plus a recipe sidecar)."""
    src = Path(src)
    look_name = look_name or recipe.get("name", "graded")
    _, gain = preview_linear(src)  # same metering as the preview
    rgb16 = raw.decode(src)
    h, w = rgb16.shape[:2]
    match_src = pipeline.match_source_stats(raw.to_float(rgb16[::8, ::8]), recipe, gain)
    # The per-pixel look stage is baked into a fine 3D LUT: same result, many times faster.
    table = lut.bake(recipe, size=65, match_src=match_src, blend=False)
    out = np.empty((h, w, 3), dtype=np.uint8)
    starts = list(range(0, h, STRIP_ROWS))
    done = [0]

    def strip(y0):
        y1 = min(h, y0 + STRIP_ROWS)
        d = pipeline.render(raw.to_float(rgb16[y0:y1]), recipe, gain, y0=y0, full_size=(h, w),
                            look_fn=lambda img: lut.apply(img, table))
        out[y0:y1] = (d * 255 + 0.5).astype(np.uint8)
        done[0] += 1
        if progress:
            progress(done[0] / len(starts))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(strip, starts))
    del rgb16
    dst = output_path(src, look_name, out_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out).save(dst, "JPEG", quality=quality, subsampling=0, icc_profile=_SRGB_ICC)
    copy_metadata(src, dst)
    dst.with_suffix(".recipe.json").write_text(json.dumps(recipe, indent=2) + "\n")
    return dst
