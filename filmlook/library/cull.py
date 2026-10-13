"""Culling: group a shoot's frames, rank each group, suggest a pick with a reason.

Groups are frames of the same scene: same folder, consecutive, a few seconds apart and
looking alike (looser than Step 3's bursts — street shooters re-frame a scene over half a
minute). Within a group every frame gets seven component scores (subject focus, face/eyes,
gesture, edges, separation, exposure, your taste); each is compared *within the group*
(z-scores), weighted, and summed. The weights start as sensible defaults and are re-fitted
from your own choices (exports, picks, rejects) — see `fit_weights`.
"""
import json
from datetime import datetime

import numpy as np

from . import vision

GROUP_GAP = 30.0         # seconds between consecutive frames of one scene
GROUP_SIMILARITY = 0.80  # whole-photo embedding similarity to the previous frame
UNSURE_MARGIN = 0.6      # top two frames closer than this (weighted z units) -> "unsure"

COMPONENTS = ["focus", "face", "gesture", "edges", "separation", "exposure", "taste"]
DEFAULT_WEIGHTS = {"focus": 1.0, "face": 0.8, "gesture": 0.6, "edges": 0.5, "separation": 0.4,
                   "exposure": 0.6, "taste": 0.9}


# --- loading ----------------------------------------------------------------------------------

def load_frames(con, photo_ids):
    """Everything ranking needs for these photos (missing analysis -> neutral values)."""
    from .secondlook import _scores
    if not photo_ids:
        return []
    qs = ",".join("?" * len(photo_ids))
    rows = con.execute(f"""SELECT p.id, p.folder_id, p.capture_time, p.camera, p.graded, p.thumb,
                                  q.sharpness, q.verdict, q.mean_lum, s.features, e.vector
                           FROM photos p LEFT JOIN quality q ON q.photo_id = p.id
                           LEFT JOIN scene s ON s.photo_id = p.id
                           LEFT JOIN embeddings e ON e.photo_id = p.id AND e.model = ?
                           WHERE p.id IN ({qs})""", [vision.MODEL_ID, *photo_ids]).fetchall()
    taste = _scores(con)
    frames = []
    for r in rows:
        f = json.loads(r["features"]) if r["features"] else {}
        try:
            t = datetime.fromisoformat(r["capture_time"]) if r["capture_time"] else None
        except ValueError:
            t = None
        vec = (np.frombuffer(r["vector"], np.float16).astype(np.float32).reshape(vision.VIEWS, -1)[0]
               if r["vector"] else None)
        ti = taste["index"].get(r["id"]) if taste else None
        frames.append({"id": r["id"], "folder_id": r["folder_id"], "time": t, "graded": bool(r["graded"]),
                       "thumb": r["thumb"], "verdict": r["verdict"], "sharpness": r["sharpness"], "f": f,
                       "vec": vec, "taste": float(taste["taste"][ti]) if ti is not None else 0.0})
    return frames


# --- grouping ---------------------------------------------------------------------------------

def group(frames, gap=GROUP_GAP, similarity=GROUP_SIMILARITY):
    """Frames -> list of groups (lists of frames, in shooting order)."""
    frames = sorted(frames, key=lambda fr: (fr["folder_id"], fr["time"] is None, fr["time"] or datetime.min, fr["id"]))
    groups, cur = [], []
    for fr in frames:
        prev = cur[-1] if cur else None
        same = (prev is not None and prev["folder_id"] == fr["folder_id"] and prev["time"] and fr["time"]
                and (fr["time"] - prev["time"]).total_seconds() <= gap
                and prev["vec"] is not None and fr["vec"] is not None
                and float(np.dot(prev["vec"], fr["vec"])) >= similarity)
        if same:
            cur.append(fr)
        else:
            if cur:
                groups.append(cur)
            cur = [fr]
    if cur:
        groups.append(cur)
    return groups


# --- scoring ----------------------------------------------------------------------------------

def components(fr):
    """Raw component values for one frame (higher is better)."""
    f = fr["f"]
    has_face = "face_quality" in f and f.get("face_size", 0) > 0.0004
    focus = f.get("face_sharp") if has_face and f.get("face_sharp") is not None else f.get("subject_sharp")
    if focus is None:
        focus = fr["sharpness"] or 0.0
    face = 0.0
    if has_face:
        eyes = f.get("eyes_open")
        face = f["face_quality"] - (0.35 if eyes is not None and eyes < 0.12 else 0.0)   # eyes shut
    gesture = f.get("gesture", 0.0) + 0.5 * f.get("interaction", 0.0)
    edges = -f.get("edge_cut", 0.0) - 0.3 * max(0.0, f.get("edge_clutter", 1.0) - 1.0)
    separation = f.get("separation", 0.0) + 0.2 * f.get("saliency", 0.0)
    exposure = -2.0 * f.get("subject_clipped", 0.0) - (1.0 if fr["verdict"] not in (None, "ok") else 0.0)
    return {"focus": float(focus), "face": float(face), "gesture": float(gesture), "edges": float(edges),
            "separation": float(separation), "exposure": float(exposure), "taste": fr["taste"]}


