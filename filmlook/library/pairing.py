"""Group a shoot's files into photos: RAW original + copies + exported JPGs = one photo.

Exports are matched to a RAW in the same shoot folder (or its graded/ subfolder) by
file name first: `25-02-12-09-32-26-IG.jpg` and `..._fire-dragon-chrome.jpg` both
belong to `25-02-12-09-32-26.RAF`. A JPG whose name doesn't match falls back to an
exact capture-time match with exactly one RAW. Anything left over (phone photos)
is a photo of its own. When the names match, a capture time shifted by whole hours also
counts as the same moment: an export (e.g. for Instagram) can carry another time zone.

A JPG the camera saved next to its RAW (RAW + JPG mode) is the camera's own colour, not
a grade: its Software tag is the camera firmware ("3.1.1", "Digital Camera X-T5 Ver4.00") and
it was never modified (modify time = capture time, no editor history; Camera Raw or Affinity
keep the firmware tag but change those).
It joins the photo as 'camera' instead of 'export', so the photo isn't counted as graded.
Anything in a graded/ subfolder is always an export.
"""
import json
import re
from datetime import datetime
from pathlib import PurePath

SEPARATORS = " -_(.~"
COPY_RE = re.compile(r"^(?P<base>.+?)(?: copy(?: \d+)?| \(\d+\))$")
MAX_TIME_DIFF = 2.0  # seconds
MAX_ZONE_SHIFT = 14  # hours: the widest time-zone difference
EXPORT_FOLDERS = {"graded"}
EDITORS = ("on1", "lightroom", "photoshop", "adobe", "capture one", "film look", "darktable", "rawtherapee",
           "luminar", "pixelmator", "affinity", "snapseed", "dxo", "photolab", "silkypix", "acdsee", "gimp",
           "photos", "vsco", "exiftool", "raw studio", "x raw", "fujifilm x raw", "capture nx", "nx studio")
FIRMWARE_RE = re.compile(r"^(v(er)?\.?\s*)?\d+(\.\d+)+\s*$|digital camera|firmware|\bver\.?\s*\d|\bv\d+\.\d+", re.I)


def _stem(f):
    return PurePath(f["rel_path"]).stem


def _time(f):
    try:
        return datetime.fromisoformat(f["capture_time"]) if f["capture_time"] else None
    except ValueError:
        return None


def _times_agree(a, b, zone_shift=False):
    """Same moment (within 2 s) — or, with zone_shift, the same moment in another time zone."""
    ta, tb = _time(a), _time(b)
    if ta is None or tb is None:
        return True
    d = abs((ta - tb).total_seconds())
    if zone_shift and d <= MAX_ZONE_SHIFT * 3600:
        d = min(d % 1800, 1800 - d % 1800)     # zones are whole or half hours apart
    return d <= MAX_TIME_DIFF


def is_camera_jpg(f):
    """True for a JPG straight out of the camera (firmware in its Software tag, kept beside the RAW)."""
    if f["kind"] != "jpg" or PurePath(f["rel_path"]).parent.name.lower() in EXPORT_FOLDERS:
        return False
    try:
        x = json.loads(f.get("exif") or "{}")
        soft = (x.get("software") or "").strip()
    except (ValueError, AttributeError):
        return False
    if not soft or any(e in soft.lower() for e in EDITORS) or not FIRMWARE_RE.search(soft):
        return False
    if x.get("edit_history"):
        return False
    shot, modified = _time(f), _time({"capture_time": x.get("modify_time")})
    return shot is None or modified is None or abs((modified - shot).total_seconds()) <= MAX_TIME_DIFF


def _name_parent(stem, candidates):
    """Longest candidate stem that `stem` equals or extends at a separator."""
    best = None
    for c in candidates:
        if stem == c or (stem.startswith(c) and stem[len(c)] in SEPARATORS):
            if best is None or len(c) > len(best):
                best = c
    return best


def group(files):
    """files: dicts with id, rel_path, kind, capture_time (and exif JSON, for camera JPGs).
    Returns [(primary_id, [(file_id, role), ...])] — primary listed first as 'original'."""
    raws = {_stem(f): f for f in files if f["kind"] == "raw"}
    groups = {}          # primary id -> [(id, role)]
    owner = {}           # raw stem -> primary id

    # RAW originals, then "X copy.raf" / "X (1).raf" as copies of X.
    for stem in sorted(raws, key=len):
        f = raws[stem]
        m = COPY_RE.match(stem)
        base = m.group("base") if m else None
        while base and base not in raws:
            m2 = COPY_RE.match(base)
            base = m2.group("base") if m2 else None
        if base and base in owner:
            groups[owner[base]].append((f["id"], "copy"))
            owner[stem] = owner[base]
        else:
            groups[f["id"]] = [(f["id"], "original")]
            owner[stem] = f["id"]

    # Exported JPGs (and ON1 .onphoto files): by name, then by exact capture time.
    others = sorted((f for f in files if f["kind"] != "raw"), key=lambda f: len(_stem(f)))
    leftovers = []
    for f in others:
        role = "camera" if is_camera_jpg(f) else "export"
        parent = _name_parent(_stem(f), raws)
        if parent and _times_agree(f, raws[parent], zone_shift=True):
            groups[owner[parent]].append((f["id"], role))
            continue
        t = _time(f)
        same_time = [s for s, r in raws.items() if t and _time(r) == t] if t else []
        if len(same_time) == 1:
            groups[owner[same_time[0]]].append((f["id"], role))
        else:
            leftovers.append(f)

    # Standalone JPGs (phone photos); a JPG extending another's name is its export.
    standalone = {}
    for f in leftovers:
        if f["kind"] != "jpg":
            continue  # an unmatched .onphoto has no original to hang on
        parent = _name_parent(_stem(f), standalone)
        if parent and _times_agree(f, standalone[parent], zone_shift=True):
            groups[standalone[parent]["id"]].append((f["id"], "export"))
        else:
            standalone[_stem(f)] = f
            groups[f["id"]] = [(f["id"], "original")]

    return list(groups.items())
