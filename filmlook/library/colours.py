"""How much of each colour a photo holds, measured on the library thumbnail, so a search for
"red" finds photos that are actually red (the search model is weak at colour words).

Shares are chroma-weighted fractions of the frame per OKLab hue sector, ignoring near-black and
near-grey pixels; `chroma` is the mid-tones' mean colourfulness (about 0 = black and white).
"""
import json

import numpy as np
from PIL import Image

from .. import color
from . import thumbs

# OKLab hue sectors (degrees), same boundaries as essay.HUES.
SECTORS = [("red", 350, 405), ("orange", 45, 80), ("yellow", 80, 108), ("green", 108, 170),
           ("teal", 170, 230), ("blue", 230, 285), ("purple", 285, 320), ("pink", 320, 350)]
WORDS = {"red": "red", "reds": "red", "crimson": "red", "scarlet": "red", "orange": "orange",
         "yellow": "yellow", "yellows": "yellow", "green": "green", "greens": "green", "teal": "teal",
         "turquoise": "teal", "cyan": "teal", "blue": "blue", "blues": "blue", "purple": "purple",
         "violet": "purple", "pink": "pink", "magenta": "pink"}
MONO_PHRASES = ("black and white", "black & white", "b&w", "monochrome", "mono", "bw")


def measure(im):
    small = im.convert("RGB")
    small.thumbnail((160, 160))
    lab = color.linear_to_oklab(color.srgb_decode(np.asarray(small, np.float32) / 255)).reshape(-1, 3)
    ch = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    hue = np.where(hue < 45, hue + 360, hue)              # red wraps around 0°
    ok = (ch > 0.06) & (lab[:, 0] > 0.25)
    w = np.where(ok, ch, 0) / 0.15                         # 1 = a strongly coloured pixel
    out = {"v": VERSION}
    out |= {name: round(float(w[(hue >= lo) & (hue < hi)].sum() / len(w)), 4) for name, lo, hi in SECTORS}
    mid = (lab[:, 0] > 0.2) & (lab[:, 0] < 0.92)             # judge B&W on the mid-tones, not the shadows
    out["chroma"] = round(float(ch[mid].mean()) if mid.any() else float(ch.mean()), 4)
    return out


VERSION = 2


def ensure(con):
    """Measure photos that have no (or an outdated) colour row. Returns {photo_id: shares}."""
    todo = con.execute("""SELECT p.id, p.thumb FROM photos p LEFT JOIN photo_colours c ON c.photo_id = p.id
                          WHERE p.thumb IS NOT NULL AND (c.photo_id IS NULL OR c.thumb != p.thumb
                          OR COALESCE(json_extract(c.shares, '$.v'), 1) != ?)""", (VERSION,)).fetchall()
    for r in todo:
        path = thumbs.path(r["thumb"])
        if not path.exists():
            continue
        try:
            shares = measure(Image.open(path))
        except OSError:
            continue
        con.execute("INSERT OR REPLACE INTO photo_colours (photo_id, thumb, shares) VALUES (?,?,?)",
                    (r["id"], r["thumb"], json.dumps(shares)))
    if todo:
        con.commit()
    return {r[0]: json.loads(r[1]) for r in con.execute("SELECT photo_id, shares FROM photo_colours")}


def find_words(text):
    """(colour names asked for, wants black and white, the text with those words removed)."""
    low = f" {text.lower()} "
    mono = False
    for ph in MONO_PHRASES:
        if f" {ph} " in low:
            mono = True
            low = low.replace(f" {ph} ", " ")
    words = low.split()
    cols = sorted({WORDS[w] for w in words if w in WORDS})
    rest = " ".join(w for w in words if w not in WORDS)
    return cols, mono, rest


_bw = {}


def graded_bw(con):
    """Photos whose newest graded version (ON1 or this app's export) is black and white. Uses the
    essays' small copies of graded JPGs (made once, internal disk), so it works with drives offline
    for anything seen before."""
    from . import essay
    out = set()
    for (pid,) in con.execute("SELECT DISTINCT photo_id FROM files WHERE role='export' AND kind='jpg' "
                              "AND missing=0 AND photo_id IS NOT NULL"):
        kind, path, key = essay.display_source(con, pid)
        if kind != "graded":
            continue
        if key not in _bw:
            try:
                name = essay._essay_thumb(pid, kind, path, key)
            except OSError:
                name = None
            if name is None:
                continue                          # drive offline and never seen: ask again next time
            _bw[key] = essay.is_mono(name)
        if _bw[key]:
            out.add(pid)
    return out