def _within(groups_values):
    """Per-component z-scores within the group; components that barely differ contribute ~0."""
    M = np.array([[v[c] for c in COMPONENTS] for v in groups_values], dtype=np.float64)
    if len(M) == 1:
        return np.zeros_like(M)
    floor = np.array([0.03, 0.05, 0.08, 0.08, 0.03, 0.05, 0.3])   # ignore differences below noise
    sd = np.maximum(M.std(axis=0), floor)
    return (M - M.mean(axis=0)) / sd


REASONS = {
    "focus": ("sharpest face", "sharpest subject"),
    "face": ("best face, eyes open", "best face, eyes open"),
    "gesture": ("best gesture / stride", "best gesture / stride"),
    "edges": ("cleanest edges", "cleanest edges"),
    "separation": ("subject stands out most", "subject stands out most"),
    "exposure": ("best exposed", "best exposed"),
    "taste": ("most like your picks", "most like your picks"),
}


def rank(grp, weights=None):
    """Rank one group. Returns {order: [ids best first], scores: {id: total}, pick, margin,
    unsure, reasons: [..] for the pick, parts: {id: {component: z}}}."""
    w = np.array([(weights or DEFAULT_WEIGHTS).get(c, 0.0) for c in COMPONENTS])
    vals = [components(fr) for fr in grp]
    Z = _within(vals)
    totals = Z @ w
    order = np.argsort(-totals)
    pick = grp[order[0]]
    margin = float(totals[order[0]] - totals[order[1]]) if len(grp) > 1 else None
    reasons = []
    if len(grp) > 1:
        contrib = Z[order[0]] * w
        for k in np.argsort(-contrib):
            if contrib[k] > 0.25 and Z[order[0], k] >= Z[:, k].max() - 1e-9 and len(reasons) < 3:
                face = "face_sharp" in pick["f"] and pick["f"].get("face_size", 0) > 0.0004
                reasons.append(REASONS[COMPONENTS[k]][0 if face else 1])
        if not reasons:
            reasons = ["best balance of focus, moment and framing"]
    else:
        reasons = single_reasons(pick, vals[0])
    return {"order": [grp[i]["id"] for i in order], "scores": {grp[i]["id"]: round(float(totals[i]), 3) for i in range(len(grp))},
            "pick": pick["id"], "margin": None if margin is None else round(margin, 3),
            "unsure": margin is not None and margin < UNSURE_MARGIN, "reasons": reasons,
            "parts": {grp[i]["id"]: {c: round(float(Z[i, j]), 2) for j, c in enumerate(COMPONENTS)} for i in range(len(grp))}}


def single_reasons(fr, v):
    """A frame on its own: say what's notable (or wrong) about it."""
    out = []
    if fr["verdict"] in ("blurry", "motion"):
        return ["soft / blurred"]
    if fr["verdict"] in ("too_dark", "too_bright"):
        return ["exposure hard to rescue"]
    f = fr["f"]
    if f.get("gesture", 0) > 0.45:
        out.append("strong gesture")
    if f.get("interaction", 0) > 0.4:
        out.append("people interacting")
    if "face_quality" in f and f["face_quality"] > 0.5:
        out.append("clear face")
    if f.get("edge_cut", 0) >= 0.5:
        out.append("someone cut at the edge")
    if fr["taste"] > 1.0:
        out.append("like your picks")
    return out or ["single frame"]


# --- learning weights from choices ------------------------------------------------------------

def pairs_from_groups(groups, chosen):
    """Training pairs (z_chosen - z_other) from groups where you chose some frames but not all."""
    X = []
    for grp in groups:
        ids = [fr["id"] for fr in grp]
        good = [i for i, x in enumerate(ids) if x in chosen]
        if not good or len(good) == len(ids):
            continue
        Z = _within([components(fr) for fr in grp])
        for i in good:
            for j in range(len(ids)):
                if j not in good:
                    X.append(Z[i] - Z[j])
    return np.array(X)


