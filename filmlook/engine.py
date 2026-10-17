"""Previews, contact sheets and full-resolution export."""
import hashlib
import io
import json
import shutil
import os
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont

from . import geometry, lut, pipeline, raw

CACHE_DIR = Path.home() / ".cache" / "filmlook"
PREVIEW_EDGE = 1600
DETAIL_EDGE = 3600       # larger cached decode, used when a tight crop needs more pixels
STRIP_ROWS = 256
WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))

_SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
_mem_cache = {}
_decode_locks = defaultdict(threading.Lock)   # one decode per photo, however many ask at once


def _key(path, edge=PREVIEW_EDGE):
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}|{edge}".encode()).hexdigest()[:16]


def preview_linear(path, edge=PREVIEW_EDGE):
    """Linear preview (long edge `edge`) and the metered gain, cached in memory and on disk.
    The gain always comes from the standard preview so every size meters the same."""
    key = _key(path, edge)
    mkey = key + ("@big" if edge != PREVIEW_EDGE else "")
    if mkey in _mem_cache:
        return _mem_cache[mkey]
    with _decode_locks[mkey]:
        if mkey in _mem_cache:            # another request decoded it while we waited
            return _mem_cache[mkey]
        return _preview_linear(path, edge, key, mkey)


_new_decodes = 0


def _prune_quietly():
    try:
        from . import appsettings
        appsettings.prune_previews()
    except Exception:
        pass


METER_VERSION = 4      # 2: never brighter than the camera; 3/4: match the camera with the white-keeping base


def _camera_gain(path, lin):
    """The gain at which our neutral render is as bright (median) as the camera's embedded preview,
    i.e. the exposure you chose when shooting. None if there's no usable preview."""
    from .library import scene
    try:
        cam = scene.upright_preview(path, "raw")
    except Exception:
        return None
    if cam is None:
        return None
    cam.thumbnail((400, 400))
    target = float(np.median(np.asarray(cam.convert("L"), np.float32) / 255))
    if not 0.02 < target < 0.98:
        return None
    small = lin[::4, ::4]
    lo, hi = np.log(0.2), np.log(8.0)
    for _ in range(16):                       # brightness rises with gain: bisect in log space
        mid = (lo + hi) / 2
        med = float(np.median(pipeline.base(small, float(np.exp(mid))) @ np.array([0.2126, 0.7152, 0.0722], np.float32)))
        lo, hi = (mid, hi) if med < target else (lo, mid)
    return float(np.exp((lo + hi) / 2))


def _auto_gain(path, lin):
    """Auto exposure for the Grade tab: as bright as the camera's own rendering (the exposure you
    chose when shooting), so Grade starts where the Library view is. The base keeps the sensor's
    white as white, so matching the camera no longer blows out skies. Without a usable camera
    preview, our meter decides."""
    cam = _camera_gain(path, lin)
    return float(cam) if cam else raw.meter(lin)


def _preview_linear(path, edge, key, mkey):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    npy, meta = CACHE_DIR / f"{key}.npy", CACHE_DIR / f"{key}.json"
    m = json.loads(meta.read_text()) if npy.exists() and meta.exists() else None
    if m is not None:
        lin, gain = np.load(npy), m["gain"]
        try:
            os.utime(meta)                        # "used recently": the cache trims the oldest first
        except OSError:
            pass
        if edge == PREVIEW_EDGE and m.get("meter") != METER_VERSION and not raw.is_display_referred(path):
            gain = _auto_gain(path, lin)              # metered with an older rule: re-check exposure
            meta.write_text(json.dumps({**m, "gain": gain, "meter": METER_VERSION}))
    else:
        lin = raw.decode(path, max_edge=edge)
        if edge == PREVIEW_EDGE:
            gain = 1.0 if raw.is_display_referred(path) else _auto_gain(path, lin)
        else:
            gain = preview_linear(path)[1]
        np.save(npy, lin)
        meta.write_text(json.dumps({"gain": gain, "source": str(path), "meter": METER_VERSION}))
        global _new_decodes
        _new_decodes += 1
        if _new_decodes % 40 == 0:                # keep the cache under its limit as it grows
            threading.Thread(target=_prune_quietly, daemon=True).start()
    if edge != PREVIEW_EDGE and len([k for k in _mem_cache if k.endswith("@big")]) >= 2:
        for k in [k for k in _mem_cache if k.endswith("@big")][:1]:   # keep memory in check
            _mem_cache.pop(k)
    _mem_cache[mkey] = (lin, gain)
    return lin, gain


