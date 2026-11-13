"""A JPG copy of a photo to drag out of the app (onto the Desktop, a folder, an email, a chat).

Always a copy in LIBRARY_DIR/drag, never your original: Finder *moves* a dragged file when the drop is
on the same disk, so the RAW itself must never be offered. What you get is what the app shows as the
finished photo: your newest exported JPG if there is one, else the photo with the grade you set in the
Grade tab (or the default look) and its crop, resized to a long edge of 4000 px, with its EXIF.
"""
import hashlib
import json
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import LIBRARY_DIR, drives

EDGE = 4000
DIR = LIBRARY_DIR / "drag"


def _tidy():
    """Copies older than a day go (they're cheap to make again)."""
    if not DIR.exists():
        return
    old = time.time() - 86400
    for f in DIR.iterdir():
        try:
            if f.stat().st_mtime < old:
                f.unlink()
        except OSError:
            pass


def source(con, photo_id):
    """(path, is_graded_export, stem) for a library photo; raises LookupError / ConnectionError."""
    mounts = drives.online_mounts(con)
    prim = con.execute("""SELECT f.drive_id, f.rel_path, f.missing FROM photos p
                          JOIN files f ON f.id = p.primary_file_id WHERE p.id=?""", (photo_id,)).fetchone()
    if not prim:
        raise LookupError("unknown photo")
    exp = con.execute("""SELECT drive_id, rel_path FROM files WHERE photo_id=? AND role='export'
                         AND kind='jpg' AND missing=0 ORDER BY mtime DESC LIMIT 1""", (photo_id,)).fetchone()
    use = exp or prim
    mount = mounts.get(use["drive_id"])
    if not mount or (use is prim and prim["missing"]):
        raise ConnectionError("The drive this photo is on isn't plugged in.")
    return Path(mount) / use["rel_path"], bool(exp), Path(prim["rel_path"]).stem


def make(src, graded, stem):
    """Write (or reuse) the copy and return its path."""
    from .. import edits, engine
    from . import essay_out
    DIR.mkdir(parents=True, exist_ok=True)
    _tidy()
    g = None if graded else edits.get_grade(src)
    geom = None if graded else edits.get(src)
    key = hashlib.sha1(json.dumps([str(src), src.stat().st_mtime, graded, g, geom], sort_keys=True,
                                  default=str).encode()).hexdigest()[:16]
    dst = DIR / f"{key}" / f"{essay_out.safe_name(stem, 80)}.jpg"
    if dst.exists():
        return dst
    dst.parent.mkdir(exist_ok=True)
    if graded:
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    else:
        im = Image.fromarray(engine.render_full(src, essay_out._own_recipe(src) or _default_recipe(), geom=geom))
    im = essay_out._resize(im, EDGE)
    tmp = dst.with_suffix(".part.jpg")
    im.save(tmp, "JPEG", quality=92, subsampling=0, icc_profile=engine._SRGB_ICC)
    engine.copy_metadata(src, tmp)
    tmp.replace(dst)
    return dst


def _default_recipe():
    from .. import recipes
    return recipes.load_look(None)
