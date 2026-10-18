"""Photo essays: the image each photo is shown with, its "rhyme" features, essay storage,
starting an essay from a source, and leave-out suggestions for a target length.

Each photo is seen as you'd publish it: its newest graded JPG (ON1 or this app) if there is
one, otherwise the RAW preview with your crop (the library thumbnail). Features are measured
on that image and cached (internal disk), so arranging works with the drive unplugged.
"""
import json
import math
from pathlib import Path, PurePath

import numpy as np
from PIL import Image, ImageOps

from . import LIBRARY_DIR, drives, thumbs, vision

ESSAY_THUMBS = LIBRARY_DIR / "essay_thumbs"
EDGE = 512
NEAR_DUPLICATE = 0.93          # whole-photo similarity above this = "the same picture"

# OKLab hue angles: sRGB red ~29°, orange ~60°, yellow ~110°, green ~142°, cyan ~195°, blue ~264°,
# magenta ~328°.
HUES = [(45, "red"), (80, "orange"), (120, "yellow"), (170, "green"), (230, "teal"), (285, "blue"),
        (320, "purple"), (350, "pink"), (380, "red")]


# --- which image an essay shows -------------------------------------------------------------

def display_source(con, photo_id):
    """(kind, path or None, source_key): the newest graded JPG if any, else the RAW preview
    with the crop (library thumbnail)."""
    mounts = drives.online_mounts(con)
    exp = con.execute("""SELECT drive_id, rel_path, mtime, quick_hash FROM files
                         WHERE photo_id=? AND role='export' AND kind='jpg' AND missing=0
                         ORDER BY mtime DESC LIMIT 1""", (photo_id,)).fetchone()
    if exp:
        mount = mounts.get(exp["drive_id"])
        return "graded", (Path(mount) / exp["rel_path"] if mount else None), f"export:{exp['quick_hash']}"
    t = con.execute("SELECT thumb FROM photos WHERE id=?", (photo_id,)).fetchone()
    return "raw", (thumbs.path(t[0]) if t and t[0] else None), f"thumb:{t[0] if t else ''}"


def _essay_thumb(photo_id, kind, path, key):
    """A 512 px copy of the display image on the internal disk (None if unavailable)."""
    ESSAY_THUMBS.mkdir(parents=True, exist_ok=True)
    import hashlib
    name = f"{photo_id}_{hashlib.sha1(key.encode()).hexdigest()[:10]}.jpg"
    dst = ESSAY_THUMBS / name
    if dst.exists():
        return name
    if path is None or not Path(path).exists():
        return None
    im = Image.open(path)
    if im.format == "JPEG":
        im.draft("RGB", (EDGE * 2, EDGE * 2))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((EDGE, EDGE), Image.LANCZOS)
    im.save(dst, "JPEG", quality=85)
    return name


# --- rhyme features ----------------------------------------------------------------------

def colour_name(lab):
    L, a, b = lab
    chroma = math.hypot(a, b)
    if chroma < 0.04:
        return "black" if L < 0.3 else "white" if L > 0.85 else "grey"
    hue = math.degrees(math.atan2(b, a)) % 360
    return next(n for h, n in HUES if hue <= h)


def _palette(lab_px, k=5, iters=12, seed=0):
    rng = np.random.default_rng(seed)
    centers = lab_px[rng.choice(len(lab_px), k, replace=False)]
    for _ in range(iters):
        d = ((lab_px[:, None, :] - centers[None]) ** 2).sum(-1)
        lab_idx = d.argmin(1)
        for j in range(k):
            sel = lab_px[lab_idx == j]
            if len(sel):
                centers[j] = sel.mean(0)
    counts = np.bincount(lab_idx, minlength=k) / len(lab_px)
    order = np.argsort(-counts)
    return centers[order], counts[order]