def fit_weights(groups, chosen, prior=None, strength=4.0):
    """Pairwise logistic regression (no intercept) on within-group differences, pulled towards the
    prior weights so a handful of choices can't swing it wildly. Returns {component: weight}."""
    X = pairs_from_groups(groups, chosen)
    w0 = np.array([(prior or DEFAULT_WEIGHTS)[c] for c in COMPONENTS])
    if len(X) < 10:
        return dict(zip(COMPONENTS, w0.tolist()))
    w = w0.copy()
    for _ in range(300):                       # gradient ascent on log-likelihood + ridge to prior
        p = 1 / (1 + np.exp(-(X @ w)))
        grad = X.T @ (1 - p) / len(X) - (w - w0) / (strength * len(X) ** 0.5)
        w += 0.5 * grad
    w = np.clip(w, 0.0, None)                  # every component may only help, never invert
    return {c: round(float(v), 3) for c, v in zip(COMPONENTS, w)}


def agreement(groups, chosen, weights=None):
    """Share of groups (2+ frames, some chosen) whose suggested pick you chose."""
    hit = n = 0
    for grp in groups:
        if len(grp) < 2:
            continue
        ids = {fr["id"] for fr in grp}
        if not ids & chosen or ids <= chosen:
            continue
        n += 1
        hit += rank(grp, weights)["pick"] in chosen
    return (hit / n if n else None), n


# --- sessions ---------------------------------------------------------------------------------

def current_weights(con):
    row = con.execute("SELECT value FROM meta WHERE key='cull_weights'").fetchone()
    return {**DEFAULT_WEIGHTS, **json.loads(row[0])} if row else dict(DEFAULT_WEIGHTS)


def scope_photo_ids(con, scope):
    """Photo ids for a cull scope: {"folder_id"} | {"date_from","date_to"} | {"new": true}; plus
    "categories" (default street + travel)."""
    where, args = [], []
    cats = scope.get("categories", ["street", "travel"])
    if cats:
        where.append(f"COALESCE(fo.category, p.category, 'misc') IN ({','.join('?' * len(cats))})")
        args += list(cats)
    if scope.get("folder_id"):
        where.append("p.folder_id = ?"); args.append(int(scope["folder_id"]))
    if scope.get("date_from"):
        where.append("p.capture_time >= ?"); args.append(scope["date_from"])
    if scope.get("date_to"):
        where.append("p.capture_time < date(?, '+1 day')"); args.append(scope["date_to"])
    if scope.get("new"):
        last = con.execute("SELECT MAX(created_at) FROM cull_sessions").fetchone()[0] or "0000"
        where.append("""(SELECT MAX(f2.scanned_at) FROM files f2 WHERE f2.photo_id = p.id) > ?
                        AND NOT EXISTS (SELECT 1 FROM marks m WHERE m.photo_id = p.id AND m.flag IS NOT NULL)""")
        args.append(last.replace(" ", "T"))
    sql = "SELECT p.id FROM photos p JOIN folders fo ON fo.id = p.folder_id"
    return [r[0] for r in con.execute(sql + (" WHERE " + " AND ".join(where) if where else ""), args)]


def start_session(con, scope, label):
    """Start a cull — or resume the unfinished one for the same shoot / dates."""
    if not scope.get("new"):
        key = json.dumps(scope, sort_keys=True)
        for r in con.execute("SELECT id, scope FROM cull_sessions WHERE finished_at IS NULL ORDER BY id DESC"):
            if json.dumps(json.loads(r["scope"]), sort_keys=True) == key:
                return r["id"]
    ids = scope_photo_ids(con, scope)
    if not ids:
        raise ValueError("No photos match — try another shoot, dates or categories.")
    weights = current_weights(con)
    groups = group(load_frames(con, ids))
    cur = con.execute("INSERT INTO cull_sessions (label, scope, weights) VALUES (?, ?, ?)",
                      (label, json.dumps(scope), json.dumps(weights)))
    sid = cur.lastrowid
    for i, g in enumerate(groups):
        r = rank(g, weights)
        con.execute("""INSERT INTO cull_groups (session_id, idx, photo_ids, suggested, order_ids, unsure, reasons)
                       VALUES (?,?,?,?,?,?,?)""", (sid, i, json.dumps([f["id"] for f in g]), r["pick"],
                                                     json.dumps(r["order"]), int(r["unsure"]), json.dumps(r["reasons"])))
    con.commit()
    return sid


def mark(con, session_id, photo_id, action, value=None):
    """pick / reject / unmark / rate (value 0-5; 0 clears). Saved immediately, logged."""
    from . import marks
    if action not in ("pick", "reject", "unmark", "rate"):
        raise ValueError(f"unknown action {action}")
    g = _group_of(con, session_id, photo_id)
    if action == "rate":
        marks.set_mark(con, photo_id, rating=value, session_id=session_id, group_idx=g[0], suggested=g[1])
    else:
        marks.set_mark(con, photo_id, flag=None if action == "unmark" else action,
                       session_id=session_id, group_idx=g[0], suggested=g[1])


