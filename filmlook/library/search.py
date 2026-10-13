"""Plain-words search and filters over the library."""
import threading
from pathlib import PurePath

import numpy as np

from . import drives, vision

DEFAULT_CATEGORIES = ("street", "travel")
CLOSEUP_PENALTY = 0.01   # a close-up match counts slightly less than a whole-photo match
STRONG_Z = 3.0           # "strong match": this many standard deviations above a typical photo...
STRONG_FROM_TOP = 1.5    # ...and within this much of the best match...
STRONG_MAX_BAR = 3.3     # ...but one standout photo never raises the bar above this
_cache = {"key": None, "ids": None, "matrix": None}
_cache_lock = threading.Lock()


def _matrix(con):
    key = con.execute("SELECT COUNT(*), COALESCE(SUM(photo_id), 0), COALESCE(MAX(rowid), 0) FROM embeddings "
                      "WHERE model=?", (vision.MODEL_ID,)).fetchone()
    key = tuple(key)
    with _cache_lock:
        if _cache["key"] != key:
            rows = con.execute("SELECT photo_id, vector FROM embeddings WHERE model=?", (vision.MODEL_ID,)).fetchall()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            mat = (np.stack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(np.float32)
                   .reshape(len(rows), vision.VIEWS, -1) if rows else np.zeros((0, vision.VIEWS, 1), np.float32))
            _cache.update(key=key, ids=ids, matrix=mat)
        return _cache["ids"], _cache["matrix"]


def query_vector(text):
    """Average a bare and a 'photo of' phrasing: steadier rankings for short queries."""
    v = vision.embed_text([text, f"a photo of {text}"]).mean(axis=0)
    return v / np.linalg.norm(v)


