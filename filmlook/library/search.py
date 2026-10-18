"""Plain-words search and filters over the library."""
import re
import threading
from pathlib import PurePath

import numpy as np

from . import colours, drives, vision

DEFAULT_CATEGORIES = ("street", "travel")
CLOSEUP_PENALTY = 0.01   # a close-up match counts slightly less than a whole-photo match
STRONG_Z = 3.0           # "strong match": this many standard deviations above a typical photo...
STRONG_FROM_TOP = 1.5    # ...and within this much of the best match...
STRONG_MAX_BAR = 3.3     # ...but one standout photo never raises the bar above this
SAME_PICTURE = 0.92      # results this alike (same shoot) are grouped: only the best one is shown up top
LIKE_Z = 2.3            # "More like this" alone: this far above typical counts as a clear match
FOLDER_BOOST = 3.5       # a word matching a shoot's folder name (a place, an event) counts this much
MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                          "august", "september", "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
FOLDER_STOP = {"the", "and", "for", "with", "photos", "photo", "pics", "images", "raw", "dng", "jpg", "edit",
               "edits", "export", "exports", "graded", "selects", "day", "trip", "area", "walk", "street", "streets",
               "new", "old", "misc", "dcim", "camera", "leica", "fuji", "fujifilm", "may"}
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


_EDITED = """EXISTS (SELECT 1 FROM files f2 JOIN grades g2 ON g2.drive_id = f2.drive_id AND g2.rel_path = f2.rel_path
                      WHERE f2.photo_id = p.id)"""          # graded in the app (on any of the photo's files)


def _filters(f):
    where, args = [], []
    cats = f.get("categories", DEFAULT_CATEGORIES)
    if cats:
        where.append(f"COALESCE(fo.category, p.category, 'misc') IN ({','.join('?' * len(cats))})")
        args += list(cats)
    if f.get("month"):
        where.append("CAST(substr(p.capture_time, 6, 2) AS INTEGER) = ?"); args.append(int(f["month"]))
    if f.get("year"):
        where.append("substr(p.capture_time, 1, 4) = ?"); args.append(str(f["year"]))
    if f.get("date_from"):
        where.append("p.capture_time >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("p.capture_time < date(?, '+1 day')"); args.append(f["date_to"])
    if f.get("camera"):
        where.append("p.camera = ?"); args.append(f["camera"])
    g = f.get("graded")
    if g == "edited":                         # graded in the app, not exported yet
        where.append(f"p.graded = 0 AND {_EDITED}")
    elif g is True:
        where.append("p.graded = 1")
    elif g is False:                          # neither exported nor graded in the app
        where.append(f"p.graded = 0 AND NOT {_EDITED}")
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
                   m.flag AS mark, m.rating AS my_rating,
                   EXISTS (SELECT 1 FROM files f2 JOIN grades g2 ON g2.drive_id = f2.drive_id AND g2.rel_path = f2.rel_path
                           WHERE f2.photo_id = p.id) AS grade_id
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
            "deleted": mount is not None and bool(r["missing"]),
            "path": f"{mount}/{r['rel_path']}" if mount else None,
            "score": None if score is None else round(float(score), 4),
            "mark": r["mark"], "my_rating": r["my_rating"], "edited": bool(r["grade_id"])}


_folder_cache = {"key": None, "words": None, "n": 0}


def _folder_words(con):
    """{word: {folder ids}} from the shoots' folder paths (places, events, people's names)."""
    key = con.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM folders").fetchone()
    key = tuple(key)
    if _folder_cache["key"] != key:
        words = {}
        rows = con.execute("SELECT id, rel_path FROM folders").fetchall()
        for r in rows:
            for w in re.split(r"[^a-z]+", r["rel_path"].lower()):
                if len(w) >= 3 and w not in FOLDER_STOP:
                    words.setdefault(w, set()).add(r["id"])
        _folder_cache.update(key=key, words=words, n=len(rows))
    return _folder_cache["words"], _folder_cache["n"]


def understand(con, text):
    """Split a search into its parts: years and months (filters), colours, shoot-folder words, and
    the words left for the picture model."""
    words = text.strip().split()
    years = [w for w in words if re.fullmatch(r"(19|20)\d\d", w)]
    year = years[-1] if years else None
    month, left = None, []
    for w in words:
        lw = w.lower().strip(",.")
        if w in years:
            continue
        if lw in MONTHS and (lw != "may" or year):          # "may" is only a month next to a year
            month = MONTHS[lw]
        else:
            left.append(w)
    rest = " ".join(left)
    cols, mono, without_colours = colours.find_words(rest)
    index, n = _folder_words(con)
    folders, folder_words = set(), []
    for w in without_colours.split():
        hits = index.get(w.lower().strip(",."))
        if hits and len(hits) <= max(3, 0.4 * n):
            folders |= hits
            folder_words.append(w.lower())
    picture = " ".join(w for w in without_colours.split() if w.lower() not in folder_words)
    return {"year": year, "month": month, "colours": cols, "mono": mono, "folders": folders,
            "folder_words": folder_words, "text": rest, "picture": picture}


def _z(x):
    return (x - np.median(x)) / max(float(x.std()), 1e-6)