def _group_of(con, session_id, photo_id):
    """(group index, suggested pick) of a photo within a session, or (None, None)."""
    for r in con.execute("SELECT idx, photo_ids, suggested FROM cull_groups WHERE session_id=?", (session_id,)):
        if photo_id in json.loads(r["photo_ids"]):
            return r["idx"], r["suggested"]
    return None, None


def session_agreement(con, session_id):
    """Of the multi-frame groups where you picked something, how often was the suggestion one of
    your picks? Returns (rate or None, groups counted)."""
    hit = n = 0
    for r in con.execute("SELECT photo_ids, suggested FROM cull_groups WHERE session_id=?", (session_id,)):
        ids = json.loads(r["photo_ids"])
        if len(ids) < 2:
            continue
        qs = ",".join("?" * len(ids))
        picks = {x[0] for x in con.execute(f"SELECT photo_id FROM marks WHERE flag='pick' AND photo_id IN ({qs})", ids)}
        if not picks:
            continue
        n += 1
        hit += r["suggested"] in picks
    return (hit / n if n else None), n


def delete_session(con, session_id):
    """Remove a cull from the list. Your picks, rejects and stars (marks) are kept, and so is the
    decision history the taste learning uses (just no longer tied to this cull)."""
    con.execute("UPDATE cull_log SET session_id = NULL WHERE session_id = ?", (session_id,))
    con.execute("DELETE FROM cull_groups WHERE session_id = ?", (session_id,))
    con.execute("DELETE FROM cull_sessions WHERE id = ?", (session_id,))
    con.commit()


def session_picks(con, session_id):
    """Picked photo ids in culling order (group order, then shooting order within a group)."""
    out = []
    for r in con.execute("SELECT photo_ids FROM cull_groups WHERE session_id=? ORDER BY idx", (session_id,)):
        ids = json.loads(r[0])
        qs = ",".join("?" * len(ids))
        picked = {x[0] for x in con.execute(f"SELECT photo_id FROM marks WHERE flag='pick' AND photo_id IN ({qs})", ids)}
        out += [i for i in ids if i in picked]
    return out


# --- learning from your culls ----------------------------------------------------------------

def training_groups(con):
    """All groups you've culled (2+ frames) and your current picks among their frames."""
    seen, groups_ids = set(), []
    for r in con.execute("SELECT photo_ids FROM cull_groups"):
        ids = tuple(json.loads(r[0]))
        if len(ids) >= 2 and ids not in seen:
            seen.add(ids)
            groups_ids.append(ids)
    all_ids = sorted({i for g in groups_ids for i in g})
    frames = {f["id"]: f for f in load_frames(con, all_ids)}
    chosen = {r[0] for r in con.execute("SELECT photo_id FROM marks WHERE flag='pick'")}
    groups = [[frames[i] for i in g if i in frames] for g in groups_ids]
    usable = [g for g in groups if len(g) >= 2 and {f["id"] for f in g} & chosen and not {f["id"] for f in g} <= chosen]
    return usable, chosen


def retrain(con):
    """Re-fit the ranking weights from your cull picks (pulled towards the defaults while there are
    few). Stored for the next culls. Returns the info shown in the app."""
    groups, chosen = training_groups(con)
    weights = fit_weights(groups, chosen, prior=DEFAULT_WEIGHTS)
    info = {"groups": len(groups), "pairs": int(len(pairs_from_groups(groups, chosen))),
            "at": datetime.now().isoformat(timespec="seconds")}
    if groups:
        before, _ = agreement(groups, chosen, DEFAULT_WEIGHTS)
        after, _ = agreement(groups, chosen, weights)
        info.update(before=before, after=after)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cull_weights', ?)", (json.dumps(weights),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cull_learning', ?)", (json.dumps(info),))
    con.commit()
    return {"weights": weights, **info}


def learning_info(con):
    row = con.execute("SELECT value FROM meta WHERE key='cull_learning'").fetchone()
    info = json.loads(row[0]) if row else {"groups": 0, "pairs": 0}
    info["weights"] = current_weights(con)
    history = []
    for r in con.execute("SELECT id, label, created_at FROM cull_sessions ORDER BY id"):
        rate, n = session_agreement(con, r["id"])
        if rate is not None:
            history.append({"id": r["id"], "label": r["label"], "date": r["created_at"][:10], "agreement": rate, "groups": n})
    info["history"] = history
    return info