def _filters(f):
    where, args = [], []
    cats = f.get("categories", DEFAULT_CATEGORIES)
    if cats:
        where.append(f"COALESCE(fo.category, p.category, 'misc') IN ({','.join('?' * len(cats))})")
        args += list(cats)
    if f.get("year"):
        where.append("substr(p.capture_time, 1, 4) = ?"); args.append(str(f["year"]))
    if f.get("date_from"):
        where.append("p.capture_time >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("p.capture_time < date(?, '+1 day')"); args.append(f["date_to"])
    if f.get("camera"):
        where.append("p.camera = ?"); args.append(f["camera"])
    if f.get("graded") is not None:
        where.append("p.graded = ?"); args.append(1 if f["graded"] else 0)
    if f.get("folder_id"):
        where.append("p.folder_id = ?"); args.append(int(f["folder_id"]))
    if f.get("flag") in ("pick", "reject"):
        where.append("m.flag = ?"); args.append(f["flag"])
    elif f.get("flag") == "none":
        where.append("m.flag IS NULL")
    if f.get("stars"):
        where.append("COALESCE(m.rating, 0) >= ?"); args.append(int(f["stars"]))
    return (" WHERE " + " AND ".join(where)) if where else "", args


SELECT = """SELECT p.id, p.thumb, p.capture_time, p.camera, p.lens, p.focal_length, p.aperture, p.shutter,
                   p.iso, p.rating, p.flag, p.graded, p.width, p.height, p.gps_lat, p.gps_lon,
                   COALESCE(fo.category, p.category, 'misc') AS category, fo.category IS NOT NULL AS category_set,
                   fo.rel_path AS folder, fo.id AS folder_id, f.rel_path, f.drive_id, f.kind, f.missing, d.name AS drive,
                   m.flag AS mark, m.rating AS my_rating
            FROM photos p
            JOIN files f ON f.id = p.primary_file_id
            JOIN folders fo ON fo.id = p.folder_id
            JOIN drives d ON d.id = f.drive_id
            LEFT JOIN marks m ON m.photo_id = p.id"""


def _shape(r, mounts, score=None):
    mount = mounts.get(r["drive_id"])
    return {"id": r["id"], "thumb": r["thumb"], "time": r["capture_time"], "camera": r["camera"],
            "lens": r["lens"], "focal_length": r["focal_length"], "aperture": r["aperture"],
            "shutter": r["shutter"], "iso": r["iso"], "rating": r["rating"], "flag": r["flag"],
            "graded": bool(r["graded"]), "category": r["category"], "folder": PurePath(r["folder"]).name,
            "folder_id": r["folder_id"], "name": PurePath(r["rel_path"]).name, "drive": r["drive"],
            "online": mount is not None and not r["missing"],
            "path": f"{mount}/{r['rel_path']}" if mount else None,
            "score": None if score is None else round(float(score), 4),
            "mark": r["mark"], "my_rating": r["my_rating"]}


def search(con, text="", limit=200, offset=0, **filters):
    """Return {total, results}. With text, ranked by match; otherwise newest first."""
    mounts = drives.online_mounts(con)
    where, args = _filters(filters)
    rows = con.execute(SELECT + where, args).fetchall()
    if not text.strip():
        rows.sort(key=lambda r: r["capture_time"] or "", reverse=True)
        page = rows[offset:offset + limit]
        return {"total": len(rows), "results": [_shape(r, mounts) for r in page]}
    ids, mat = _matrix(con)
    if not len(ids):
        return {"total": 0, "results": []}
    s = mat @ query_vector(text.strip())                       # (N, VIEWS)
    scores = np.maximum(s[:, 0], s[:, 1:].max(axis=1) - CLOSEUP_PENALTY)
    # Relevance against the whole library: real matches stand out from the crowd.
    z = (scores - np.median(scores)) / max(float(scores.std()), 1e-6)
    cutoff = max(STRONG_Z, min(float(z.max()) - STRONG_FROM_TOP, STRONG_MAX_BAR))
    by_id = dict(zip(ids.tolist(), z.tolist()))
    ranked = sorted((r for r in rows if r["id"] in by_id), key=lambda r: -by_id[r["id"]])
    strong = sum(1 for r in ranked if by_id[r["id"]] >= cutoff)
    page = ranked[offset:offset + limit]
    results = [_shape(r, mounts, by_id[r["id"]]) for r in page]
    for r in results:
        r["strong"] = r["score"] >= cutoff
    return {"total": len(ranked), "strong": strong, "results": results}


def photo(con, photo_id):
    r = con.execute(SELECT + " WHERE p.id = ?", (photo_id,)).fetchone()
    if not r:
        return None
    out = _shape(r, drives.online_mounts(con))
    out["files"] = [dict(x) for x in con.execute(
        "SELECT rel_path, kind, role, size, missing FROM files WHERE photo_id=? ORDER BY role != 'original', rel_path",
        (photo_id,))]
    return out


def facets(con):
    """Values for the filter menus."""
    return {
        "years": [r[0] for r in con.execute("SELECT DISTINCT substr(capture_time,1,4) FROM photos "
                                            "WHERE capture_time IS NOT NULL ORDER BY 1 DESC")],
        "cameras": [r[0] for r in con.execute("SELECT camera FROM photos WHERE camera IS NOT NULL "
                                              "GROUP BY camera ORDER BY COUNT(*) DESC")],
        "categories": vision.CATEGORIES,
    }


def folder_categories(con):
    """Each shoot folder with the model's category counts and your override."""
    out = []
    for fo in con.execute("""SELECT fo.id, fo.rel_path, fo.category, d.name AS drive FROM folders fo
                             JOIN drives d ON d.id = fo.drive_id
                             WHERE EXISTS (SELECT 1 FROM photos p WHERE p.folder_id = fo.id)
                             ORDER BY fo.rel_path"""):
        counts = dict(con.execute("SELECT COALESCE(category,'?'), COUNT(*) FROM photos WHERE folder_id=? "
                                  "GROUP BY category", (fo["id"],)).fetchall())
        model = max((c for c in counts if c != "?"), key=counts.get, default=None)
        out.append({"id": fo["id"], "folder": PurePath(fo["rel_path"]).name, "path": fo["rel_path"],
                    "drive": fo["drive"], "override": fo["category"], "suggested": model,
                    "counts": counts, "photos": sum(counts.values())})
    return out


def set_folder_category(con, folder_id, category):
    if category not in (None, "", *vision.CATEGORIES):
        raise ValueError(f"unknown category: {category}")
    con.execute("UPDATE folders SET category=? WHERE id=?", (category or None, folder_id))
    con.commit()