def to_image(display):
    return Image.fromarray((np.clip(display, 0, 1) * 255 + 0.5).astype(np.uint8))


def render_preview(path, recipe, edge=None, geom=None):
    """Preview with the look and (optionally) a crop/straighten edit applied.

    Metering uses the whole uncropped frame, like the export, so a crop never changes exposure.
    """
    lin, gain = preview_linear(path)
    if geom is not None and not geometry.is_identity(geom):
        g = geometry.normalise(geom)
        if g["crop"] and edge and edge / max(g["crop"][2], g["crop"][3]) > max(lin.shape[:2]) * 1.05:
            lin, gain = preview_linear(path, DETAIL_EDGE)   # tight crop: start from more pixels
        lin = geometry.apply(lin, g)
    if edge and edge < max(lin.shape[:2]):
        h, w = lin.shape[:2]
        s = edge / max(h, w)
        lin = raw.resize_float(lin, (max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR)
    return to_image(render_threaded(lin, recipe, gain, display=raw.is_display_referred(path)))


def render_threaded(lin, recipe, gain, display=False, y0=0, x0=0, full_size=None, match_src=None):
    """pipeline.render split into strips over a few cores: the same picture, several times faster.
    (x0, y0) / full_size place a tile within the whole frame. A reference match needs whole-image
    statistics, so unless given they're measured once up front."""
    h, w = lin.shape[:2]
    full_size = full_size or (h, w)
    if match_src is None and full_size == (h, w):
        match_src = pipeline.match_source_stats(lin[::2, ::2], recipe, gain, display)
    if h < 2 * WORKERS * 16:
        return pipeline.render(lin, recipe, gain, y0=y0, x0=x0, full_size=full_size, match_src=match_src, display=display)
    rows = -(-h // (WORKERS * 2))
    out = np.empty((h, w, 3), dtype=np.float32)

    def strip(r0):
        out[r0:r0 + rows] = pipeline.render(lin[r0:r0 + rows], recipe, gain, y0=y0 + r0, x0=x0, full_size=full_size,
                                            match_src=match_src, display=display)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(strip, range(0, h, rows)))
    return out


_full_cache = {"key": None, "img": None}   # one full-resolution decode (with its edit) for zooming


def render_region(path, recipe, geom, box, out_w):
    """Render part of the final (edited) frame: box = [x, y, w, h] as fractions of it, at out_w
    pixels wide. Uses the larger cached preview when that's detailed enough, otherwise the
    full-resolution decode (cached for the photo being looked at). Grain and vignette are
    placed as in the whole frame, so a zoomed tile matches the export."""
    g = geometry.normalise(geom)
    W, H = geometry.output_size(*raw.full_size(path), g)
    bx, by, bw, bh = (min(max(v, 0.0), 1.0) for v in box)
    bw, bh = max(bw, 1e-4), max(bh, 1e-4)
    need = out_w / (bw * W)                       # output pixels per full-res pixel
    display = raw.is_display_referred(path)
    _, gain = preview_linear(path)
    if need <= DETAIL_EDGE / max(W, H) * 1.05:
        lin, _ = preview_linear(path, DETAIL_EDGE)
        frame = geometry.apply(lin, g) if not geometry.is_identity(g) else lin
    else:
        key = (str(path), Path(path).stat().st_mtime_ns, geometry.key(g))
        if _full_cache["key"] != key:
            _full_cache.update(key=None, img=None)                    # free the previous photo first
            full = raw.decode(path)
            _full_cache.update(key=key, img=geometry.apply(full, g) if not geometry.is_identity(g) else full)
        frame = _full_cache["img"]
    fh, fw = frame.shape[:2]
    x0, y0 = int(bx * fw), int(by * fh)
    x1, y1 = max(x0 + 1, int(round((bx + bw) * fw))), max(y0 + 1, int(round((by + bh) * fh)))
    tile = frame[y0:y1, x0:x1]
    tile = raw.to_float(tile) if tile.dtype == np.uint16 else tile
    match_src = None
    if recipe.get("match"):
        plin, _ = preview_linear(path)
        match_src = pipeline.match_source_stats(plin[::4, ::4], recipe, gain, display)
    if need < 0.75:
        # Zoomed out: shrink first, then grade at the size shown (grain lands as on the preview);
        # from about 100% up, every full-resolution pixel is graded so the tile matches the export.
        tw, th = max(1, round(tile.shape[1] * need)), max(1, round(tile.shape[0] * need))
        tile = raw.resize_float(tile, (tw, th), Image.BOX)
        x0, y0, fh, fw = round(x0 * need), round(y0 * need), round(fh * need), round(fw * need)
    out = render_threaded(tile, recipe, gain, display=display, y0=y0, x0=x0, full_size=(fh, fw), match_src=match_src)
    img = to_image(out)
    out_h = max(1, round(out_w * img.height / img.width))
    return img.resize((max(1, int(out_w)), out_h), Image.LANCZOS) if abs(img.width - out_w) > 1 else img


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


def export(src, recipe, out_dir=None, quality=95, look_name=None, progress=None, geom=None):
    """Render the full-resolution photo (RAW or JPG) with its crop/straighten edit in strips and
    write a JPEG (plus a recipe sidecar recording the look and the edit)."""
    src = Path(src)
    look_name = look_name or recipe.get("name", "graded")
    out = render_full(src, recipe, geom=geom, progress=progress)
    dst = output_path(src, look_name, out_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out).save(dst, "JPEG", quality=quality, subsampling=0, icc_profile=_SRGB_ICC)
    copy_metadata(src, dst)
    side = {**recipe, "geometry": geometry.normalise(geom)} if geom is not None else recipe
    dst.with_suffix(".recipe.json").write_text(json.dumps(side, indent=2) + "\n")
    return dst


def render_full(src, recipe, geom=None, progress=None):
    """The full-resolution graded photo (uint8 RGB array) with its crop/straighten edit."""
    src = Path(src)
    display = raw.is_display_referred(src)
    _, gain = preview_linear(src)  # same metering as the preview
    rgb16 = raw.decode(src)
    if geom is not None and not geometry.is_identity(geom):
        rgb16 = geometry.apply(rgb16, geom)
    h, w = rgb16.shape[:2]
    match_src = pipeline.match_source_stats(raw.to_float(rgb16[::8, ::8]), recipe, gain, display)
    # The per-pixel look stage is baked into a fine 3D LUT: same result, many times faster.
    table = lut.bake(recipe, size=65, match_src=match_src, blend=False)
    out = np.empty((h, w, 3), dtype=np.uint8)
    starts = list(range(0, h, STRIP_ROWS))
    done = [0]

    def strip(y0):
        y1 = min(h, y0 + STRIP_ROWS)
        d = pipeline.render(raw.to_float(rgb16[y0:y1]), recipe, gain, y0=y0, full_size=(h, w),
                            look_fn=lambda img: lut.apply(img, table), display=display)
        out[y0:y1] = (d * 255 + 0.5).astype(np.uint8)
        done[0] += 1
        if progress:
            progress(done[0] / len(starts))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(strip, starts))
    return out
