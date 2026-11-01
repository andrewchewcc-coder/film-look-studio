"""XMP files next to your photos, so ON1, Lightroom and other apps can see your work (⚙ Settings → XMP files).

For each photo with something to say, `<original name>.xmp` beside the original:
    xmp:Rating        your stars (1–5); a reject is -1, which most apps show as "Rejected"
    dc:subject        "Picked" for a pick (XMP has no standard pick flag)
Every file the app writes says so (xmp:CreatorTool = "Film Look Studio"). The app only ever creates a new
file or rewrites one of its own; an XMP file from ON1 or any other app is never touched — that photo is
skipped. Photos are never opened for writing. A drive that's offline is caught up when it's back.

Changes are queued in `xmp_dirty` (marks.set_mark adds to it) and written by `flush` when writing is on.
"""
import os
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import drives

CREATOR = "Film Look Studio"
PICK_KEYWORD = "Picked"


def is_ours(path):
    try:
        with open(path, "rb") as f:
            return CREATOR.encode() in f.read(4096)
    except OSError:
        return False


def target(original):
    """The XMP file for an original: same folder, same name, .xmp."""
    p = Path(original)
    return p.with_name(p.stem + ".xmp")


def content(flag, rating, original_ext, keywords=()):
    stars = -1 if flag == "reject" else int(rating or 0)
    words = ([PICK_KEYWORD] if flag == "pick" else []) + [k for k in keywords if k and k != PICK_KEYWORD]
    subject = ""
    if words:
        items = "".join(f"\n      <rdf:li>{escape(w)}</rdf:li>" for w in words)
        subject = f"\n   <dc:subject>\n    <rdf:Bag>{items}\n    </rdf:Bag>\n   </dc:subject>"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="{CREATOR}">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
   xmp:CreatorTool="{CREATOR}"
   xmp:MetadataDate="{now}"
   xmp:Rating="{stars}"
   photoshop:SidecarForExtension="{escape(original_ext.lstrip('.'))}">{subject}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def _original(con, photo_id, mounts):
    r = con.execute("""SELECT f.drive_id, f.rel_path, f.missing FROM photos p JOIN files f ON f.id = p.primary_file_id
                       WHERE p.id=?""", (photo_id,)).fetchone()
    if not r or r["missing"]:
        return None, "gone"
    mount = mounts.get(r["drive_id"])
    if not mount:
        return None, "offline"
    return Path(mount) / r["rel_path"], None


def write(con, photo_id, mounts=None):
    """Bring one photo's XMP file up to date. Returns written / unchanged / nothing / skipped / offline / gone."""
    mounts = drives.online_mounts(con) if mounts is None else mounts
    original, why = _original(con, photo_id, mounts)
    if original is None:
        return why
    m = con.execute("SELECT flag, rating FROM marks WHERE photo_id=?", (photo_id,)).fetchone()
    flag, rating = (m["flag"], m["rating"]) if m else (None, None)
    dst = target(original)
    exists = dst.exists()
    if exists and not is_ours(dst):
        return "skipped"                                   # ON1's (or another app's): leave it alone
    if not exists and not flag and not rating:
        return "nothing"                                   # nothing to say: no file needed
    text = content(flag, rating, original.suffix)
    if exists:
        old = dst.read_text("utf-8", "replace")
        strip = lambda t: "\n".join(l for l in t.splitlines() if "MetadataDate" not in l)
        if strip(old) == strip(text):
            return "unchanged"
    tmp = dst.with_name(f".{dst.name}.fls-tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, dst)                                   # all or nothing
    return "written"


def mark_dirty(con, photo_ids):
    con.executemany("INSERT OR IGNORE INTO xmp_dirty (photo_id) VALUES (?)", [(p,) for p in photo_ids])


def queue_all(con):
    """Every photo you've marked (used when you switch XMP files on)."""
    con.execute("INSERT OR IGNORE INTO xmp_dirty (photo_id) SELECT photo_id FROM marks")
    con.commit()


def flush(con, limit=500):
    """Write queued photos whose drive is online. Returns counts per outcome."""
    mounts = drives.online_mounts(con)
    counts = {}
    for (pid,) in con.execute("SELECT photo_id FROM xmp_dirty LIMIT ?", (limit,)).fetchall():
        try:
            out = write(con, pid, mounts)
        except OSError:
            out = "error"
        counts[out] = counts.get(out, 0) + 1
        if out not in ("offline", "error"):
            con.execute("DELETE FROM xmp_dirty WHERE photo_id=?", (pid,))
    con.commit()
    return counts


def pending(con):
    return con.execute("SELECT COUNT(*) FROM xmp_dirty").fetchone()[0]
