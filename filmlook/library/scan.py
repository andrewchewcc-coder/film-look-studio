"""Scan library folders into the database: metadata, hashes, sidecars, RAW+JPG pairing.

Incremental: a file whose size and modified time haven't changed is skipped. Each
folder is committed as it finishes, so stopping at any point (Ctrl+C, Pause, unplugging
the drive) loses at most the folder in progress, and the next scan picks up there.
Photo folders are only read: nothing is written next to your photos.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePath

from . import drives, pairing, sidecars

MEDIA_KINDS = {".dng": "raw", ".raf": "raw", ".nef": "raw", ".cr3": "raw", ".cr2": "raw",
               ".arw": "raw", ".rw2": "raw", ".orf": "raw",
               ".jpg": "jpg", ".jpeg": "jpg", ".heic": "jpg",
               ".onphoto": "onphoto"}
SKIP_DIRS = {"System Volume Information", ".Spotlight-V100", ".Trashes", ".fseventsd", ".filmlook"}
EXPORT_SUBFOLDERS = {"graded"}  # exports kept in a subfolder still pair with the shoot
EXIF_TAGS = ["DateTimeOriginal", "SubSecTimeOriginal", "CreateDate", "Make", "Model", "LensModel",
             "FocalLength", "FNumber", "ExposureTime", "ISO", "GPSLatitude", "GPSLongitude",
             "ImageSize", "Orientation"]
EXIFTOOL = next((p for p in ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool") if os.path.exists(p)),
                "exiftool")
SAMPLE = 1 << 20  # quick hash reads 3 x 1 MB


class Stopped(Exception):
    pass


def forget_photo(con, pid, keep_decision=False):
    """Delete a photo record and everything hanging off it (never the photo's files)."""
    for table in ("embeddings", "quality", "scene") + (() if keep_decision else ("picks", "marks")):
        con.execute(f"DELETE FROM {table} WHERE photo_id=?", (pid,))
    con.execute("UPDATE files SET photo_id=NULL WHERE photo_id=?", (pid,))
    con.execute("DELETE FROM photos WHERE id=?", (pid,))


def now():
    return datetime.now().isoformat(timespec="seconds")


# --- library folders ----------------------------------------------------------

BLOCKED = {Path("/"), Path("/Users"), Path("/System"), Path("/Library"), Path("/Applications"),
           Path("/private"), Path("/Volumes"), Path.home(), Path.home() / "Library"}


def check_folder(path):
    """Refuse folders that would mean scanning the whole Mac or system files."""
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a folder: {path}")
    if path in {p.resolve() for p in BLOCKED} or path == Path("/System/Volumes/Data") \
            or str(path).startswith(("/System/", "/Library/", str(Path.home() / "Library") + "/")):
        raise ValueError(f"{path} is a system folder or your whole Mac. Choose a photo folder inside it instead.")
    return path


def add_root(con, path):
    path = check_folder(path)
    drive_id, _, rel = drives.ensure_drive(con, path)
    con.execute("INSERT OR IGNORE INTO roots (drive_id, rel_path) VALUES (?, ?)", (drive_id, rel))
    con.commit()
    return drive_id, rel


def remove_root(con, root_id):
    """Take a folder out of the library: forget its photos (unless another library folder covers
    them). Only the library's own records and thumbnails go; the photos themselves are untouched."""
    r = con.execute("SELECT drive_id, rel_path FROM roots WHERE id=?", (root_id,)).fetchone()
    if not r:
        return 0
    con.execute("DELETE FROM roots WHERE id=?", (root_id,))
    others = [o["rel_path"] for o in con.execute("SELECT rel_path FROM roots WHERE drive_id=?", (r["drive_id"],))]

    def covered(rel):
        return any(o == "." or rel == o or rel.startswith(o.rstrip("/") + "/") for o in others)
    prefix = "" if r["rel_path"] == "." else r["rel_path"].rstrip("/") + "/"
    folders = [f for f in con.execute("SELECT id, rel_path FROM folders WHERE drive_id=?", (r["drive_id"],))
               if (f["rel_path"] == r["rel_path"] or f["rel_path"].startswith(prefix)) and not covered(f["rel_path"])]
    removed = 0
    for f in folders:
        pids = [x[0] for x in con.execute("SELECT id FROM photos WHERE folder_id=?", (f["id"],))]
        for pid in pids:
            forget_photo(con, pid)
        con.execute("UPDATE files SET photo_id=NULL WHERE folder_id=?", (f["id"],))
        con.execute("DELETE FROM files WHERE folder_id=?", (f["id"],))
        con.execute("DELETE FROM sidecars WHERE folder_id=?", (f["id"],))
        con.execute("DELETE FROM folders WHERE id=?", (f["id"],))
        removed += len(pids)
    con.commit()
    from . import thumbs
    thumbs.remove_stale({x[0] for x in con.execute("SELECT thumb FROM photos WHERE thumb IS NOT NULL")})
    return removed


def browse(path=None):
    """Folders to pick from: drives and common places, or the subfolders of `path`."""
    home = Path.home()
    places = [{"name": "Pictures", "path": str(home / "Pictures")},
              {"name": "Desktop", "path": str(home / "Desktop")},
              {"name": "Documents", "path": str(home / "Documents")}]
    vols = []
    for v in sorted(Path("/Volumes").iterdir()) if Path("/Volumes").exists() else []:
        try:
            if v.is_symlink() or not v.is_dir() or os.path.realpath(v) == "/":
                continue
            info = drives.locate(v)
            if info[3] == ".":            # a real mounted drive
                vols.append({"name": v.name, "path": str(v)})
        except Exception:
            continue
    out = {"places": vols + places, "path": None, "parent": None, "dirs": []}
    if not path:
        return out
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {p}")
    out["path"] = str(p)
    out["parent"] = str(p.parent) if p.parent != p and str(p.parent) not in ("/Volumes", "/") else None
    try:                                   # photos directly in this folder (for "open in Grade")
        from .. import raw
        out["photos"] = len(raw.list_images(p))
    except OSError:
        out["photos"] = 0
    try:
        out["dirs"] = sorted(({"name": e.name, "path": e.path} for e in os.scandir(p)
                              if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                              and e.name not in SKIP_DIRS), key=lambda d: d["name"].lower())
    except OSError as e:
        raise ValueError(f"can't open {p}: {e}")
    try:
        check_folder(p)
        out["can_add"] = True
    except ValueError as e:
        out["can_add"], out["why_not"] = False, str(e)
    return out


def list_roots(con):
    online = drives.online_mounts(con)
    out = []
    for r in con.execute("""SELECT roots.id, roots.drive_id, roots.rel_path, drives.name
                            FROM roots JOIN drives ON drives.id = roots.drive_id"""):
        mount = online.get(r["drive_id"])
        out.append({"id": r["id"], "drive_id": r["drive_id"], "drive": r["name"], "rel_path": r["rel_path"],
                    "online": mount is not None, "path": str(Path(mount) / r["rel_path"]) if mount else None})
    return out


# --- reading files ------------------------------------------------------------

def walk(root):
    """Yield (dir_path, [media entries], [sidecar entries]) for every folder under root."""
    stack = [Path(root)]
    while stack:
        d = stack.pop()
        media, side = [], []
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):          # ._ AppleDouble files, .DS_Store
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name not in SKIP_DIRS:
                    stack.append(Path(e.path))
                continue
            ext = os.path.splitext(e.name)[1].lower()
            if ext in MEDIA_KINDS:
                media.append(e)
            elif ext in sidecars.SIDECAR_EXTENSIONS:
                side.append(e)
        if media or side:
            yield d, media, side


