"""RAW decoding: DNG (and other LibRaw formats) to linear sRGB."""
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image

from . import dng
from .color import LUMA

RAW_EXTENSIONS = {".dng", ".raf", ".nef", ".cr3", ".cr2", ".arw", ".rw2", ".orf"}


def is_raw(path):
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def list_raws(folder):
    folder = Path(folder).expanduser()
    return sorted(p for p in folder.iterdir() if p.is_file() and is_raw(p) and not p.name.startswith("."))


def resize_float(img, size, resample=Image.LANCZOS):
    """Resize a float32 (H, W, 3) image to size=(w, h)."""
    return np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(img[..., c])).resize(size, resample))
                     for c in range(3)], axis=-1).astype(np.float32)


def decode(path, max_edge=None):
    """Decode a RAW file to linear sRGB.

    Full size (max_edge=None) returns uint16 (H, W, 3). With max_edge, a fast
    half-size decode is downscaled and returned as float32 in 0..1.

    Uses the camera's as-shot white balance and no auto-brightening; exposure
    is handled by `meter` so previews and full-size exports match. DNG lens
    corrections and the default crop are applied before rotating to the
    camera's orientation.
    """
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        active_w = raw.sizes.width
        rgb = raw.postprocess(
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
    if Path(path).suffix.lower() == ".dng":
        rgb = dng.apply(rgb, dng.read_info(path), scale=rgb.shape[1] / active_w)
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
