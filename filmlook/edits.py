"""Where crop / straighten edits live: the library database on the internal disk.

Never written into or next to the photo. Keyed by drive (volume UUID) + path on that drive.
"""
import json
import threading

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
