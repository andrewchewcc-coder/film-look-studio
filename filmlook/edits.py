"""Where crop / straighten edits and Grade-tab grades live: the library database on the internal disk.

Never written into or next to the photo. Keyed by drive (volume UUID) + path on that drive.
"""
import json
import threading
from pathlib import Path

from . import geometry

_local = threading.local()


def _con():
    from .library import db
    c = getattr(_local, "con", None)
    if c is None:
        c = _local.con = db.connect()
    return c


def _where(path, create=False):
    from .library import drives
    con = _con()
    if create:
        drive_id, _, rel = drives.ensure_drive(con, path)
        return con, drive_id, rel
    uuid, _, _, rel = drives.locate(path)
    row = con.execute("SELECT id FROM drives WHERE uuid=?", (uuid,)).fetchone()
    return con, (row[0] if row else None), rel


def get(path):
    """The stored edit for a photo file (normalised), or the default (untouched)."""
    try:
        con, drive_id, rel = _where(path)
    except (OSError, ValueError):
        return geometry.normalise(None)
    if drive_id is None:
        return geometry.normalise(None)
    row = con.execute("SELECT geometry FROM edits WHERE drive_id=? AND rel_path=?", (drive_id, rel)).fetchone()
    return geometry.normalise(json.loads(row[0]) if row else None)


def put(path, g):
    """Save an edit; an untouched edit removes the record. Returns the normalised edit."""
    g = geometry.normalise(g)
    con, drive_id, rel = _where(path, create=True)
    if geometry.is_identity(g):
        con.execute("DELETE FROM edits WHERE drive_id=? AND rel_path=?", (drive_id, rel))
    else:
        con.execute("""INSERT INTO edits (drive_id, rel_path, geometry, updated_at) VALUES (?, ?, ?, datetime('now'))
                       ON CONFLICT(drive_id, rel_path) DO UPDATE SET geometry=excluded.geometry,
                       updated_at=excluded.updated_at""", (drive_id, rel, json.dumps(g)))
    con.commit()
    return g


def reset(path):
    return put(path, None)


# --- grades: the look and slider / curve adjustments you set in Grade, remembered per photo ---

ADJUST_DEFAULTS = {"strength": 1.0}


def is_plain(look, adjust):
    """No look and every adjustment at its default: nothing worth remembering."""
    if look not in (None, "", "neutral"):
        return False
    for k, v in (adjust or {}).items():
        if k == "curve":
            pts = [[round(float(x), 4), round(float(y), 4)] for x, y in (v or [])]
            if pts and pts != [[0.0, 0.0], [1.0, 1.0]]:
                return False
        elif v not in (None, "") and abs(float(v) - ADJUST_DEFAULTS.get(k, 0.0)) > 1e-9:
            return False
    return True


def edited_names(paths):
    """Names of these photo files (usually one folder) that have a remembered grade: one drive
    lookup and one query, so opening a big folder stays quick."""
    from .library import drives
    paths = [Path(p) for p in paths]
    if not paths:
        return []
    con, out, by_mount, mount_of = _con(), [], {}, {}
    for p in paths:
        try:
            rp = p.expanduser().resolve()
            if rp.parent not in mount_of:
                mount_of[rp.parent] = drives._mount_point(rp)
            mount = mount_of[rp.parent]
            if mount not in by_mount:
                uuid, _ = drives._volume_info(mount)
                row = con.execute("SELECT id FROM drives WHERE uuid=?", (uuid,)).fetchone()
                by_mount[mount] = row[0] if row else None
            by_mount.setdefault(("files", mount), []).append((str(rp.relative_to(mount)), p.name))
        except (OSError, ValueError):
            continue
    for key, items in by_mount.items():
        if not isinstance(key, tuple) or by_mount.get(key[1]) is None:
            continue
        saved = {r[0] for r in con.execute("SELECT rel_path FROM grades WHERE drive_id=?", (by_mount[key[1]],))}
        out += [name for rel, name in items if rel in saved]
    return out


def get_grade(path):
    """{"look", "adjust"} saved for this photo file, or None."""
    try:
        con, drive_id, rel = _where(path)
    except (OSError, ValueError):
        return None
    if drive_id is None:
        return None
    row = con.execute("SELECT look, adjust FROM grades WHERE drive_id=? AND rel_path=?", (drive_id, rel)).fetchone()
    return {"look": row[0], "adjust": json.loads(row[1])} if row else None


def put_grade(path, look, adjust):
    """Remember this photo's grade; a plain one (no look, nothing moved) removes the record."""
    con, drive_id, rel = _where(path, create=True)
    if is_plain(look, adjust):
        con.execute("DELETE FROM grades WHERE drive_id=? AND rel_path=?", (drive_id, rel))
    else:
        con.execute("""INSERT INTO grades (drive_id, rel_path, look, adjust, updated_at) VALUES (?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(drive_id, rel_path) DO UPDATE SET look=excluded.look, adjust=excluded.adjust,
                       updated_at=excluded.updated_at""", (drive_id, rel, look or "neutral", json.dumps(adjust or {})))
    con.commit()
    return get_grade(path)