def features_of(im, analysis=None, scene_features=None):
    """Shape, colour, light, gesture and pacing descriptors for an upright PIL image."""
    from .. import color
    im = im.convert("RGB")
    small = im.copy()
    small.thumbnail((256, 256), Image.LANCZOS)
    rgb = np.asarray(small, np.float32) / 255
    lin = color.srgb_decode(rgb)
    lab = color.linear_to_oklab(lin)
    L = lab[..., 0]

    # Shape: gradient-orientation histogram (lines and curves) + coarse layout of light masses.
    gy, gx = np.gradient(L)
    mag = np.hypot(gx, gy)
    ang = (np.degrees(np.arctan2(gy, gx)) + 180) % 180
    hist, _ = np.histogram(ang, bins=12, range=(0, 180), weights=mag)
    hist = hist / max(hist.sum(), 1e-9)
    edges = float(mag.mean())
    lay = np.asarray(Image.fromarray((L * 255).clip(0, 255).astype("uint8")).resize((8, 8), Image.BILINEAR), np.float32)
    lay = ((lay - lay.mean()) / max(lay.std(), 1e-6)).ravel()
    # Equal 30-degree sectors (gradient direction ⟂ edge): vertical edges, horizontal edges, and
    # the two diagonals. Each would get 1/6 of the energy in a featureless image.
    vert = float(hist[[11, 0]].sum())
    horiz = float(hist[[5, 6]].sum())
    diag = float(hist[[2, 3, 8, 9]].sum()) / 2

    # Colour: palette in OKLab, a hue profile of the saturated pixels (for colour rhymes), and
    # the accent — the strongest saturated colour covering at least a little of the frame.
    px = lab.reshape(-1, 3)[::3]
    centers, weights = _palette(px.copy())
    ch = np.hypot(lab[..., 1], lab[..., 2]).ravel()
    hue = (np.degrees(np.arctan2(lab[..., 2], lab[..., 1])).ravel()) % 360
    sat = ch > 0.07
    hue_hist, _ = np.histogram(hue[sat], bins=12, range=(0, 360), weights=ch[sat])
    hue_prof = hue_hist / max(len(ch) * 0.05, 1e-9)            # ~1 = a strong colour over 5% of the frame
    accent = None
    if sat.mean() > 0.01:
        b = int(np.argmax(hue_hist))
        in_bin = sat & (hue >= b * 30) & (hue < (b + 1) * 30)
        mean_lab = lab.reshape(-1, 3)[in_bin].mean(0)
        accent = {"name": colour_name(mean_lab), "lab": mean_lab.round(4).tolist(),
                  "share": round(float(in_bin.mean()), 4), "chroma": round(float(ch[in_bin].mean()), 4)}

    # Is the accent colour on the main person (clothing) or in the scene?
    on_person = False
    sf0 = scene_features or {}
    if accent and sf0.get("subject_kind") == "person" and sf0.get("subject_box"):
        x, y, w, h = sf0["subject_box"]
        H, W = L.shape
        box = np.zeros((H, W), bool)
        box[int(y * H):max(int(y * H) + 1, int((y + h) * H)), int(x * W):max(int(x * W) + 1, int((x + w) * W))] = True
        b = int(np.argmax(hue_hist))
        in_bin = (sat & (hue >= b * 30) & (hue < (b + 1) * 30)).reshape(H, W)
        on_person = bool(in_bin.sum() and (in_bin & box).sum() / in_bin.sum() > 0.5)

    # Light.
    light = {"mean": round(float(L.mean()), 4), "contrast": round(float(L.std()), 4),
             "shadows": round(float((L < 0.25).mean()), 4), "highlights": round(float((L > 0.85).mean()), 4),
             "saturation": round(float(np.hypot(lab[..., 1], lab[..., 2]).mean()), 4)}
    sf = scene_features or {}
    if sf.get("subject_kind") == "person" and sf.get("subject_box"):
        x, y, w, h = sf["subject_box"]
        H, W = L.shape
        sub = L[int(y * H):max(int(y * H) + 1, int((y + h) * H)), int(x * W):max(int(x * W) + 1, int((x + w) * W))]
        light["silhouette"] = round(float(L.mean() - sub.mean()), 4)   # >0.2: dark figure on bright ground

    # Gesture: the main person's pose, normalised (position and size removed).
    gesture = None
    if analysis and analysis.get("poses"):
        pose = max(analysis["poses"], key=len)
        if "neck" in pose and len(pose) >= 6:
            nx, ny = pose["neck"][:2]
            ys = [v[1] for v in pose.values()]
            hgt = max(max(ys) - min(ys), 1e-3)
            names = ["nose", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist",
                     "right_wrist", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"]
            vec = [[(pose[n][0] - nx) / hgt, (pose[n][1] - ny) / hgt] if n in pose else None for n in names]
            labels = []
            for side in ("left", "right"):
                w, s = pose.get(f"{side}_wrist"), pose.get(f"{side}_shoulder")
                if w and s and w[1] < s[1]:
                    labels.append("raised arm")
            la, ra = pose.get("left_ankle"), pose.get("right_ankle")
            if la and ra and abs(la[0] - ra[0]) / hgt > 0.25:
                labels.append("stride")
            gesture = {"vec": vec, "labels": sorted(set(labels)), "score": sf.get("gesture", 0.0)}

    people = sf.get("people", 0)
    scale, sure = shot_scale(analysis)       # frames without a big person are finished in ensure_features
    return {"shape": {"hist": hist.round(4).tolist(), "layout": lay.round(3).tolist(), "vertical": round(vert, 3),
                      "horizontal": round(horiz, 3), "diagonal": round(diag, 3), "edges": round(edges, 5)},
            "accent_on_person": on_person,
            "lines": max((("vertical", vert), ("horizontal", horiz), ("diagonal", diag)), key=lambda t: t[1])[0],
            "colour": {"palette": centers.round(4).tolist(), "weights": weights.round(4).tolist(), "accent": accent,
                       "hues": hue_prof.round(4).tolist(), "colourful": round(float(sat.mean()), 4)},
            "light": light, "gesture": gesture,
            "pace": {"people": people, "scale": scale, "scale_sure": sure, "busy": round(edges, 5),
                     "bright": light["mean"]}}


# --- shot size ---------------------------------------------------------------------------------
# close (face fills the frame, or a detail), medium (person cut by the frame, or one object / facade),
# full (the whole person, large), wide (people small, or a scene). People are measured with Apple
# Vision's boxes; frames without a big person ask SigLIP, calibrated against your whole library.

SCALE_VERSION = 2
SCALE_NAMES = {"close": "close-up", "medium": "medium", "full": "full-length", "wide": "wide"}
SCALE_PROMPTS = {
    "close": ["an extreme close-up photograph of a small detail", "a macro close-up of an object filling the frame",
              "a close-up of hands holding something", "a close-up of a texture or pattern"],
    "medium": ["a photograph of a single object or statue", "a photograph of a doorway or window",
               "a photograph of a building facade", "a medium shot of a group of people"],
    "wide": ["a wide photograph of a street scene", "a wide shot of a city with buildings and roads",
             "a landscape photograph with a distant horizon", "a wide view of a large interior space"]}
_scale_cal = {}


def shot_scale(analysis, ai=None):
    """(scale, sure). `ai` = calibrated SigLIP scores {close, medium, wide} (z-scores over the library);
    without it, frames with no big person come back as ("wide", False) to be finished later."""
    a = analysis or {}
    face = max((f["box"][3] for f in a.get("faces", [])), default=0)
    people = [p for p in a.get("people", []) if p.get("conf", 1) >= 0.4]
    main = max(people, key=lambda p: p["box"][3]) if people else None
    h = main["box"][3] if main else 0
    close_ai = ai["close"] - max(ai["medium"], ai["wide"]) if ai else 0
    if face >= 0.16:
        return "close", True
    if face < 0.06 and close_ai > 1.0:
        return "close", True                                   # hands, objects, a detail
    if main and h >= 0.33:
        x, y, w, hh = main["box"]
        def seen(joint):                                      # a pose joint inside the main person's box
            return any(k.endswith(joint) and x - .05 <= v[0] <= x + w + .05 and y - .05 <= v[1] <= y + hh + .05
                       for pose in a.get("poses", []) for k, v in pose.items())
        if seen("ankle") or (h >= 0.5 and seen("knee") and y + hh < 0.96):
            return "full", True                                # whole figure (feet may just be hidden)
        return "medium", True                                  # cut by the frame, a counter, a crowd…
    if ai is None:
        return "wide", False
    crowd = len([p for p in a.get("people", []) if p.get("conf", 1) >= 0.3]) >= 3
    c, m, w = ai["close"], ai["medium"], ai["wide"]
    if c > 0.8 and close_ai >= 0.8 and not crowd:
        return "close", close_ai >= 1.2
    if m > w + 0.3 and not main:
        return "medium", m > w + 0.8
    return "wide", bool(main) or w > max(c, m) + 0.5


def _scale_scores(con, photo_ids):
    """{photo_id: {close, medium, wide}}: SigLIP shot-size scores of the library thumbnail (whole
    photo), as z-scores against the whole library so each prompt set's own bias cancels out."""
    from .search import _matrix
    ids, mat = _matrix(con)
    if not len(ids):
        return {}
    if _scale_cal.get("n") != len(ids):
        tv = {k: vision.embed_text(v) for k, v in SCALE_PROMPTS.items()}
        whole = mat[:, 0]
        cal = {k: ((whole @ t.T).mean(1).mean(), (whole @ t.T).mean(1).std() or 1.0) for k, t in tv.items()}
        _scale_cal.update(n=len(ids), tv=tv, cal=cal)
    lib = {int(i): k for k, i in enumerate(ids)}
    out = {}
    for pid in photo_ids:
        if pid in lib:
            v = mat[lib[pid], 0]
            out[pid] = {k: float(((v @ t.T).mean() - _scale_cal["cal"][k][0]) / _scale_cal["cal"][k][1])
                        for k, t in _scale_cal["tv"].items()}
    return out


def _finish_scale(con, feats):
    """Bring shot sizes up to date (SCALE_VERSION) without re-measuring anything else."""
    todo = [pid for pid, f in feats.items() if (f.get("pace") or {}).get("scale_v") != SCALE_VERSION]
    if not todo:
        return
    ai = _scale_scores(con, todo)
    for pid in todo:
        sc = con.execute("SELECT analysis FROM scene WHERE photo_id=?", (pid,)).fetchone()
        scale, sure = shot_scale(json.loads(sc[0]) if sc and sc[0] else None, ai.get(pid))
        if pid not in ai and not sure:
            scale, sure = "wide", False
        f = feats[pid]
        f["pace"].update(scale=scale, scale_sure=sure, scale_v=SCALE_VERSION)
        stored = {k: v for k, v in f.items() if k not in ("thumb", "image")}
        con.execute("UPDATE essay_features SET features=? WHERE photo_id=?", (json.dumps(stored), pid))
    con.commit()


def ensure_features(con, photo_ids):
    """Measure (or reuse cached) features for these photos. Returns {photo_id: features}."""
    out, todo = {}, []
    for pid in photo_ids:
        kind, path, key = display_source(con, pid)
        row = con.execute("SELECT source, features, thumb FROM essay_features WHERE photo_id=?", (pid,)).fetchone()
        if row and row["source"] == key and row["thumb"] and (ESSAY_THUMBS / row["thumb"]).exists():
            out[pid] = {**json.loads(row["features"]), "thumb": row["thumb"], "image": kind}
            continue
        if row and path is None:          # drive offline: keep what we measured before
            out[pid] = {**json.loads(row["features"]), "thumb": row["thumb"], "image": kind}
            continue
        todo.append((pid, kind, path, key))
    if todo:
        imgs, rows = [], []
        for pid, kind, path, key in todo:
            name = _essay_thumb(pid, kind, path, key) if kind == "graded" else None
            if name is None:              # RAW preview + crop = the library thumbnail
                t = con.execute("SELECT thumb FROM photos WHERE id=?", (pid,)).fetchone()
                if not t or not t[0] or not thumbs.path(t[0]).exists():
                    continue
                name = _essay_thumb(pid, "raw", thumbs.path(t[0]), f"thumb:{t[0]}")
                kind, key = "raw", f"thumb:{t[0]}"
            sc = con.execute("SELECT analysis, features FROM scene WHERE photo_id=?", (pid,)).fetchone()
            im = Image.open(ESSAY_THUMBS / name)
            f = features_of(im, json.loads(sc[0]) if sc else None, json.loads(sc[1]) if sc else None)
            imgs.append(im)
            rows.append((pid, key, name, f, kind))
        vecs = vision.embed_images(imgs) if imgs else []
        for (pid, key, name, f, kind), v in zip(rows, vecs):
            con.execute("INSERT OR REPLACE INTO essay_features (photo_id, source, thumb, features, vector) VALUES (?,?,?,?,?)",
                        (pid, key, name, json.dumps(f), np.asarray(v, np.float16).tobytes()))
            out[pid] = {**f, "thumb": name, "image": kind}
        con.commit()
    _finish_scale(con, out)
    return out


def vectors(con, photo_ids):
    """{photo_id: whole-photo embedding of the display image}."""
    qs = ",".join("?" * len(photo_ids)) or "NULL"
    return {r[0]: np.frombuffer(r[1], np.float16).astype(np.float32)
            for r in con.execute(f"SELECT photo_id, vector FROM essay_features WHERE photo_id IN ({qs})", list(photo_ids))
            if r[1]}


# --- essays ------------------------------------------------------------------------------

def source_photo_ids(con, source):
    """Photo ids for a starting source (see the Essays tab)."""
    t = source.get("type")
    cats = source.get("categories")
    if t == "search":
        from . import search
        res = search.search(con, source.get("q", ""), limit=source.get("limit", 80), categories=cats,
                            folder_id=source.get("folder_id"))
        rows = res["results"]
        strong = [r["id"] for r in rows if r.get("strong", True)]
        return strong if len(strong) >= min(len(rows), source.get("min", 24)) else [r["id"] for r in rows[:source.get("min", 24)]]
    if t == "picks":
        return [r[0] for r in con.execute("""SELECT m.photo_id FROM marks m JOIN photos p ON p.id = m.photo_id
                                             WHERE m.flag='pick' ORDER BY p.capture_time""")]
    if t == "stars":
        return [r[0] for r in con.execute("""SELECT m.photo_id FROM marks m JOIN photos p ON p.id = m.photo_id
                                             WHERE m.rating >= ? ORDER BY p.capture_time""", (int(source.get("min", 3)),))]
    if t == "shortlist":
        return [r[0] for r in con.execute("""SELECT p.id FROM photos p JOIN marks m ON m.photo_id = p.id
                                             WHERE m.flag='pick' ORDER BY p.capture_time""")]
    if t == "photos":                       # chosen in the Library (a selection or a whole view)
        return [int(i) for i in source.get("ids", [])]
    if t in ("folder", "dates"):
        where, args = [], []
        if source.get("folder_id"):
            where.append("p.folder_id = ?"); args.append(int(source["folder_id"]))
        if source.get("date_from"):
            where.append("p.capture_time >= ?"); args.append(source["date_from"])
        if source.get("date_to"):
            where.append("p.capture_time < date(?, '+1 day')"); args.append(source["date_to"])
        if source.get("picked_only"):
            where.append("EXISTS (SELECT 1 FROM marks m WHERE m.photo_id = p.id AND m.flag = 'pick')")
        sql = "SELECT p.id FROM photos p" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.capture_time"
        return [r[0] for r in con.execute(sql, args)]
    raise ValueError(f"unknown source {t}")


def create(con, name, source, target=20, look="leica-m9"):
    ids = source_photo_ids(con, source)
    if not ids:
        raise ValueError("No photos match — try other words, or another source.")
    cur = con.execute("INSERT INTO essays (name, look, target, source) VALUES (?,?,?,?)",
                      (name, look, target, json.dumps(source)))
    eid = cur.lastrowid
    con.executemany("INSERT OR IGNORE INTO essay_items (essay_id, photo_id, pos) VALUES (?,?,?)",
                    [(eid, pid, i) for i, pid in enumerate(ids)])
    con.commit()
    return eid


def items(con, essay_id):
    return [dict(r) for r in con.execute("""SELECT photo_id, pos, locked, title FROM essay_items
                                            WHERE essay_id=? ORDER BY pos""", (essay_id,))]


def set_order(con, essay_id, order, locked=None, titles=None):
    """Replace the order (list of photo ids). Photos not in `order` are removed."""
    keep = set(order)
    con.execute(f"DELETE FROM essay_items WHERE essay_id=? AND photo_id NOT IN ({','.join('?' * len(keep)) or 'NULL'})",
                [essay_id, *keep])
    old = {r["photo_id"]: r for r in items(con, essay_id)}
    for i, pid in enumerate(order):
        lk = int(locked.get(pid, old.get(pid, {}).get("locked", 0))) if locked is not None else int(old.get(pid, {}).get("locked", 0))
        tt = titles.get(pid, old.get(pid, {}).get("title")) if titles is not None else old.get(pid, {}).get("title")
        con.execute("""INSERT INTO essay_items (essay_id, photo_id, pos, locked, title) VALUES (?,?,?,?,?)
                       ON CONFLICT(essay_id, photo_id) DO UPDATE SET pos=excluded.pos, locked=excluded.locked,
                       title=excluded.title""", (essay_id, pid, i, lk, tt))
    con.execute("UPDATE essays SET updated_at=datetime('now') WHERE id=?", (essay_id,))
    con.commit()


def add(con, essay_id, photo_ids):
    n = con.execute("SELECT COALESCE(MAX(pos), -1) FROM essay_items WHERE essay_id=?", (essay_id,)).fetchone()[0]
    con.executemany("INSERT OR IGNORE INTO essay_items (essay_id, photo_id, pos) VALUES (?,?,?)",
                    [(essay_id, pid, n + 1 + i) for i, pid in enumerate(photo_ids)])
    con.commit()


# --- leaving photos out for a target length ---------------------------------------------------

def strength(con, photo_ids):
    """{photo_id: how strong a photo is (taste + appeal + your marks), z-ish units}."""
    from .secondlook import _scores
    d = _scores(con)
    marks = {r[0]: (r[1], r[2]) for r in con.execute("SELECT photo_id, flag, rating FROM marks")}
    out = {}
    for pid in photo_ids:
        i = d["index"].get(pid) if d else None
        s = float(d["total"][i]) if i is not None else 0.0
        flag, rating = marks.get(pid, (None, None))
        s += (0.8 if flag == "pick" else 0) + 0.25 * (rating or 0) - (2.0 if flag == "reject" else 0)
        out[pid] = s
    return out


def leave_out(con, photo_ids, target):
    """Which photos to drop to reach `target`, with a reason each: [(photo_id, reason)]."""
    ids = list(photo_ids)
    if len(ids) <= target:
        return []
    ensure_features(con, ids)
    vec = vectors(con, ids)
    st = strength(con, ids)
    keep, out = list(ids), []
    pos = {pid: i for i, pid in enumerate(ids)}
    while len(keep) > target:
        worst, worst_val, why = None, None, None
        for pid in keep:
            sims = [(float(np.dot(vec[pid], vec[o])), o) for o in keep if o != pid and pid in vec and o in vec]
            top = max(sims) if sims else (0.0, None)
            redundancy = max(0.0, top[0] - 0.75) * 6 + (3.0 if top[0] >= NEAR_DUPLICATE else 0.0)
            # the weaker one of a near-duplicate pair goes; otherwise the weakest / most redundant
            val = st[pid] - redundancy
            if worst_val is None or val < worst_val:
                worst, worst_val = pid, val
                if top[0] >= NEAR_DUPLICATE and top[1] is not None:
                    why = ("near-duplicate", top[1])
                elif redundancy > 0.6 and top[1] is not None:
                    why = ("similar", top[1])
                else:
                    why = ("weaker", None)
        keep.remove(worst)
        kind, other = why
        reason = {"near-duplicate": "almost the same picture as another one you have",
                  "similar": "covers the same ground as a stronger photo",
                  "weaker": "weaker than the rest (your taste, appeal and marks)"}[kind]
        out.append({"photo_id": worst, "reason": reason, "like": other})
    return out


# --- choosing from a brief ("Best of 2026", 20 photos) ------------------------------------------

MONO_SPREAD = 0.016          # colour spread below this = black & white (toned B&W included)
_mono_cache = {}


def is_mono(thumb_name):
    """Is this display image black & white? 95th percentile of colour distance from the photo's own
    average tint, so a warm-toned B&W (Tri-X Gold) still counts as B&W but a dull colour scene doesn't."""
    if not thumb_name:
        return False
    if thumb_name not in _mono_cache:
        from .. import color
        try:
            im = Image.open(ESSAY_THUMBS / thumb_name).convert("RGB")
        except OSError:
            return False
        im.thumbnail((200, 200))
        lab = color.linear_to_oklab(color.srgb_decode(np.asarray(im, np.float32) / 255))
        a, b = lab[..., 1].ravel(), lab[..., 2].ravel()
        _mono_cache[thumb_name] = float(np.percentile(np.hypot(a - a.mean(), b - b.mean()), 95)) < MONO_SPREAD
    return _mono_cache[thumb_name]


def _brief(brief):
    b = dict(brief or {})
    return {"count": max(1, int(b.get("count") or 20)), "theme": (b.get("theme") or "").strip(),
            "colour": b.get("colour") if b.get("colour") in ("mix", "colour", "bw") else "mix",
            "per_shoot": int(b["per_shoot"]) if b.get("per_shoot") else None}


def _pool_info(con, ids, theme):
    """Everything the chooser needs per photo: strength, theme fit, B&W, shoot, scale, vector."""
    f = ensure_features(con, ids)
    ids = [i for i in ids if i in f]
    vec, st = vectors(con, ids), strength(con, ids)
    vals = np.array([st[i] for i in ids]) if ids else np.zeros(0)
    z = (vals - vals.mean()) / max(float(vals.std()), 1e-6) if len(vals) else vals
    fit = {}
    if theme and ids:
        t = vision.embed_text([theme])[0]
        sims = np.array([float(vec[i] @ t) if i in vec else 0.0 for i in ids])
        fz = (sims - sims.mean()) / max(float(sims.std()), 1e-6)
        fit = dict(zip(ids, fz.tolist()))
    rows = {r[0]: r for r in con.execute(
        f"""SELECT p.id, p.folder_id, fo.rel_path, p.graded, m.flag, m.rating FROM photos p
            JOIN folders fo ON fo.id = p.folder_id LEFT JOIN marks m ON m.photo_id = p.id
            WHERE p.id IN ({','.join('?' * len(ids)) or 'NULL'})""", ids)}
    info = {}
    for k, i in enumerate(ids):
        r = rows.get(i)
        info[i] = {"z": float(z[k]), "fit": fit.get(i, 0.0), "mono": is_mono(f[i].get("thumb")),
                   "folder": r["folder_id"] if r else None,
                   "shoot": PurePath(r["rel_path"]).name.strip() if r else "",
                   "graded": bool(r and r["graded"]), "flag": r["flag"] if r else None,
                   "rating": (r["rating"] or 0) if r else 0,
                   "scale": (f[i].get("pace") or {}).get("scale"), "vec": vec.get(i)}
    return info


def _score(x, theme):
    return x["z"] + (0.8 * x["fit"] if theme else 0.0)


def choose(con, pool_ids, brief, keep=()):
    """Pick the best `count` photos from the pool for a brief, with variety. `keep` (locked photos)
    always stay. Returns {"chosen": [{id, reasons}], "bench": [{id, reason, like}], "note"}."""
    b = _brief(brief)
    info = _pool_info(con, list(dict.fromkeys(list(keep) + list(pool_ids))), b["theme"])
    want_mono = {"bw": True, "colour": False}.get(b["colour"])
    chosen = [i for i in keep if i in info]
    per = {}
    for i in chosen:
        per[info[i]["folder"]] = per.get(info[i]["folder"], 0) + 1
    why_not = {}

    def penalty(i):
        x, v = info[i], info[i]["vec"]
        top, like = 0.0, None
        if v is not None:
            for c in chosen:
                if info[c]["vec"] is not None:
                    s = float(v @ info[c]["vec"])
                    if s > top:
                        top, like = s, c
        pen = max(0.0, top - 0.70) * 5 + (4.0 if top >= NEAR_DUPLICATE else 0.0)
        scales = [info[c]["scale"] for c in chosen if info[c]["scale"]]
        if x["scale"] and len(scales) >= 4 and scales.count(x["scale"]) / len(scales) > 0.5:
            pen += 0.3                                  # keep a mix of close-up, medium, full-length and wide
        return pen, top, like

    while len(chosen) < b["count"]:
        best, best_val = None, None
        for i, x in info.items():
            if i in chosen:
                continue
            if want_mono is not None and x["mono"] != want_mono:
                why_not[i] = ("colour", None); continue
            if b["per_shoot"] and per.get(x["folder"], 0) >= b["per_shoot"]:
                why_not[i] = ("shoot", None); continue
            pen, top, like = penalty(i)
            val = _score(x, b["theme"]) - pen
            if top >= NEAR_DUPLICATE:
                why_not[i] = ("dup", like)
            elif pen >= 0.75:
                why_not[i] = ("similar", like)
            else:
                why_not.pop(i, None)
            if best_val is None or val > best_val:
                best, best_val = i, val
        if best is None:
            break
        chosen.append(best)
        per[info[best]["folder"]] = per.get(info[best]["folder"], 0) + 1

    # reasons
    top_in_shoot = {}
    for i, x in info.items():
        if x["folder"] not in top_in_shoot or x["z"] > info[top_in_shoot[x["folder"]]]["z"]:
            top_in_shoot[x["folder"]] = i
    all_graded = all(y["graded"] for y in info.values())
    out = []
    for i in chosen:
        x, r = info[i], []
        if i in keep:
            r.append("you locked it")
        if x["rating"] >= 4:
            r.append(f"one of your {x['rating']}★ photos")
        elif x["flag"] == "pick":
            r.append("one of your picks")
        if b["theme"] and x["fit"] > 0.8:
            r.append(f"fits “{b['theme']}”")
        if top_in_shoot.get(x["folder"]) == i and len([1 for y in info.values() if y["folder"] == x["folder"]]) > 1:
            r.append(f"the strongest from {x['shoot']}")
        if not r and x["z"] > 1.0:
            r.append("among the strongest of your selection")
        if x["graded"] and not all_graded and len(r) < 3:
            r.append("graded")
        out.append({"id": i, "reasons": r or ["a solid photo that adds variety"]})
    number = {c: k + 1 for k, c in enumerate(chosen)}
    bench = []
    rest = sorted((i for i in info if i not in chosen), key=lambda i: -_score(info[i], b["theme"]))
    for i in rest:
        kind, like = why_not.get(i, ("weaker", None))
        reason = {"colour": "a colour photo (this essay is black & white)" if want_mono else "black & white (this essay is colour)",
                  "shoot": f"already {b['per_shoot']} from {info[i]['shoot']}",
                  "dup": "almost the same picture as one already in", "similar": "very like one already in",
                  "weaker": "a little weaker than the ones chosen"}[kind]
        bench.append({"id": i, "reason": reason, "like": like, "like_no": number.get(like)})
    note = None
    if len(chosen) < b["count"]:
        note = (f"Only {len(chosen)} photos fit the brief" +
                (" (black & white only)" if want_mono else " (colour only)" if want_mono is False else "") +
                (f" with at most {b['per_shoot']} per shoot" if b["per_shoot"] else "") + ".")
    return {"chosen": out, "bench": bench, "note": note}


def create_from_brief(con, name, pool_ids, brief, look="leica-m9", origin=None):
    """A new essay from a pool of photos and a brief: the best `count` chosen, in a suggested order.
    Returns (essay_id, note)."""
    pool_ids = list(dict.fromkeys(int(i) for i in pool_ids))
    if not pool_ids:
        raise ValueError("Select some photos first.")
    b = _brief(brief)
    r = choose(con, pool_ids, b)
    if not r["chosen"]:
        raise ValueError(r["note"] or "No photos fit this brief.")
    source = {"type": "photos", "ids": pool_ids, "brief": b, "from": origin,
              "reasons": {str(x["id"]): x["reasons"] for x in r["chosen"]}}
    cur = con.execute("INSERT INTO essays (name, look, target, source) VALUES (?,?,?,?)",
                      (name, look, b["count"], json.dumps(source)))
    eid = cur.lastrowid
    con.executemany("INSERT INTO essay_items (essay_id, photo_id, pos) VALUES (?,?,?)",
                    [(eid, x["id"], k) for k, x in enumerate(r["chosen"])])
    con.commit()
    set_order(con, eid, sequence(con, eid, "flow")["order"])
    return eid, r["note"]


def _essay_source(con, essay_id):
    row = con.execute("SELECT source FROM essays WHERE id=?", (essay_id,)).fetchone()
    return json.loads(row[0] or "{}") if row else {}


def bench(con, essay_id):
    """The photos from the essay's pool that aren't in it, best first, each with why not."""
    src = _essay_source(con, essay_id)
    try:
        pool = source_photo_ids(con, src)
    except ValueError:
        pool = []
    current = [it["photo_id"] for it in items(con, essay_id)]
    rest = [i for i in pool if i not in set(current)]
    if not rest:
        return []
    b = _brief(src.get("brief") or {"count": len(current)})
    info = _pool_info(con, current + rest, b["theme"])
    want_mono = {"bw": True, "colour": False}.get(b["colour"])
    number = {c: k + 1 for k, c in enumerate(current)}
    per = {}
    for c in current:
        if c in info:
            per[info[c]["folder"]] = per.get(info[c]["folder"], 0) + 1
    out = []
    for i in rest:
        if i not in info:
            continue
        x, top, like = info[i], 0.0, None
        if x["vec"] is not None:
            for c in current:
                if c in info and info[c]["vec"] is not None:
                    sv = float(x["vec"] @ info[c]["vec"])
                    if sv > top:
                        top, like = sv, c
        if want_mono is not None and x["mono"] != want_mono:
            reason = "a colour photo (this essay is black & white)" if want_mono else "black & white (this essay is colour)"
        elif top >= NEAR_DUPLICATE:
            reason = "almost the same picture as one already in"
        elif top >= 0.85:
            reason = "very like one already in"
        elif b["per_shoot"] and per.get(x["folder"], 0) >= b["per_shoot"]:
            reason = f"already {b['per_shoot']} from {x['shoot']}"
        else:
            reason, like = "a little weaker than the ones chosen", None
        out.append({"id": i, "reason": reason, "like": like, "like_no": number.get(like),
                    "score": round(_score(x, b["theme"]) - max(0.0, top - 0.70) * 5, 3)})
    out.sort(key=lambda r: -r["score"])
    return out


def replacements(con, essay_id, photo_id, limit=5):
    """Alternatives from the bench for one photo's spot: strong, unlike the rest of the essay, and
    the same kind of shot where possible."""
    src = _essay_source(con, essay_id)
    b = _brief(src.get("brief") or {})
    current = [it["photo_id"] for it in items(con, essay_id)]
    others = [c for c in current if c != photo_id]
    cands = [x for x in bench(con, essay_id) if not x["reason"].startswith(("a colour photo", "black & white"))]
    if not cands:
        return []
    info = _pool_info(con, current + [x["id"] for x in cands], b["theme"])
    me = info.get(photo_id, {})
    out = []
    for x in cands:
        i = x["id"]; y = info.get(i)
        if not y:
            continue
        top = max((float(y["vec"] @ info[o]["vec"]) for o in others
                   if y["vec"] is not None and o in info and info[o]["vec"] is not None), default=0.0)
        if top >= NEAR_DUPLICATE:
            continue
        val = _score(y, b["theme"]) - max(0.0, top - 0.70) * 5
        why = []
        if me.get("scale") and y["scale"] == me.get("scale"):
            val += 0.3; why.append(f"also a {SCALE_NAMES.get(y['scale'], y['scale'])} shot")
        if y["rating"] >= 4:
            why.append(f"one of your {y['rating']}★ photos")
        elif y["flag"] == "pick":
            why.append("one of your picks")
        if b["theme"] and y["fit"] > 0.8:
            why.append(f"fits “{b['theme']}”")
        why.append("strong" if y["z"] > 0.8 else "solid")
        if top < 0.75:
            why.append("unlike the rest of the essay")
        out.append({"id": i, "reasons": why, "score": round(val, 3)})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def rechoose(con, essay_id, brief):
    """Choose again from the essay's pool with a (changed) brief. Locked photos stay where they are."""
    src = _essay_source(con, essay_id)
    pool = source_photo_ids(con, src)
    its = items(con, essay_id)
    locked = [it["photo_id"] for it in its if it["locked"]]
    locked_pos = {it["photo_id"]: it["pos"] for it in its if it["locked"]}
    b = _brief(brief)
    r = choose(con, pool, b, keep=locked)
    chosen = [x["id"] for x in r["chosen"]]
    order = [c for c in chosen if c not in locked]
    for pid, pos in sorted(locked_pos.items(), key=lambda kv: kv[1]):
        order.insert(min(pos, len(order)), pid)
    src.update(brief=b, reasons={str(x["id"]): x["reasons"] for x in r["chosen"]})
    con.execute("UPDATE essays SET source=?, target=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(src), b["count"], essay_id))
    set_order(con, essay_id, order, locked={p: True for p in locked})
    set_order(con, essay_id, sequence(con, essay_id, "flow")["order"])
    return r["note"]


# --- pairs (diptychs) -------------------------------------------------------------------------

AGE_PROMPTS = (["a photo of an elderly person", "an old man or old woman"],
               ["a photo of a young child", "a teenager or young person"])
TOO_ALIKE = 0.90           # pairs this similar are the same scene, not a rhyme
RHYMES = ["shape", "colour", "gesture", "light", "contrast"]


def _corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a - a.mean(), b - b.mean()
    return float(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))


def _shape(fa, fb):
    s = 0.55 * _corr(fa["shape"]["hist"], fb["shape"]["hist"]) + 0.45 * _corr(fa["shape"]["layout"], fb["shape"]["layout"])
    la, lb = fa["lines"], fb["lines"]
    why = f"strong {la} lines in both" if la == lb else "the same composition of light and dark shapes"
    if _corr(fa["shape"]["layout"], fb["shape"]["layout"]) > 0.6:
        why = "the same composition of light and dark shapes" + (f", {la} lines" if la == lb else "")
    return s, why


def _colour(fa, fb):
    ha, hb = np.asarray(fa["colour"]["hues"]), np.asarray(fb["colour"]["hues"])
    overlap = float(np.minimum(ha, hb).sum())
    s = min(overlap, 2.0) / 2.0
    aa, ab = fa["colour"]["accent"], fb["colour"]["accent"]
    if aa and ab and aa["name"] == ab["name"] and min(aa["share"], ab["share"]) > 0.015:
        s += 0.35
        where = lambda f: "on a person" if f.get("accent_on_person") else "in the scene"
        if fa.get("accent_on_person") and fb.get("accent_on_person"):
            return s, f"{aa['name']} clothing in both"
        return s, f"{aa['name']} {where(fa)} echoes {ab['name']} {where(fb)}"
    if aa and ab:
        return s, f"{aa['name']} and {ab['name']} tones"
    return s * 0.5, "the same muted palette"


def _gesture(fa, fb):
    ga, gb = fa.get("gesture"), fb.get("gesture")
    if not ga or not gb:
        return None, None
    d = [np.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(ga["vec"], gb["vec"]) if p and q]
    if len(d) < 4:
        return None, None
    s = float(np.exp(-np.mean(d) / 0.12)) + 0.15 * (ga["score"] + gb["score"])
    shared = sorted(set(ga["labels"]) & set(gb["labels"]))
    if shared:
        s += 0.3
    elif max(ga["score"], gb["score"]) < 0.3:
        s *= 0.4                     # two people just standing: not a gesture rhyme
    return s, ("both " + " and ".join("mid-stride" if x == "stride" else "with a raised arm" for x in shared)
               if shared else "the same body pose")


def _light(fa, fb):
    la, lb = fa["light"], fb["light"]
    keys = ["mean", "contrast", "shadows", "highlights"]
    d = float(np.sqrt(sum((la[k] - lb[k]) ** 2 for k in keys)))
    s = float(np.exp(-d / 0.12))
    if la.get("silhouette", 0) > 0.2 and lb.get("silhouette", 0) > 0.2:
        return s + 0.4, "both silhouettes against bright light"
    tone = lambda l: "dark, low-key" if l["mean"] < 0.35 else "bright, high-key" if l["mean"] > 0.65 else "mid-tone"
    feel = lambda l: "hard, contrasty" if l["contrast"] > 0.22 else "soft"
    if tone(la) == tone(lb) and tone(la) != "mid-tone":
        return s + 0.2, f"the same {feel(la)} {tone(la)} light"
    if feel(la) == feel(lb) == "hard, contrasty":
        return s + 0.1, "the same hard, contrasty light and deep shadows"
    return s * 0.4, f"the same {feel(la)} {tone(la)} light"   # ordinary daylight: weak


def _contrast(fa, fb, age_a=None, age_b=None):
    pa, pb = fa["pace"], fb["pace"]
    opts = []
    if max(pa["people"], pb["people"]) >= 4 and min(pa["people"], pb["people"]) <= 1:
        opts.append((0.9, "crowded ↔ empty"))
    if abs(fa["light"]["mean"] - fb["light"]["mean"]) > 0.3:
        opts.append((0.75 + abs(fa["light"]["mean"] - fb["light"]["mean"]), "light ↔ dark"))
    if {pa["scale"], pb["scale"]} == {"close", "wide"} and pa.get("scale_sure", True) and pb.get("scale_sure", True):
        opts.append((0.8, "close-up ↔ wide"))
    if age_a is not None and age_b is not None and abs(age_a - age_b) > 0.04 and (age_a > 0.02) != (age_b > 0.02):
        opts.append((0.7, "old ↔ young (approximate)"))
    if not opts:
        return None, None
    best = max(opts)
    return best[0] + 0.1 * (len(opts) - 1), best[1]


def _ages(con, ids):
    """Rough old-vs-young score per photo (positive = older) from the image model; only photos
    with people. Approximate by nature."""
    vec = vectors(con, ids)
    old, young = (vision.embed_text(p).mean(0) for p in AGE_PROMPTS)
    return {pid: float(v @ old - v @ young) for pid, v in vec.items()}


def pairs(con, essay_id, limit=30, per_photo=2):
    """Ranked diptychs: [{a, b, rhyme, reason, score, mark}] — near-identical frames excluded;
    your bad pairs left out, good ones first."""
    ids = [it["photo_id"] for it in items(con, essay_id)]
    f = ensure_features(con, ids)
    ids = [i for i in ids if i in f]
    vec = vectors(con, ids)
    times = {r[0]: (r[1], r[2]) for r in con.execute(
        f"SELECT id, folder_id, capture_time FROM photos WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
    with_people = {i for i in ids if f[i]["pace"]["people"] > 0}
    ages = _ages(con, list(with_people)) if len(with_people) >= 2 else {}
    marks = {(r[0], r[1]): r[2] for r in con.execute("SELECT a, b, verdict FROM pair_marks")}
    raw = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in vec and b in vec and float(vec[a] @ vec[b]) >= TOO_ALIKE:
                continue                                   # the same scene, not a rhyme
            ta, tb = times.get(a), times.get(b)
            if ta and tb and ta[0] == tb[0] and ta[1] and tb[1] and abs(_secs(ta[1]) - _secs(tb[1])) < 20:
                continue                                   # frames from the same moment
            fa, fb = f[a], f[b]
            s = {"shape": _shape(fa, fb), "colour": _colour(fa, fb), "gesture": _gesture(fa, fb),
                 "light": _light(fa, fb), "contrast": _contrast(fa, fb, ages.get(a), ages.get(b))}
            raw.append((a, b, s))
    if not raw:
        return []
    # Rank each rhyme type within this essay (0 = weakest pair, 1 = strongest), so a colour echo,
    # a shared pose and matching light compete on equal terms.
    ranks = {}
    for r in RHYMES:
        vals = sorted(s[r][0] for _, _, s in raw if s[r][0] is not None)
        ranks[r] = vals
    def pct(r, v):
        vals = ranks[r]
        if len(vals) < 4:
            return 0.5
        return float(np.searchsorted(vals, v, side="right")) / len(vals)
    out = []
    for a, b, s in raw:
        key = (min(a, b), max(a, b))
        if marks.get(key) == "bad":
            continue
        zs = sorted(((pct(r, s[r][0]), r) for r in RHYMES if s[r][0] is not None), reverse=True)
        top_z, top = zs[0]
        if top == "contrast":
            top_z -= 0.04                                  # rhymes first; contrasts when they're clear
        score = top_z + 0.25 * (zs[1][0] if len(zs) > 1 else 0) + (5 if marks.get(key) == "good" else 0)
        label = {"shape": "Shape rhyme", "colour": "Colour rhyme", "gesture": "Gesture rhyme",
                 "light": "Light rhyme", "contrast": "Contrast"}[top]
        out.append({"a": key[0], "b": key[1], "rhyme": top, "reason": f"{label}: {s[top][1]}",
                    "score": round(float(score), 3), "mark": marks.get(key)})
    out.sort(key=lambda p: -p["score"])
    picked, used = [], {}
    for p in out:                                          # variety: each photo in at most `per_photo` pairs
        if used.get(p["a"], 0) >= per_photo or used.get(p["b"], 0) >= per_photo:
            continue
        picked.append(p)
        used[p["a"]] = used.get(p["a"], 0) + 1
        used[p["b"]] = used.get(p["b"], 0) + 1
        if len(picked) >= limit:
            break
    return picked


def _secs(t):
    from datetime import datetime
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return 0.0


def mark_pair(con, a, b, verdict, rhyme=None):
    """Save a pair as good / bad (None removes the decision)."""
    a, b = min(a, b), max(a, b)
    if verdict is None:
        con.execute("DELETE FROM pair_marks WHERE a=? AND b=?", (a, b))
    elif verdict in ("good", "bad"):
        con.execute("""INSERT INTO pair_marks (a, b, verdict, rhyme) VALUES (?,?,?,?)
                       ON CONFLICT(a, b) DO UPDATE SET verdict=excluded.verdict, rhyme=excluded.rhyme,
                       created_at=datetime('now')""", (a, b, verdict, rhyme))
    else:
        raise ValueError(f"unknown verdict {verdict}")
    con.commit()


# --- running order ------------------------------------------------------------------------------

def _short(rhyme, fa, fb):
    """A short 'this → that' reason for a transition — or None when the shared quality is too
    ordinary to claim (the caller then describes the change of pace instead)."""
    if rhyme == "colour":
        a, b = fa["colour"]["accent"], fb["colour"]["accent"]
        if a and b and (a["name"] == b["name"] or min(a["share"], b["share"]) > 0.03):
            ta = f"{a['name']} {'clothing' if fa.get('accent_on_person') else 'accent'}"
            tb = f"{b['name']} {'clothing' if fb.get('accent_on_person') else 'accent'}"
            return f"{ta} echoes {ta.split()[0]}" if ta == tb else f"{ta} → {tb}"
        return None
    if rhyme == "shape":
        strong = lambda f: max(f["shape"]["vertical"], f["shape"]["horizontal"], f["shape"]["diagonal"]) > 0.3
        if _corr(fa["shape"]["layout"], fb["shape"]["layout"]) > 0.6:
            return "same layout of light and dark"
        if fa["lines"] == fb["lines"] and strong(fa) and strong(fb):
            return f"{fa['lines']} lines continue" if fa["lines"] == fb["lines"] else f"{fa['lines']} lines → {fb['lines']} lines"
        return None
    if rhyme == "gesture":
        la = (fa.get("gesture") or {}).get("labels") or []
        lb = (fb.get("gesture") or {}).get("labels") or []
        if not la or not lb:
            return None
        nice = lambda x: "stride" if x == "stride" else "raised arm"
        return f"{nice(la[0])} answers {nice(lb[0])}" if la[0] == lb[0] else f"{nice(la[0])} → {nice(lb[0])}"
    if rhyme == "light":
        if fa["light"].get("silhouette", 0) > 0.2 and fb["light"].get("silhouette", 0) > 0.2:
            return "silhouette answers silhouette"
        tone = lambda l: "dark" if l["mean"] < 0.35 else "bright" if l["mean"] > 0.65 else "contrasty" if l["contrast"] > 0.22 else None
        ta, tb = tone(fa["light"]), tone(fb["light"])
        return (f"{ta} light continues" if ta == tb else f"{ta} light → {tb} light") if ta and tb else None
    return None


def transition_matrix(con, ids):
    """T[i][j]: how well photo i leads into photo j (0..1, rank within this essay), and why."""
    f = ensure_features(con, ids)
    ids = [i for i in ids if i in f]
    vec = vectors(con, ids)
    n = len(ids)
    raw = {r: np.full((n, n), np.nan) for r in RHYMES[:-1]}     # rhymes only; contrasts are pacing
    dup = np.zeros((n, n), bool)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ids[i], ids[j]
            fa, fb = f[a], f[b]
            for r, (s, _) in {"shape": _shape(fa, fb), "colour": _colour(fa, fb), "gesture": _gesture(fa, fb),
                              "light": _light(fa, fb)}.items():
                if s is not None:
                    raw[r][i, j] = raw[r][j, i] = s
            if a in vec and b in vec and float(vec[a] @ vec[b]) >= TOO_ALIKE:
                dup[i, j] = dup[j, i] = True
    best = np.zeros((n, n))
    why = [[None] * n for _ in range(n)]
    for r, m in raw.items():
        vals = np.sort(m[~np.isnan(m)])
        if len(vals) < 4:
            continue
        pct = np.where(np.isnan(m), 0, np.searchsorted(vals, np.nan_to_num(m), side="right") / len(vals))
        upd = pct > best
        best = np.where(upd, pct, best)
        for i, j in zip(*np.nonzero(upd)):
            why[i][j] = r
    best[dup] = -2.0                                           # never two near-identical photos side by side
    np.fill_diagonal(best, -2.0)
    return ids, f, best, why


def _pace_class(fv):
    p = fv["pace"]
    return (p["scale"], "busy" if p["busy"] > 0.035 or p["people"] >= 4 else "quiet",
            "dark" if p["bright"] < 0.35 else "bright" if p["bright"] > 0.6 else "mid")


def score_order(order, T, pace, strength, idx):
    """Total score of a sequence: transitions + pacing variety + a strong opener and closer."""
    s = 0.0
    pos = [idx[p] for p in order]
    for k in range(len(pos) - 1):
        s += T[pos[k], pos[k + 1]]
    for k in range(len(pos) - 2):                              # three alike in a row is monotonous
        for dim in range(3):
            if pace[pos[k]][dim] == pace[pos[k + 1]][dim] == pace[pos[k + 2]][dim]:
                s -= 0.25
    if pos:
        s += 0.35 * strength[order[0]] + 0.3 * strength[order[-1]]
    return s


def _optimise(order, locked, T, pace, strength, idx, keep=None, iters=4000, seed=0, scorer=None):
    """Swap / move unlocked photos to improve score_order, or `scorer(order)` (simple local search)."""
    rng = np.random.default_rng(seed)
    free = [k for k, p in enumerate(order) if p not in locked]
    if len(free) < 2:
        return order
    extra = keep or (lambda o: 0.0)
    base = scorer or (lambda o: score_order(o, T, pace, strength, idx))
    best = list(order)
    best_s = base(best) + extra(best)
    cur, cur_s = list(best), best_s
    temp = 0.6
    for it in range(iters):
        cand = list(cur)
        i, j = rng.choice(free, 2, replace=False)
        if rng.random() < 0.5:
            cand[i], cand[j] = cand[j], cand[i]
        else:                                                  # reverse a run of free positions
            lo, hi = sorted((i, j))
            run = [k for k in range(lo, hi + 1) if k in free]
            vals = [cand[k] for k in run][::-1]
            for k, v in zip(run, vals):
                cand[k] = v
        cs = base(cand) + extra(cand)
        if cs > cur_s or rng.random() < np.exp((cs - cur_s) / max(temp, 1e-3)):
            cur, cur_s = cand, cs
            if cs > best_s:
                best, best_s = list(cand), cs
        temp *= 0.999
    return best


def _place_name(con, pid):
    r = con.execute("""SELECT p.gps_lat, p.gps_lon, fo.rel_path FROM photos p JOIN folders fo ON fo.id = p.folder_id
                       WHERE p.id=?""", (pid,)).fetchone()
    if r and r[0] is not None:
        return f"gps:{round(r[0], 2)},{round(r[1], 2)}"           # ~1 km cells
    return f"folder:{r[2]}" if r else "?"


def _essay_ctx(con, ids):
    """(ids, features, T, why, strength z, craft context) for a set of essay photos."""
    from . import craft
    ids, f, T, why = transition_matrix(con, ids)
    st = strength(con, ids)
    sv = np.array([st[p] for p in ids]) if ids else np.zeros(1)
    zst = {p: float((st[p] - sv.mean()) / max(sv.std(), 1e-6)) for p in ids}
    ctx = craft.context(con, ids, f, T, zst)
    every = strength(con, [r[0] for r in con.execute("SELECT id FROM photos")])
    ctx["weak"] = [st[p] < float(np.percentile(list(every.values()), 25)) if every else False for p in ids]
    return ids, f, T, why, zst, ctx


def sequence(con, essay_id, style="flow", arc="day_to_night", seed=0):
    """Suggest an order for the essay. Locked photos keep their positions.
    Styles: classic / quiet / juxtaposition (photobook craft, see craft.py), flow, time, mood.
    Returns {order, transitions: [{a, b, reason}], opener, closer, style}."""
    from . import craft
    its = items(con, essay_id)
    ids = [it["photo_id"] for it in its]
    locked_pos = {it["pos"]: it["photo_id"] for it in its if it["locked"]}
    ids, f, T, why, zst, ctx = _essay_ctx(con, ids)
    idx = {p: k for k, p in enumerate(ids)}
    pace = [_pace_class(f[p]) for p in ids]
    scorer = lambda o: craft.score(o, ctx, style if style in craft.STYLES else "flow")
    times = {r[0]: r[1] or "" for r in con.execute(
        f"SELECT id, capture_time FROM photos WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}

    unlocked = [p for p in ids if p not in locked_pos.values()]
    keep = None
    if style == "time":
        places = {p: _place_name(con, p) for p in ids}
        first_seen = {}
        for p in sorted(ids, key=lambda p: times[p]):
            first_seen.setdefault(places[p], times[p])
        unlocked.sort(key=lambda p: (first_seen[places[p]], times[p]))
    elif style == "mood":
        def mood(p):
            fv = f[p]
            if arc == "calm_to_intense":
                g = (fv.get("gesture") or {}).get("score", 0)
                return fv["pace"]["busy"] * 20 + 0.15 * fv["pace"]["people"] + fv["light"]["contrast"] + g
            hour = int(times[p][11:13]) if len(times[p]) >= 13 else 12
            return -fv["light"]["mean"] + 0.04 * hour         # bright daytime first, dark late scenes last
        unlocked.sort(key=mood)
        rank = {p: k for k, p in enumerate(unlocked)}
        n = max(len(unlocked), 1)

        def keep(o):                                           # stay close to the arc while smoothing
            return -0.35 * sum(abs(rank[p] - k * n / len(o)) / n for k, p in enumerate(o) if p in rank) * 4
    else:
        unlocked.sort(key=lambda p: -zst[p])

    order, it_un = [], iter(unlocked)
    for k in range(len(ids)):
        order.append(locked_pos[k] if k in locked_pos else next(it_un))
    if style != "time":
        order = _optimise(order, set(locked_pos.values()), T, pace, zst, idx, keep=keep, seed=seed,
                          iters=3000 if style == "mood" else 6000, scorer=scorer)

    trans = []
    for a, b in zip(order, order[1:]):
        i, j = idx[a], idx[b]
        r = why[i][j]
        reason = (_short(r, f[a], f[b]) if r and T[i, j] > 0.5 else None) or _pace_reason(f[a], f[b])
        trans.append({"a": a, "b": b, "reason": reason, "score": round(float(T[i, j]), 3)})
    return {"style": style, "arc": arc if style == "mood" else None, "order": order, "transitions": trans,
            "chapters": [order[k] for k in craft.chapter_starts(len(order))][1:] if style == "classic" else [],
            "opener": _strong_reason(f[order[0]], zst[order[0]]) if order else None,
            "closer": _strong_reason(f[order[-1]], zst[order[-1]]) if order else None}


def craft_check(con, order):
    """The photobook guidelines as a checklist for this order, plus the hero photos."""
    from . import craft
    ids, f, T, why, zst, ctx = _essay_ctx(con, list(order))
    order = [p for p in order if p in ctx["idx"]]
    items_ = craft.check(order, ctx, f)
    rules = list(dict.fromkeys(x["rule"] for x in items_))
    failing = {x["rule"] for x in items_ if not x["ok"]}
    return {"items": items_, "passed": len(rules) - len(failing), "rules": len(rules),
            "heroes": craft.heroes(ctx, order)}


def craft_fix(con, order, kind, positions=(), locked=(), rule=None):
    """One step towards fixing a checklist item; returns the new order (the page applies it, so Undo works).
    kind: opener / closer (bring the best candidate there) or move (find the best new place for the
    photos at `positions`, 1-based, by moving or swapping them)."""
    from . import craft
    ids, f, T, why, zst, ctx = _essay_ctx(con, list(order))
    order = [p for p in order if p in ctx["idx"]]
    locked = set(locked)
    n = len(order)
    free = [k for k in range(n) if order[k] not in locked]
    score = lambda o: craft.score(o, ctx, "classic")
    def legal(o):                                               # locked photos stay where they are
        return all(o[j] == order[j] for j in range(n) if order[j] in locked)

    if kind in ("opener", "closer"):
        at = 0 if kind == "opener" else n - 1
        if order[at] in locked:
            return order
        def passes(o):                                         # the checklist's own test
            i = ctx["idx"][o[at]]
            if kind == "opener":
                return not (ctx["busy"][i] or ctx["scale"][i] == "close" or ctx["z"][i] < 0)
            return not ((ctx["busy"][i] and ctx["z"][i] < 0.7) or ctx["z"][i] < -0.3)
        best_o, best_s = list(order), (passes(order), score(order))
        for k in free:
            if k == at:
                continue
            swap = list(order)
            swap[at], swap[k] = swap[k], swap[at]
            move = list(order)
            p = move.pop(k)
            move.insert(0, p) if at == 0 else move.append(p)
            for o in (swap, move):
                if legal(o):
                    sc = (passes(o), score(o))                  # a passing opener first, then the whole essay
                    if sc > best_s:
                        best_o, best_s = o, sc
        return best_o
    movers = {order[k - 1] for k in positions if 0 < k <= n and order[k - 1] not in locked}
    if rule:                                                    # aim at the guideline that was clicked
        base_score = score
        score = lambda o: base_score(o) - 3.0 * craft.failures(o, ctx, rule)
    cur = list(order)
    for _ in range(3):                                          # a few steps: long runs need more than one move
        best_o, best_s = cur, score(cur)
        for k in [i for i, p in enumerate(cur) if p in movers]:
            for t in free:
                if t == k:
                    continue
                o = list(cur)
                o[k], o[t] = o[t], o[k]
                cands = [o]
                o2 = list(cur)
                o2.insert(t, o2.pop(k))
                cands.append(o2)
                for c in cands:
                    if legal(c):
                        sc = score(c)
                        if sc > best_s + 1e-9:
                            best_o, best_s = c, sc
        if best_o is cur:
            break
        cur = best_o
    return cur


def _pace_reason(fa, fb):
    pa, pb = _pace_class(fa), _pace_class(fb)
    sure = fa["pace"].get("scale_sure", True) and fb["pace"].get("scale_sure", True)
    if pa[0] != pb[0] and sure:                    # only claim a shot size we're confident about
        return f"change of pace: {SCALE_NAMES.get(pa[0], pa[0])} → {SCALE_NAMES.get(pb[0], pb[0])}"
    if pa[1] != pb[1]:
        return f"{pa[1]} → {pb[1]}"
    if pa[2] != pb[2]:
        return f"{pa[2]} → {pb[2]}".replace("mid", "mid-tone")
    return "a gentle step"


def _strong_reason(fv, z):
    bits = ["one of the strongest photos" if z > 0.7 else "a solid photo"]
    if fv["pace"]["scale"] == "wide" and fv["pace"].get("scale_sure", True):
        bits.append("sets the scene")
    if fv["colour"]["accent"]:
        bits.append(f"{fv['colour']['accent']['name']} accent")
    return ", ".join(bits)


# --- fill the gap ------------------------------------------------------------------------------

def fill_gap(con, essay_id, position, limit=8, pool=60):
    """Photos from your archive that would fit at `position` (between position-1 and position),
    with a reason: they rhyme with the neighbours and give the pacing what it needs."""
    from .search import _matrix
    its = items(con, essay_id)
    order = [it["photo_id"] for it in its]
    prev = order[position - 1] if 0 < position <= len(order) else None
    nxt = order[position] if position < len(order) else None
    neighbours = [p for p in (prev, nxt) if p is not None]
    if not neighbours:
        return []
    ids, mat = _matrix(con)                                   # library embeddings (whole photo = view 0)
    if not len(ids):
        return []
    lib = {int(i): k for k, i in enumerate(ids)}
    nv = [mat[lib[p], 0] for p in neighbours if p in lib]
    if not nv:
        return []
    sim = np.max(np.stack([mat[:, 0] @ v for v in nv]), axis=0)
    inside = set(order)
    rejected = {r[0] for r in con.execute("SELECT photo_id FROM marks WHERE flag='reject'")}
    cand = [int(ids[k]) for k in np.argsort(-sim) if int(ids[k]) not in inside and int(ids[k]) not in rejected
            and sim[k] < TOO_ALIKE][:pool]
    f = ensure_features(con, neighbours + cand)
    st = strength(con, cand)
    need_quiet = all(f[p]["pace"]["busy"] > 0.035 or f[p]["pace"]["people"] >= 4 for p in neighbours)
    scales = {f[p]["pace"]["scale"] for p in neighbours}
    out = []
    for c in cand:
        if c not in f:
            continue
        fc = f[c]
        best, why = 0.0, None
        for p in neighbours:
            for r, (s, w) in {"colour": _colour(f[p], fc), "shape": _shape(f[p], fc), "light": _light(f[p], fc),
                              "gesture": _gesture(f[p], fc)}.items():
                if s is not None and s > best:
                    best, why = s, (r, p)
        score = best + 0.15 * st[c]
        note = []
        busy_c = fc["pace"]["busy"] > 0.035 or fc["pace"]["people"] >= 4
        if need_quiet and not busy_c:
            score += 0.4
            note.append("a quieter frame between busy ones")
        if len(scales) == 1 and fc["pace"]["scale"] not in scales and fc["pace"].get("scale_sure", True):
            score += 0.25
            note.append(f"a {SCALE_NAMES.get(fc['pace']['scale'], fc['pace']['scale'])} shot to vary the pace")
        if why and _short(why[0], f[why[1]], fc):
            r, p = why
            note.insert(0, _short(r, f[p], fc) + (" (with the photo before)" if p == prev else " (with the photo after)"))
        out.append({"photo_id": c, "score": round(float(score), 3), "reason": "; ".join(note) or "fits the mood"})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# --- transitions for an order you arranged, versions -------------------------------------------

def transitions_for(con, order):
    """Reasons between neighbours for any order (e.g. after you dragged photos around)."""
    if len(order) < 2:
        return []
    ids, f, T, why = transition_matrix(con, order)
    idx = {p: k for k, p in enumerate(ids)}
    out = []
    for a, b in zip(order, order[1:]):
        if a not in idx or b not in idx:
            out.append({"a": a, "b": b, "reason": "", "score": 0.0})
            continue
        i, j = idx[a], idx[b]
        if T[i, j] <= -1:
            reason = "⚠ almost the same picture side by side"
        else:
            reason = (_short(why[i][j], f[a], f[b]) if why[i][j] and T[i, j] > 0.5 else None) or _pace_reason(f[a], f[b])
        out.append({"a": a, "b": b, "reason": reason, "score": round(float(T[i, j]), 3)})
    return out


def save_version(con, essay_id, name):
    its = items(con, essay_id)
    cur = con.execute("INSERT INTO essay_versions (essay_id, name, items) VALUES (?,?,?)",
                      (essay_id, name, json.dumps([{"photo_id": i["photo_id"], "locked": i["locked"], "title": i["title"]}
                                                   for i in its])))
    con.commit()
    return cur.lastrowid


def restore_version(con, essay_id, version_id):
    r = con.execute("SELECT items FROM essay_versions WHERE id=? AND essay_id=?", (version_id, essay_id)).fetchone()
    if not r:
        raise ValueError("unknown version")
    its = json.loads(r[0])
    set_order(con, essay_id, [i["photo_id"] for i in its], locked={i["photo_id"]: i["locked"] for i in its},
              titles={i["photo_id"]: i["title"] for i in its})


def delete(con, essay_id):
    for t in ("essay_items", "essay_versions"):
        con.execute(f"DELETE FROM {t} WHERE essay_id=?", (essay_id,))
    con.execute("DELETE FROM essays WHERE id=?", (essay_id,))
    con.commit()