def quick_hash(path, size):
    h = hashlib.blake2b(str(size).encode(), digest_size=16)
    with open(path, "rb") as f:
        for off in sorted({0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)}):
            f.seek(off)
            h.update(f.read(SAMPLE))
    return h.hexdigest()


def full_hash(path):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_exif(paths):
    """{path: {tag: value}} via one exiftool call for many files."""
    if not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as args:
        args.write("\n".join(str(p) for p in paths))
    try:
        cmd = [EXIFTOOL, "-json", "-n", "-fast", "-charset", "filename=utf8",
               *[f"-{t}" for t in EXIF_TAGS], "-@", args.name]
        out = subprocess.run(cmd, capture_output=True, timeout=600).stdout
        rows = json.loads(out or b"[]")
    except (subprocess.SubprocessError, ValueError):
        rows = []
    finally:
        os.unlink(args.name)
    return {r["SourceFile"]: r for r in rows}


def _capture_time(x):
    s = x.get("DateTimeOriginal") or x.get("CreateDate")
    if not s or not isinstance(s, str) or s.startswith("0000"):
        return None
    try:
        t = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    sub = str(x.get("SubSecTimeOriginal") or "")
    return t.isoformat() + (f".{sub[:3]}" if sub.isdigit() else "")


def _camera(x):
    make, model = (x.get("Make") or "").strip(), str(x.get("Model") or "").strip()
    if make and model and not model.lower().startswith(make.split()[0].lower()):
        return f"{make.split()[0].title() if make.isupper() or make.islower() else make.split()[0]} {model}"
    return model or make or None


