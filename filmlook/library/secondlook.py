"""Second-look finder: strong photos you shot but never graded.

Score = your taste + general appeal + a small bonus for ON1 ratings/flags.

- Taste: a small model (logistic regression on the SigLIP 2 embeddings) learns from what you
  graded, flagged or rated 3+, plus your Keep / Dismiss decisions here. Each photo is scored
  by a model that never saw its own folder, so a shoot can't vouch for itself. On held-out
  folders it ranked graded photos above ungraded ones ~74% of the time (~76% with appeal).
- General appeal: zero-shot prompts ("award-winning street photograph" vs "ordinary
  snapshot") on the same SigLIP 2 model; no extra download.
- Only ungraded photos that pass the technical check, one per burst, and never a burst
  where you already graded a frame.
"""
import threading
from datetime import date, datetime, timedelta
from pathlib import PurePath

import numpy as np

from . import drives, vision

APPEAL_WEIGHT = 0.5
RATING_BONUS = {3: 0.5, 4: 0.8, 5: 1.0}
FLAG_BONUS = 0.3
SIMILAR_AT = 0.80          # "similar to N photos you graded" counts graded photos this close
TAG_Z = 1.6                # a style tag must stand out this much from a typical photo
REPEAT_FOLDER_PENALTY = 0.35  # gems: each earlier pick from the same shoot pushes the next one down

APPEAL_PROMPTS = (
    ["an award-winning street photograph", "a stunning, beautifully composed photograph with dramatic light",
     "a fine art photograph from a photography book", "a decisive moment captured on the street"],
    ["a boring, badly composed snapshot", "an ordinary amateur snapshot",
     "a cluttered, messy photo with no subject", "a tourist snapshot of friends posing"],
)
STYLE_TAGS = {
    "strong silhouette": "a dramatic silhouette of a person against bright light",
    "light and shadow": "hard sunlight with deep dramatic shadows",
    "reflections": "reflections in glass or water",
    "night lights": "a city street at night with glowing lights",
    "layered scene": "a layered composition with people at different depths",
    "strong lines": "strong leading lines and geometry",
    "candid moment": "a candid unposed moment between people",
    "vivid colour": "bold vivid saturated colours",
    "minimal": "a minimalist composition with lots of empty space",
    "rain": "rain, umbrellas and wet streets",
    "street portrait": "a close candid street portrait of a stranger",
    "movement": "motion blur conveying movement",
    "humour": "a funny, humorous juxtaposition on the street",
    "framed subject": "a person framed by a doorway or window",
}

_lock = threading.Lock()
_compute = threading.Lock()   # one computation at a time (the app warms this up in the background)
_cache = {"key": None, "data": None}


def _cache_key(con):
    return tuple(con.execute("""SELECT (SELECT COUNT(*) FROM embeddings),
        (SELECT COALESCE(SUM(graded), 0) FROM photos), (SELECT COUNT(*) FROM quality),
        (SELECT COUNT(*) || COALESCE(MAX(decided_at), '') FROM picks),
        (SELECT COALESCE(SUM(rating), 0) + COALESCE(SUM(flag), 0) FROM photos)""").fetchone())


def _scores(con):
    """Per-photo arrays, recomputed when photos, grades or decisions change."""
    key = _cache_key(con)
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
    with _compute:
        with _lock:
            if _cache["key"] == key:      # another thread just finished it
                return _cache["data"]
        return _compute_scores(con, key)


