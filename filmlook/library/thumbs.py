"""Small previews for browsing, made from the JPEG preview embedded in each RAW
(no RAW processing) or from the JPG itself. Stored on the internal disk."""
import io
from pathlib import Path

import rawpy
from PIL import Image, ImageOps

from . import LIBRARY_DIR

THUMB_DIR = LIBRARY_DIR / "thumbs"
EDGE = 512
RAW_FLIP = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90, 6: Image.Transpose.ROTATE_270}


def name_for(file_id, quick_hash):
    """Thumbnail file name; changes when the source file changes."""
    return f"{file_id}_{(quick_hash or 'x')[:8]}.jpg"


def _open_jpeg(data_or_path):
    im = Image.open(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path)
    im.draft("RGB", (EDGE * 2, EDGE * 2))   # JPEG DCT downscale: reads a fraction of the pixels
    return im


def _from_raw(path):
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        try:
            th = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            th = None
        im = None
        if th is not None and th.format == rawpy.ThumbFormat.JPEG:
            im = _open_jpeg(th.data)
            if im.getexif().get(0x0112, 1) != 1:
                im = ImageOps.exif_transpose(im)
            elif flip in RAW_FLIP:
                # Embedded preview carries no orientation of its own: use the RAW's.
                im = im.transpose(RAW_FLIP[flip])
        elif th is not None and th.format == rawpy.ThumbFormat.BITMAP:
            im = Image.fromarray(th.data)
            im = im.transpose(RAW_FLIP[flip]) if flip in RAW_FLIP else im
        if im is not None and not _is_mono(im):
            return im
        # No usable preview, or a black-and-white one from the camera's monochrome
        # mode while the RAW itself holds colour: fast half-size decode instead.
        rgb = raw.postprocess(half_size=True, use_camera_wb=True)
        return Image.fromarray(rgb)


def _is_mono(im):
    small = im.convert("RGB").resize((64, 64))
    r, g, b = small.split()
    spread = [max(px) - min(px) for px in zip(r.getdata(), g.getdata(), b.getdata())]
    return sum(spread) / len(spread) < 4


def make(path, kind, out_name):
    """Write THUMB_DIR/out_name for the photo file at path. Returns the name."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    if kind == "raw":
        im = _from_raw(path)
    else:
        im = ImageOps.exif_transpose(_open_jpeg(str(path)))
    im = im.convert("RGB")
    im.thumbnail((EDGE, EDGE), Image.LANCZOS)
    tmp = THUMB_DIR / (out_name + ".tmp")
    im.save(tmp, "JPEG", quality=82, optimize=True)
    tmp.replace(THUMB_DIR / out_name)      # never leave a half-written thumbnail
    return out_name


def path(name):
    return THUMB_DIR / name if name else None


def remove_stale(keep):
    """Delete thumbnails no photo refers to any more (internal disk only)."""
    if not THUMB_DIR.exists():
        return 0
    if not keep and any(THUMB_DIR.glob("*.jpg")):
        return 0  # an empty keep-list would wipe everything: never trust it
    n = 0
    for p in THUMB_DIR.glob("*.jpg"):
        if p.name not in keep:
            p.unlink()
            n += 1
    return n