def _exif_fields(x):
    w = h = None
    size = str(x.get("ImageSize") or "").replace("x", " ").split()
    if len(size) == 2 and all(s.isdigit() for s in size):
        w, h = int(size[0]), int(size[1])
        if x.get("Orientation") in (5, 6, 7, 8):
            w, h = h, w
    lens = (x.get("LensModel") or "")
    return {"camera": _camera(x), "lens": str(lens).strip() or None,
            "focal_length": x.get("FocalLength"), "aperture": x.get("FNumber"),
            "shutter": x.get("ExposureTime"), "iso": x.get("ISO"),
            "gps_lat": x.get("GPSLatitude"), "gps_lon": x.get("GPSLongitude"),
            "width": w, "height": h, "orientation": x.get("Orientation")}


# --- folders and shoots -----------------------------------------------------------

def _folder_id(con, drive_id, rel):
    con.execute("INSERT OR IGNORE INTO folders (drive_id, rel_path) VALUES (?, ?)", (drive_id, rel))
    return con.execute("SELECT id FROM folders WHERE drive_id=? AND rel_path=?", (drive_id, rel)).fetchone()[0]


def shoot_of(rel):
    """graded/ subfolders belong to their parent shoot."""
    p = PurePath(rel)
    return str(p.parent) if p.name.lower() in EXPORT_SUBFOLDERS else rel


