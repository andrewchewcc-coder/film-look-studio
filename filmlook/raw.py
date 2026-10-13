"""RAW decoding: DNG (and other LibRaw formats) to linear sRGB."""
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageOps

from . import dng, fuji
from .color import LUMA

RAW_EXTENSIONS = {".dng", ".raf", ".nef", ".cr3", ".cr2", ".arw", ".rw2", ".orf"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}


def is_raw(path):
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def is_jpeg(path):
    return Path(path).suffix.lower() in JPEG_EXTENSIONS


def is_display_referred(path):
    """Already-finished images (JPG): no auto exposure or base contrast, so 'neutral' = as shot."""
    return is_jpeg(path)


def list_raws(folder):
    folder = Path(folder).expanduser()
    return sorted(p for p in folder.iterdir() if p.is_file() and is_raw(p) and not p.name.startswith("."))


def list_images(folder):
    """RAWs plus JPGs that aren't an export of a RAW in the same folder (e.g. phone photos)."""
    from .library.pairing import _name_parent
    folder = Path(folder).expanduser()
    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
    raws = sorted(p for p in files if is_raw(p))
    stems = {p.stem for p in raws}
    jpgs = [p for p in files if is_jpeg(p) and not _name_parent(p.stem, stems)]
    return sorted(raws + jpgs)


def decode_jpeg(path, max_edge=None):
    """A JPG as linear sRGB, upright: uint16 full size, or float32 when max_edge is given."""
    from .color import srgb_decode
    im = Image.open(path)
    if max_edge:
        im.draft("RGB", (max_edge, max_edge))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max_edge and max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    lin = srgb_decode(np.asarray(im, np.float32) / 255.0).astype(np.float32)
    return lin if max_edge else (np.clip(lin, 0, 1) * 65535 + 0.5).astype(np.uint16)


_size_cache = {}


def full_size(path):
    """(width, height) of the full-resolution decode, upright and after lens/default crops,
    without decoding the pixels."""
    path = Path(path)
    k = (str(path), path.stat().st_mtime_ns)
    if k in _size_cache:
        return _size_cache[k]
    if is_jpeg(path):
        with Image.open(path) as im:
            w, h = im.size
            turned = im.getexif().get(0x0112, 1) in (5, 6, 7, 8)
    else:
        with rawpy.imread(str(path)) as raw:
            w, h, flip = raw.sizes.width, raw.sizes.height, raw.sizes.flip
        suffix = path.suffix.lower()
        if suffix == ".dng":
            info = dng.read_info(path)
            if info.crop_size:
                w, h = (int(round(v)) for v in info.crop_size)
        elif suffix == ".raf":
            info = fuji.read_info(path)
            if info.crop_size:
                w, h = info.crop_size
        turned = flip in (5, 6)
    _size_cache[k] = (h, w) if turned else (w, h)
    return _size_cache[k]


def resize_float(img, size, resample=Image.LANCZOS):
    """Resize a float32 (H, W, 3) image to size=(w, h)."""
    return np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(img[..., c])).resize(size, resample))
                     for c in range(3)], axis=-1).astype(np.float32)


def decode(path, max_edge=None, fast=False):
    """Decode a RAW file to linear sRGB.

    Full size (max_edge=None) returns uint16 (H, W, 3). With max_edge, a fast
    half-size decode is downscaled and returned as float32 in 0..1.

    Uses the camera's as-shot white balance and no auto-brightening; exposure
    is handled by `meter` so previews and full-size exports match. DNG lens
    corrections and the default crop are applied before rotating to the
    camera's orientation. Fuji RAFs get Fuji's crop and lens corrections the same way.
    """
    if is_jpeg(path):
        return decode_jpeg(path, max_edge)
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        active_w = raw.sizes.width
        margins = (raw.sizes.left_margin, raw.sizes.top_margin)
        # fast=True (zoom-to-check-focus): simple demosaic, ~20x quicker on Fuji X-Trans
        extra = {"demosaic_algorithm": rawpy.DemosaicAlgorithm.LINEAR} if fast else {}
        rgb = raw.postprocess(
            **extra,
            use_camera_wb=True,
            no_auto_bright=True,
            gamma=(1, 1),
            output_bps=16,
            output_color=rawpy.ColorSpace.sRGB,
            half_size=max_edge is not None,
            highlight_mode=rawpy.HighlightMode.Clip,
            user_flip=0,
        )
    if max_edge is not None:
        rgb = to_float(rgb)
        h, w = rgb.shape[:2]
        s = max_edge / max(h, w)
        if s < 1:
            rgb = np.clip(resize_float(rgb, (round(w * s), round(h * s))), 0, 1)
    suffix = Path(path).suffix.lower()
    if suffix == ".dng":
        rgb = dng.apply(rgb, dng.read_info(path), scale=rgb.shape[1] / active_w)
    elif suffix == ".raf":
        rgb = fuji.apply(rgb, fuji.read_info(path), scale=rgb.shape[1] / active_w, margins=margins)
    if flip == 3:
        rgb = rgb[::-1, ::-1]
    elif flip == 5:
        rgb = np.rot90(rgb, 1)
    elif flip == 6:
        rgb = np.rot90(rgb, -1)
    return np.ascontiguousarray(rgb)


def to_float(rgb16):
    return rgb16.astype(np.float32) / 65535.0


def meter(linear):
    """Return an exposure gain that brings the scene to a pleasant mid-grey.

    Corrects only partially (street shots are often meant to be dark or bright)
    and clamps the gain so night scenes are not blown up to daylight.
    """
    sample = linear[::4, ::4].reshape(-1, 3)
    lum = sample @ LUMA
    lum = lum[lum < 0.98]  # ignore clipped highlights
    if lum.size == 0:
        return 1.0
    log_avg = float(np.exp(np.mean(np.log(lum + 1e-4))))
    gain = (0.16 / max(log_avg, 1e-4)) ** 0.75
    return float(np.clip(gain, 0.5, 4.0))