def search(con, text="", limit=200, offset=0, order="newest", like=(), unlike=(), **filters):
    """Return {total, results}. With text, ranked by match; otherwise newest first
    (order="oldest": shooting order, as when looking through one shoot).

    The words are understood in parts: a year or month filters, colour words check the photo's
    actual colours, words in a shoot's folder name (a place, an event) favour that shoot, and the
    rest is matched by the picture model. Near-identical frames are grouped behind the best one.
    `like` / `unlike`: photo ids marked "More like this" / "Not this" to steer the results (they
    work with or without words)."""
    mounts = drives.online_mounts(con)
    u = understand(con, text) if text.strip() else None
    if u:
        filters = dict(filters)
        if u["year"] and not filters.get("year"):
            filters["year"] = u["year"]
        if u["month"]:
            filters["month"] = u["month"]
    where, args = _filters(filters)
    rows = con.execute(SELECT + where, args).fetchall()
    like, unlike = [int(i) for i in like], [int(i) for i in unlike]
    steer = bool(like)
    if not steer and (not u or not (u["text"] or u["colours"] or u["mono"])):
        rows.sort(key=lambda r: (r["capture_time"] or "", r["rel_path"]), reverse=order != "oldest")
        page = rows[offset:offset + limit]
        return {"total": len(rows), "results": [_shape(r, mounts) for r in page]}
    ids, mat = _matrix(con)
    if not len(ids):
        return {"total": 0, "results": []}
    u = u or {"year": None, "month": None, "colours": [], "mono": False, "folders": set(), "folder_words": [],
              "text": "", "picture": ""}
    model_words = u["picture"] or u["text"]
    if model_words or u["colours"] or u["mono"]:
        s = mat @ query_vector(model_words or " ".join(u["colours"]) or "black and white photo")   # (N, VIEWS)
        scores = np.maximum(s[:, 0], s[:, 1:].max(axis=1) - CLOSEUP_PENALTY)
        # Relevance against the whole library: real matches stand out from the crowd.
        z = _z(scores)
    else:
        z = np.zeros(len(ids), np.float32)
    if u["colours"] or u["mono"]:
        shares = colours.ensure(con)
        extra = []
        for c in u["colours"]:
            extra.append(_z(np.array([shares.get(int(i), {}).get(c, 0.0) for i in ids])))
        if u["mono"]:                         # graded in B&W first, then the least colourful frames
            bw = colours.graded_bw(con)
            extra.append(_z(-np.array([shares.get(int(i), {}).get("chroma", 0.1) for i in ids])) * 0.3
                         + np.array([6.0 if int(i) in bw else 0.0 for i in ids]))
        colour_z = np.mean(extra, axis=0)
        z = _z(0.4 * z + colour_z) if not u["picture"] else _z(z + 0.8 * colour_z)
    pos = {int(i): k for k, i in enumerate(ids)}
    liked = [mat[pos[i], 0] for i in like if i in pos]
    if liked:                                 # "More like this": photos that look like the ones you liked
        v = np.mean(liked, axis=0)
        z = _z(z + 1.2 * _z(mat[:, 0] @ (v / np.linalg.norm(v)))) if (model_words or u["colours"] or u["mono"]) \
            else _z(mat[:, 0] @ (v / np.linalg.norm(v)))
    unliked = [mat[pos[i], 0] for i in unlike if i in pos]
    if unliked:                               # "Not this": push away from what you turned down
        v = np.mean(unliked, axis=0)
        z = _z(z - 0.6 * _z(mat[:, 0] @ (v / np.linalg.norm(v))))
    cutoff = max(STRONG_Z, min(float(z.max()) - STRONG_FROM_TOP, STRONG_MAX_BAR))
    if liked and not (model_words or u["colours"] or u["mono"]):
        cutoff = LIKE_Z                       # photo-to-photo likeness spreads wider than words do
    if like or unlike:                        # your choices themselves: liked first, turned down last
        z = z + np.array([20.0 if int(i) in like else -20.0 if int(i) in unlike else 0.0 for i in ids])
    if u["folders"]:
        boost = 100.0 if not u["picture"] and not u["colours"] and not u["mono"] else FOLDER_BOOST
        folder_of = dict(con.execute("SELECT id, folder_id FROM photos").fetchall())
        z = z + np.array([boost if folder_of.get(int(i)) in u["folders"] else 0.0 for i in ids])
    by_id = dict(zip(ids.tolist(), z.tolist()))
    ranked = sorted((r for r in rows if r["id"] in by_id), key=lambda r: -by_id[r["id"]])
    # Group near-identical frames of the same shoot behind the best one (they move to the end).
    kept, kept_vecs, dups, similar = [], [], [], {}
    for r in ranked:
        if len(kept) < 400 and by_id[r["id"]] >= cutoff - 1.5 and r["id"] not in like:
            v = mat[pos[r["id"]], 0]
            twin = next((k for k, kv in zip(kept, kept_vecs) if k["folder_id"] == r["folder_id"]
                         and float(kv @ v) >= SAME_PICTURE), None)
            if twin is not None:
                dups.append(r)
                similar[twin["id"]] = similar.get(twin["id"], 0) + 1
                continue
            kept_vecs.append(v)
        kept.append(r)
    ranked = kept + dups
    strong = sum(1 for r in kept if by_id[r["id"]] >= cutoff and r["id"] not in unlike)
    page = ranked[offset:offset + limit]
    dup_ids = {r["id"] for r in dups}
    results = [_shape(r, mounts, by_id[r["id"]]) for r in page]
    for r in results:
        r["strong"] = r["score"] >= cutoff and r["id"] not in dup_ids
        r["similar"] = similar.get(r["id"], 0)
        r["grouped"] = r["id"] in dup_ids
        r["steer"] = "like" if r["id"] in like else "unlike" if r["id"] in unlike else None
        if r["steer"] == "unlike":
            r["strong"] = False
    return {"total": len(ranked), "strong": strong, "results": results,
            "understood": {k: u[k] for k in ("year", "month", "colours", "mono", "folder_words")}}


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