def repair_shoot(con, drive_id, shoot_rel):
    """Re-group a shoot's files into photos and refresh each photo's summary fields."""
    folders = {r["id"]: r["rel_path"] for r in con.execute(
        "SELECT id, rel_path FROM folders WHERE drive_id=? AND (rel_path=? OR rel_path LIKE ?)",
        (drive_id, shoot_rel, shoot_rel.rstrip("/") + "/%"))
        if shoot_of(r["rel_path"]) == shoot_rel}
    if not folders:
        return
    qs = ",".join("?" * len(folders))
    files = [dict(r) for r in con.execute(
        f"SELECT id, rel_path, kind, capture_time, folder_id, exif, photo_id FROM files "
        f"WHERE folder_id IN ({qs}) AND missing=0", list(folders))]
    by_id = {f["id"]: f for f in files}
    side = defaultdict(list)
    for s in con.execute(f"SELECT folder_id, target, rating, flag FROM sidecars WHERE folder_id IN ({qs})",
                         list(folders)):
        side[(s["folder_id"], (s["target"] or "").lower())].append(s)

    keep = set()
    for primary, members in pairing.group(files):
        p = by_id[primary]
        x = json.loads(p["exif"] or "{}")
        ratings = []
        for fid, _ in members:
            f = by_id[fid]
            for s in side.get((f["folder_id"], PurePath(f["rel_path"]).name.lower()), []):
                ratings.append((fid == primary, s["rating"], s["flag"]))
        ratings.sort(key=lambda r: not r[0])           # primary's own sidecar first
        rating = next((r[1] for r in ratings if r[1] is not None), None)
        flag = next((r[2] for r in ratings if r[2] is not None), None)
        graded = int(any(role == "export" for _, role in members))
        con.execute("""INSERT INTO photos (primary_file_id, folder_id, capture_time, camera, lens, focal_length,
                       aperture, shutter, iso, gps_lat, gps_lon, width, height, rating, flag, graded)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(primary_file_id) DO UPDATE SET folder_id=excluded.folder_id,
                       capture_time=excluded.capture_time, camera=excluded.camera, lens=excluded.lens,
                       focal_length=excluded.focal_length, aperture=excluded.aperture, shutter=excluded.shutter,
                       iso=excluded.iso, gps_lat=excluded.gps_lat, gps_lon=excluded.gps_lon, width=excluded.width,
                       height=excluded.height, rating=excluded.rating, flag=excluded.flag, graded=excluded.graded""",
                    (primary, p["folder_id"], p["capture_time"], x.get("camera"), x.get("lens"),
                     x.get("focal_length"), x.get("aperture"), x.get("shutter"), x.get("iso"),
                     x.get("gps_lat"), x.get("gps_lon"), x.get("width"), x.get("height"), rating, flag, graded))
        pid = con.execute("SELECT id FROM photos WHERE primary_file_id=?", (primary,)).fetchone()[0]
        keep.add(pid)
        con.executemany("UPDATE files SET photo_id=?, role=? WHERE id=?",
                        [(pid, role, fid) for fid, role in members])

    # Photos whose primary file is gone or now belongs to another photo.
    stale = [r[0] for r in con.execute(f"SELECT id FROM photos WHERE folder_id IN ({qs})", list(folders))
             if r[0] not in keep]
    for pid in stale:
        if con.execute("SELECT 1 FROM files WHERE photo_id=? AND missing=1 AND role='original'", (pid,)).fetchone():
            continue  # original vanished from an online drive: keep the record (and its thumbnail)
        forget_photo(con, pid)


def link_duplicates(con, mounts):
    """Same file on two drives (or twice on one): confirm with a full hash, then merge
    the later photo into the earlier one and mark its file a 'duplicate'."""
    groups = con.execute("""SELECT quick_hash FROM files WHERE quick_hash IS NOT NULL AND missing=0
                            GROUP BY quick_hash HAVING COUNT(*) > 1""").fetchall()
    merged = 0
    for (qh,) in groups:
        rows = con.execute("SELECT id, drive_id, rel_path, full_hash, photo_id, role FROM files "
                           "WHERE quick_hash=? AND missing=0 ORDER BY id", (qh,)).fetchall()
        by_full = defaultdict(list)
        for r in rows:
            fh = r["full_hash"]
            if not fh and r["drive_id"] in mounts:
                fh = full_hash(Path(mounts[r["drive_id"]]) / r["rel_path"])
                con.execute("UPDATE files SET full_hash=? WHERE id=?", (fh, r["id"]))
            if fh:
                by_full[fh].append(r)
        for same in by_full.values():
            keeper = same[0]
            for r in same[1:]:
                if r["photo_id"] == keeper["photo_id"] or r["role"] not in ("original", None):
                    continue
                old = r["photo_id"]
                con.execute("UPDATE files SET photo_id=?, role='duplicate' WHERE photo_id=? AND id=?",
                            (keeper["photo_id"], old, r["id"]))
                con.execute("UPDATE files SET photo_id=? WHERE photo_id=?", (keeper["photo_id"], old))
                con.execute("UPDATE OR IGNORE picks SET photo_id=? WHERE photo_id=?", (keeper["photo_id"], old))
                forget_photo(con, old)
                merged += 1
    con.commit()
    return merged