def _compute_scores(con, key):
    rows = con.execute("""SELECT p.id, p.graded, p.rating, p.flag, p.folder_id, e.vector, pk.decision
                          FROM photos p JOIN embeddings e ON e.photo_id = p.id
                          LEFT JOIN picks pk ON pk.photo_id = p.id WHERE e.model = ?""",
                       (vision.MODEL_ID,)).fetchall()
    if not rows:
        return None
    V = np.stack([np.frombuffer(r["vector"], np.float16).astype(np.float32).reshape(vision.VIEWS, -1)
                  for r in rows])
    whole = V[:, 0]
    X = np.concatenate([whole, V[:, 1:].mean(axis=1)], axis=1)
    graded = np.array([bool(r["graded"]) for r in rows])
    rating = np.array([r["rating"] or 0 for r in rows])
    flag = np.array([(r["flag"] or 0) == 4 for r in rows])
    keep = np.array([r["decision"] == "keep" for r in rows])
    dismiss = np.array([r["decision"] == "dismiss" for r in rows])
    folder = np.array([r["folder_id"] for r in rows])
    y = (graded | (rating >= 3) | flag | keep) & ~dismiss
    taste = _cross_fit(X, y, folder)

    pos, neg = (vision.embed_text(p) for p in APPEAL_PROMPTS)
    appeal = (whole @ pos.T).mean(1) - (whole @ neg.T).mean(1)
    z = lambda a: (a - np.nanmean(a)) / max(float(np.nanstd(a)), 1e-9)
    total = z(taste) + APPEAL_WEIGHT * z(appeal)
    total += np.array([RATING_BONUS.get(int(r), 0) for r in rating]) + FLAG_BONUS * flag

    tag_names = list(STYLE_TAGS)
    tag_sim = whole @ vision.embed_text([STYLE_TAGS[t] for t in tag_names]).T
    tag_z = (tag_sim - tag_sim.mean(0)) / tag_sim.std(0).clip(1e-9)

    # "Similar to N photos you graded", never counting the photo's own shoot.
    sim = whole @ whole[graded].T
    other_folder = folder[:, None] != folder[graded][None, :]
    similar = ((sim >= SIMILAR_AT) & other_folder).sum(axis=1)

    data = {"index": {r["id"]: i for i, r in enumerate(rows)}, "total": total, "taste": z(taste),
            "appeal": z(appeal), "tag_z": tag_z, "tags": tag_names, "similar": similar}
    with _lock:
        _cache.update(key=key, data=data)
    return data


