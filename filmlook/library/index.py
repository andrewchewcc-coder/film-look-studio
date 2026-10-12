"""After a scan: make thumbnails, then embeddings and categories. Incremental and stoppable.

Thumbnails need the photo's drive plugged in; embeddings and categories are computed
from thumbnails, so they work with the drive offline.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from . import drives, quality, thumbs, vision

BATCH = 32
EMBED_BATCH = 8   # photos per GPU batch (x VIEWS images each)


def _missing_thumbs(con):
    return con.execute("""SELECT p.id, f.id AS file_id, f.drive_id, f.rel_path, f.kind, f.quick_hash, p.thumb
                          FROM photos p JOIN files f ON f.id = p.primary_file_id""").fetchall()


def build_thumbnails(con, progress=None, should_stop=lambda: False, workers=4):
    mounts = drives.online_mounts(con)
    todo, offline = [], 0
    for r in _missing_thumbs(con):
        want = thumbs.name_for(r["file_id"], r["quick_hash"])
        if r["thumb"] == want and thumbs.path(want).exists():
            continue
        if r["drive_id"] not in mounts:
            offline += 1
            continue
        todo.append((r["id"], Path(mounts[r["drive_id"]]) / r["rel_path"], r["kind"], want))

    done = failed = 0

    def work(item):
        pid, src, kind, want = item
        try:
            return pid, thumbs.make(src, kind, want)
        except Exception:
            return pid, None

    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(todo), BATCH):
            if should_stop():
                break
            for pid, name in pool.map(work, todo[i:i + BATCH]):
                if name:
                    con.execute("UPDATE photos SET thumb=? WHERE id=?", (name, pid))
                    done += 1
                else:
                    failed += 1
            con.commit()
            if progress:
                progress({"phase": "thumbnails", "done": done + failed, "todo": len(todo)})

    keep = {r[0] for r in con.execute("SELECT thumb FROM photos WHERE thumb IS NOT NULL")}
    removed = thumbs.remove_stale(keep) if not should_stop() else 0
    return {"thumbs_made": done, "thumbs_failed": failed, "thumbs_offline": offline, "thumbs_removed": removed}


def build_embeddings(con, progress=None, should_stop=lambda: False):
    rows = con.execute("""SELECT p.id, p.thumb FROM photos p LEFT JOIN embeddings e ON e.photo_id = p.id
                          WHERE p.thumb IS NOT NULL
                          AND (e.photo_id IS NULL OR e.model != ? OR e.thumb != p.thumb)""",
                       (vision.MODEL_ID,)).fetchall()
    done = 0
    for i in range(0, len(rows), EMBED_BATCH):
        if should_stop():
            break
        chunk = [r for r in rows[i:i + EMBED_BATCH] if thumbs.path(r["thumb"]).exists()]
        if not chunk:
            continue
        images = [Image.open(thumbs.path(r["thumb"])) for r in chunk]
        vecs = vision.embed_photos(images)
        cats = vision.classify(vecs[:, 0])        # categories from the whole-photo view
        for r, v, (cat, margin) in zip(chunk, vecs, cats):
            con.execute("INSERT OR REPLACE INTO embeddings (photo_id, model, thumb, vector) VALUES (?,?,?,?)",
                        (r["id"], vision.MODEL_ID, r["thumb"], v.astype(np.float16).tobytes()))
            con.execute("UPDATE photos SET category=?, category_score=? WHERE id=?", (cat, margin, r["id"]))
        con.commit()
        done += len(chunk)
        if progress:
            progress({"phase": "understanding", "done": done, "todo": len(rows)})
    return {"embedded": done}


def reclassify(con):
    """Re-run categories from stored embeddings (after changing the category prompts)."""
    rows = con.execute("SELECT photo_id, vector FROM embeddings WHERE model=?", (vision.MODEL_ID,)).fetchall()
    if not rows:
        return 0
    vecs = np.stack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(np.float32)
    vecs = vecs.reshape(len(rows), vision.VIEWS, -1)[:, 0]
    for r, (cat, margin) in zip(rows, vision.classify(vecs)):
        con.execute("UPDATE photos SET category=?, category_score=? WHERE id=?", (cat, margin, r[0]))
    con.commit()
    return len(rows)


def build_quality(con, progress=None, should_stop=lambda: False, workers=4):
    """Measure focus / blur / exposure for photos not yet measured (needs the drive)."""
    mounts = drives.online_mounts(con)
    rows = con.execute("""SELECT p.id, f.drive_id, f.rel_path, f.kind, f.quick_hash FROM photos p
                          JOIN files f ON f.id = p.primary_file_id
                          LEFT JOIN quality q ON q.photo_id = p.id
                          WHERE q.photo_id IS NULL OR q.source != f.quick_hash""").fetchall()
    todo = [r for r in rows if r["drive_id"] in mounts]

    def work(r):
        try:
            return r, quality.measure(Path(mounts[r["drive_id"]]) / r["rel_path"], r["kind"])
        except Exception:
            return r, None

    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(todo), BATCH):
            if should_stop():
                break
            for r, m in pool.map(work, todo[i:i + BATCH]):
                if m is None:
                    continue
                con.execute("""INSERT OR REPLACE INTO quality (photo_id, source, sharpness, motion, mean_lum,
                               dark_frac, bright_frac, verdict, burst_id, measured_at)
                               VALUES (?,?,?,?,?,?,?,NULL,NULL,datetime('now'))""",
                            (r["id"], r["quick_hash"], m["sharpness"], m["motion"], m["mean_lum"],
                             m["dark_frac"], m["bright_frac"]))
                done += 1
            con.commit()
            if progress:
                progress({"phase": "checking", "done": done, "todo": len(todo)})
    if not should_stop():
        update_verdicts(con)
        update_bursts(con)
    return {"measured": done, "unmeasured_offline": len(rows) - len(todo)}


def update_verdicts(con):
    """Blur is judged per camera, so verdicts are refreshed across the library."""
    rows = con.execute("""SELECT q.photo_id, COALESCE(p.camera, '?') AS camera, q.sharpness, q.motion,
                          q.mean_lum, q.dark_frac, q.bright_frac FROM quality q JOIN photos p ON p.id = q.photo_id""").fetchall()
    cut = quality.blur_cutoffs([(r["camera"], r["sharpness"]) for r in rows])
    con.executemany("UPDATE quality SET verdict=? WHERE photo_id=?",
                    [(quality.verdict(dict(r), cut.get(r["camera"], cut[None])), r["photo_id"]) for r in rows])
    con.commit()


def update_bursts(con):
    from datetime import datetime
    vecs = {r[0]: np.frombuffer(r[1], dtype=np.float16).astype(np.float32).reshape(vision.VIEWS, -1)[0]
            for r in con.execute("SELECT photo_id, vector FROM embeddings WHERE model=?", (vision.MODEL_ID,))}
    photos = []
    for r in con.execute("SELECT p.id, p.folder_id, p.capture_time FROM photos p JOIN quality q ON q.photo_id = p.id"):
        if r["id"] not in vecs:
            continue
        try:
            t = datetime.fromisoformat(r["capture_time"]) if r["capture_time"] else None
        except ValueError:
            t = None
        photos.append((r["id"], r["folder_id"], t, vecs[r["id"]]))
    bursts = quality.group_bursts(photos)
    con.executemany("UPDATE quality SET burst_id=? WHERE photo_id=?", [(b, pid) for pid, b in bursts.items()])
    con.commit()
    return bursts


def build(con, progress=None, should_stop=lambda: False):
    out = build_thumbnails(con, progress, should_stop)
    if not should_stop():
        out.update(build_embeddings(con, progress, should_stop))
    if not should_stop():
        out.update(build_quality(con, progress, should_stop))
    return out