# --- the scan -------------------------------------------------------------------

def scan(con, root_ids=None, progress=None, should_stop=lambda: False, batch=40):
    """Scan library roots (all, or the given ids). Returns a summary dict."""
    drives.forget_cache()
    mounts = drives.online_mounts(con)
    stats = {"folders": 0, "seen": 0, "new": 0, "changed": 0, "unchanged": 0, "missing": 0,
             "sidecars": 0, "offline_roots": [], "stopped": False}
    roots = [r for r in list_roots(con) if root_ids is None or r["id"] in root_ids]

    def report(**kw):
        if progress:
            progress({**stats, **kw})

    try:
        for root in roots:
            if not root["online"]:
                stats["offline_roots"].append(f"{root['drive']}/{root['rel_path']}")
                continue
            mount, drive_id = Path(mounts[root["drive_id"]]), root["drive_id"]
            report(phase="listing", current=root["rel_path"])
            tree = list(walk(Path(root["path"])))
            todo = sum(len(m) for _, m, _ in tree)
            done = 0
            seen_rel = set()
            for d, media, side in tree:
                if should_stop():
                    raise Stopped
                rel_dir = str(d.relative_to(mount))
                folder_id = _folder_id(con, drive_id, rel_dir)
                known = {r["rel_path"]: r for r in con.execute(
                    "SELECT id, rel_path, size, mtime, missing FROM files WHERE folder_id=?", (folder_id,))}
                touched = False

                # Sidecars: cheap, re-read when changed.
                known_side = {r["rel_path"]: r for r in con.execute(
                    "SELECT rel_path, size, mtime FROM sidecars WHERE folder_id=?", (folder_id,))}
                for e in side:
                    st = e.stat()
                    rel = str(Path(e.path).relative_to(mount))
                    seen_rel.add(rel)
                    k = known_side.get(rel)
                    if k and k["size"] == st.st_size and abs(k["mtime"] - st.st_mtime) < 1e-3:
                        continue
                    info = sidecars.read(e.path) or {}
                    con.execute("""INSERT INTO sidecars (drive_id, folder_id, rel_path, kind, target, size, mtime,
                                   rating, flag, keywords) VALUES (?,?,?,?,?,?,?,?,?,?)
                                   ON CONFLICT(drive_id, rel_path) DO UPDATE SET target=excluded.target,
                                   size=excluded.size, mtime=excluded.mtime, rating=excluded.rating,
                                   flag=excluded.flag, keywords=excluded.keywords""",
                                (drive_id, folder_id, rel, Path(e.name).suffix.lower()[1:], info.get("target"),
                                 st.st_size, st.st_mtime, info.get("rating"), info.get("flag"),
                                 json.dumps(info.get("keywords") or [])))
                    stats["sidecars"] += 1
                    touched = True

                # Media files: only new or changed ones get read.
                pending = []
                for e in media:
                    st = e.stat()
                    rel = str(Path(e.path).relative_to(mount))
                    seen_rel.add(rel)
                    stats["seen"] += 1
                    k = known.get(rel)
                    if k and not k["missing"] and k["size"] == st.st_size and abs(k["mtime"] - st.st_mtime) < 1e-3:
                        stats["unchanged"] += 1
                        done += 1
                        continue
                    stats["changed" if k else "new"] += 1
                    pending.append((e, st, rel))

                for i in range(0, len(pending), batch):
                    if should_stop():
                        raise Stopped
                    chunk = pending[i:i + batch]
                    report(phase="reading", current=rel_dir, done=done, todo=todo)
                    exif = read_exif([e.path for e, _, _ in chunk])
                    for e, st, rel in chunk:
                        x = exif.get(e.path, {})
                        try:
                            qh = quick_hash(e.path, st.st_size)
                        except OSError:
                            continue
                        con.execute("""INSERT INTO files (drive_id, folder_id, rel_path, kind, size, mtime, quick_hash,
                                       full_hash, capture_time, exif, missing, scanned_at)
                                       VALUES (?,?,?,?,?,?,?,NULL,?,?,0,?)
                                       ON CONFLICT(drive_id, rel_path) DO UPDATE SET folder_id=excluded.folder_id,
                                       kind=excluded.kind, size=excluded.size, mtime=excluded.mtime,
                                       quick_hash=excluded.quick_hash, full_hash=NULL,
                                       capture_time=excluded.capture_time, exif=excluded.exif, missing=0,
                                       scanned_at=excluded.scanned_at""",
                                    (drive_id, folder_id, rel, MEDIA_KINDS[Path(e.name).suffix.lower()],
                                     st.st_size, st.st_mtime, qh, _capture_time(x),
                                     json.dumps(_exif_fields(x)), now()))
                        done += 1
                        touched = True

                # Files that disappeared from this (online) folder.
                gone = [r["id"] for rel, r in known.items() if rel not in seen_rel and not r["missing"]]
                if gone:
                    con.executemany("UPDATE files SET missing=1 WHERE id=?", [(g,) for g in gone])
                    stats["missing"] += len(gone)
                    touched = True
                gone_side = [rel for rel in known_side if rel not in seen_rel]
                if gone_side:
                    con.executemany("DELETE FROM sidecars WHERE drive_id=? AND rel_path=?",
                                    [(drive_id, rel) for rel in gone_side])
                    touched = True

                if touched:
                    repair_shoot(con, drive_id, shoot_of(rel_dir))
                con.commit()
                stats["folders"] += 1
                report(phase="reading", current=rel_dir, done=done, todo=todo)

            # Whole folders that vanished under this root.
            prefix = "" if root["rel_path"] == "." else root["rel_path"].rstrip("/") + "/"
            for f in con.execute("SELECT id, rel_path FROM files WHERE drive_id=? AND missing=0 AND rel_path LIKE ?",
                                 (drive_id, prefix + "%")).fetchall():
                if f["rel_path"] not in seen_rel:
                    con.execute("UPDATE files SET missing=1 WHERE id=?", (f["id"],))
                    stats["missing"] += 1
            con.commit()

        report(phase="duplicates")
        stats["duplicates_merged"] = link_duplicates(con, mounts)
    except Stopped:
        con.commit()
        stats["stopped"] = True
    except KeyboardInterrupt:
        con.commit()
        stats["stopped"] = True
    except OSError as e:  # drive unplugged mid-scan: keep everything done so far
        con.commit()
        stats["stopped"] = True
        stats["error"] = f"drive went away: {e}"
    report(phase="stopped" if stats["stopped"] else "done")
    return stats


def summary(con):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    return {
        "photos": q("SELECT COUNT(*) FROM photos"),
        "graded": q("SELECT COUNT(*) FROM photos WHERE graded=1"),
        "files": q("SELECT COUNT(*) FROM files WHERE missing=0"),
        "by_role": dict(con.execute("SELECT COALESCE(role,'?'), COUNT(*) FROM files WHERE missing=0 GROUP BY role")),
        "by_kind": dict(con.execute("SELECT kind, COUNT(*) FROM files WHERE missing=0 GROUP BY kind")),
        "cameras": dict(con.execute("SELECT COALESCE(camera,'(unknown)'), COUNT(*) FROM photos GROUP BY camera "
                                    "ORDER BY COUNT(*) DESC")),
        "rated": q("SELECT COUNT(*) FROM photos WHERE rating > 0"),
        "with_gps": q("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL"),
        "unpaired_onphoto": q("SELECT COUNT(*) FROM files WHERE kind='onphoto' AND photo_id IS NULL AND missing=0"),
    }