def _cross_fit(X, y, groups, folds=5):
    """Score every photo with a model trained without its folder."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold
    out = np.zeros(len(y))
    if y.sum() < 5 or (~y).sum() < 5:
        return out
    k = min(folds, len(set(groups)))
    if k < 2:
        return LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced").fit(X, y).decision_function(X)
    for tr, te in GroupKFold(n_splits=k).split(X, y, groups):
        if y[tr].all() or not y[tr].any():
            continue
        m = LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced").fit(X[tr], y[tr])
        out[te] = m.decision_function(X[te])
    return out


# --- candidates and views ------------------------------------------------------------------

SELECT = """SELECT p.id, p.thumb, p.capture_time, p.camera, p.lens, p.rating, p.flag, p.folder_id,
                   COALESCE(fo.category, p.category, 'misc') AS category, fo.rel_path AS folder,
                   f.rel_path, f.drive_id, f.missing, d.name AS drive, q.sharpness, q.burst_id,
                   pk.decision
            FROM photos p
            JOIN files f ON f.id = p.primary_file_id
            JOIN folders fo ON fo.id = p.folder_id
            JOIN drives d ON d.id = f.drive_id
            JOIN quality q ON q.photo_id = p.id
            LEFT JOIN picks pk ON pk.photo_id = p.id"""

CANDIDATE = """p.graded = 0 AND q.verdict = 'ok'
    AND NOT EXISTS (SELECT 1 FROM quality q2 JOIN photos p2 ON p2.id = q2.photo_id
                    WHERE q2.burst_id = q.burst_id AND p2.graded = 1)"""


def _reasons(d, i, r, sharp_median):
    out = []
    if r["sharpness"] is not None and sharp_median and r["sharpness"] >= sharp_median:
        out.append("sharp")
    tz = d["tag_z"][i]
    for j in np.argsort(-tz)[:2]:
        if tz[j] >= TAG_Z:
            out.append(d["tags"][j])
    if d["similar"][i]:
        n = int(d["similar"][i])
        out.append(f"similar to {n} photo{'s' if n != 1 else ''} you graded")
    elif d["taste"][i] > 1.0:
        out.append("matches your taste")
    if r["rating"] and r["rating"] >= 3:
        out.append(f"{r['rating']}★ in ON1")
    if (r["flag"] or 0) == 4:
        out.append("flagged in ON1")
    return out


def suggestions(con, view="gems", categories=("street", "travel"), year=None, date_from=None, date_to=None,
                folder_id=None, offset=0, limit=20, today=None):
    """Return {total, results, note} for a view: gems, this_day, period, shortlist."""
    d = _scores(con)
    if d is None:
        return {"total": 0, "results": [], "note": "Scan your folders first."}
    where, args, note = [], [], None
    if view == "shortlist":
        where.append("pk.decision = 'keep'")
    else:
        where.append(CANDIDATE)
        where.append("(pk.decision IS NULL OR pk.decision != 'dismiss')")
    if categories and view != "shortlist":
        where.append(f"COALESCE(fo.category, p.category, 'misc') IN ({','.join('?' * len(categories))})")
        args += list(categories)
    if view == "period":
        if folder_id:
            where.append("p.folder_id = ?"); args.append(int(folder_id))
        if year:
            where.append("substr(p.capture_time, 1, 4) = ?"); args.append(str(year))
        if date_from:
            where.append("p.capture_time >= ?"); args.append(date_from)
        if date_to:
            where.append("p.capture_time < date(?, '+1 day')"); args.append(date_to)
    rows = con.execute(SELECT + " WHERE " + " AND ".join(where), args).fetchall()

    if view == "this_day":
        today = today or date.today()
        for window in (0, 3):
            days = {(today + timedelta(days=k)).strftime("%m-%d") for k in range(-window, window + 1)}
            hit = [r for r in rows if r["capture_time"] and r["capture_time"][5:10] in days
                   and r["capture_time"][:4] != str(today.year)]
            if hit or window == 3:
                rows = hit
                note = (f"Taken on {today:%-d %B} in past years." if window == 0 and hit else
                        f"Nothing from {today:%-d %B} exactly — showing {today - timedelta(days=3):%-d}–"
                        f"{today + timedelta(days=3):%-d %B} in past years." if hit else
                        f"No photos from around {today:%-d %B} in past years yet.")
                break

    # One frame per burst: the best-scoring one.
    best = {}
    for r in rows:
        i = d["index"].get(r["id"])
        if i is None:
            continue
        key = r["burst_id"] or r["id"]
        if view == "shortlist":
            key = r["id"]
        if key not in best or d["total"][i] > d["total"][best[key][1]]:
            best[key] = (r, i)
    ranked = sorted(best.values(), key=lambda t: -d["total"][t[1]])
    if view == "gems":
        ranked = _spread_shoots(ranked, d["total"])
    if view == "this_day":
        ranked.sort(key=lambda t: (t[0]["capture_time"][:4], -d["total"][t[1]]), reverse=False)
        ranked.sort(key=lambda t: t[0]["capture_time"][:4], reverse=True)

    medians = dict(con.execute("""SELECT COALESCE(p.camera, '?'), AVG(q.sharpness) FROM quality q
                                  JOIN photos p ON p.id = q.photo_id GROUP BY 1"""))
    mounts = drives.online_mounts(con)
    page = []
    for r, i in ranked[offset:offset + limit]:
        mount = mounts.get(r["drive_id"])
        page.append({
            "id": r["id"], "thumb": r["thumb"], "time": r["capture_time"], "camera": r["camera"],
            "folder": PurePath(r["folder"]).name, "category": r["category"], "name": PurePath(r["rel_path"]).name,
            "drive": r["drive"], "online": mount is not None and not r["missing"],
            "score": round(float(d["total"][i]), 3), "decision": r["decision"],
            "reasons": _reasons(d, i, r, medians.get(r["camera"] or "?")),
        })
    return {"total": len(ranked), "results": page, "note": note}


def _spread_shoots(ranked, total):
    """Greedy re-rank so one shoot doesn't fill the top: each repeat from a folder costs a bit."""
    out, seen, pool = [], {}, list(ranked)
    while pool:
        k = max(range(len(pool)), key=lambda j: total[pool[j][1]] - REPEAT_FOLDER_PENALTY * seen.get(pool[j][0]["folder_id"], 0))
        r = pool.pop(k)
        seen[r[0]["folder_id"]] = seen.get(r[0]["folder_id"], 0) + 1
        out.append(r)
        if len(out) >= 400:          # beyond the first pages plain score order is fine
            out += sorted(pool, key=lambda t: -total[t[1]])
            break
    return out


def decide(con, photo_id, decision, source="second-look"):
    """Keep / dismiss / None (undo). Saved immediately; survives restarts."""
    if decision in (None, "", "undo"):
        con.execute("DELETE FROM picks WHERE photo_id=?", (photo_id,))
    elif decision in ("keep", "dismiss"):
        con.execute("""INSERT INTO picks (photo_id, decision, source, decided_at) VALUES (?, ?, ?, datetime('now'))
                       ON CONFLICT(photo_id) DO UPDATE SET decision=excluded.decision, source=excluded.source,
                       decided_at=excluded.decided_at""", (photo_id, decision, source))
    else:
        raise ValueError(f"unknown decision: {decision}")
    con.commit()


def counts(con):
    return dict(con.execute("SELECT decision, COUNT(*) FROM picks GROUP BY decision").fetchall())
