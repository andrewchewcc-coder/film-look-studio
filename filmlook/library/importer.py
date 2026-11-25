"""Import from a memory card: copy new photos and videos into dated shoot folders, renamed.

The one place the app writes to a photo drive, and only like this:
  * the card is only read: nothing on it is changed, moved or deleted;
  * only NEW files are created in the destination; an existing file is never overwritten, changed
    or deleted (a name that's taken gets the next number);
  * every file is copied to a temporary name, read back and compared with the card's bytes, and
    only then given its real name. A failed copy leaves nothing behind but a message.

Names come from the moment each photo was taken (from the photo's own data). RAW + JPG pairs
keep one name with their own endings; shots in the same second get -02, -03... in shooting order.
"""
import hashlib
import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import scan

PHOTO_KINDS = {**{e: k for e, k in scan.MEDIA_KINDS.items() if k in ("raw", "jpg")}, ".hif": "jpg", ".png": "jpg",
               ".tif": "jpg", ".tiff": "jpg"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".mxf"}
NAME_PRESETS = [
    {"id": "{YY}-{MM}-{DD}-{hh}-{mm}-{ss}", "label": "26-11-17-21-02-59 (date and time)"},
    {"id": "{YYYY}{MM}{DD}_{hh}{mm}{ss}", "label": "20261117_210259"},
    {"id": "{YYYY}-{MM}-{DD}_{shoot}_{n}", "label": "2026-11-17_Shoot name_001"},
    {"id": "{YY}-{MM}-{DD}-{hh}-{mm}-{ss}_{name}", "label": "26-11-17-21-02-59_L1001234 (with the camera's name)"},
    {"id": "{name}", "label": "Keep the camera's names (L1001234)"},
]
FOLDER_PRESETS = [
    {"id": "{YYYY} {MM} {DD} {shoot}", "label": "2026 11 17 Shoot name"},
    {"id": "{YYYY}-{MM}-{DD} {shoot}", "label": "2026-11-17 Shoot name"},
    {"id": "{shoot}", "label": "Shoot name only"},
]
TOKENS = ("YYYY", "YY", "MM", "DD", "hh", "mm", "ss", "name", "shoot", "camera", "n")
COPY_CHUNK = 8 << 20
TMP_SUFFIX = ".wen-part"

_lock = threading.Lock()
_listing = {"key": None, "items": None}
job = {"running": False, "phase": "idle", "done": 0, "todo": 0, "bytes_done": 0, "bytes_todo": 0, "current": "",
       "copied": 0, "skipped": 0, "errors": [], "folders": [], "source": None, "finished_at": None}
_stop = threading.Event()


# --- cards -----------------------------------------------------------------------------------------

def cards(volumes=Path("/Volumes")):
    """Memory cards (any plugged-in drive with a DCIM folder), with a rough count."""
    out = []
    try:
        vols = sorted(volumes.iterdir())
    except OSError:
        return out
    for v in vols:
        try:
            if v.is_symlink() or not (v / "DCIM").is_dir():
                continue
            n = 0
            for _, files in _walk(v / "DCIM"):
                n += len(files)
            out.append({"path": str(v), "name": v.name, "files": n})
        except OSError:
            continue
    return out


def _kind(name):
    ext = os.path.splitext(name)[1].lower()
    return "video" if ext in VIDEO_EXTS else PHOTO_KINDS.get(ext)


def _walk(root):
    stack = [Path(root)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        files = []
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name not in scan.SKIP_DIRS:
                    stack.append(Path(e.path))
            elif _kind(e.name):
                files.append(e)
        if files:
            yield d, files


# --- reading the card ------------------------------------------------------------------------------

def _when(x, path, kind):
    """When it was taken, as a datetime (with milliseconds when the camera records them)."""
    for tag in ("DateTimeOriginal", "CreationDate") + (() if kind == "video" else ("CreateDate",)):
        s = x.get(tag)
        iso = scan._time_of(s, x.get("SubSecTimeOriginal") if tag == "DateTimeOriginal" else None) if s else None
        if iso:
            return datetime.fromisoformat(iso)
    # Videos' CreateDate is often in UTC; a card's file time is the camera's own clock (local time).
    return datetime.fromtimestamp(os.stat(path).st_mtime)


def read_source(source):
    """Every photo and video on the card (or in a folder): [{path, rel, size, kind, when, camera}]."""
    src = Path(source)
    root = src / "DCIM" if (src / "DCIM").is_dir() else src
    entries = [(d, e) for d, files in _walk(root) for e in files]
    key = (str(root), tuple(sorted((e.path, e.stat().st_size, e.stat().st_mtime) for _, e in entries)))
    with _lock:
        if _listing["key"] == key:
            return _listing["items"]
    exif = _read_exif([e.path for _, e in entries])
    items = []
    for d, e in entries:
        x = exif.get(e.path, {})
        kind = _kind(e.name)
        items.append({"path": e.path, "rel": os.path.relpath(e.path, src), "dir": str(d), "name": e.name,
                      "stem": os.path.splitext(e.name)[0], "ext": os.path.splitext(e.name)[1].lower(),
                      "size": e.stat().st_size, "kind": kind, "when": _when(x, e.path, kind),
                      "camera": scan._camera(x)})
    with _lock:
        _listing.update(key=key, items=items)
    return items


def _read_exif(paths):
    return scan.read_exif(paths, ["DateTimeOriginal", "SubSecTimeOriginal", "CreateDate", "CreationDate", "Make", "Model"])


def shots(items):
    """Files of one shot together (RAW + JPG of the same frame: same folder and name), in shooting order."""
    groups = {}
    for it in items:
        groups.setdefault((it["dir"], it["stem"].upper()), []).append(it)
    out = []
    for (d, stem), files in groups.items():
        files.sort(key=lambda f: (f["kind"] != "raw", f["kind"] == "video", f["ext"]))
        main = files[0]
        out.append({"key": f"{d}/{stem}", "files": files, "when": main["when"], "stem": main["stem"],
                    "camera": main["camera"], "kind": "video" if all(f["kind"] == "video" for f in files) else "photo",
                    "size": sum(f["size"] for f in files)})
    out.sort(key=lambda s: (s["when"], s["stem"]))
    return out


# --- names -----------------------------------------------------------------------------------------

def _safe(s):
    s = re.sub(r'[/:\\\x00-\x1f]', "-", str(s)).strip().strip(".")
    return re.sub(r"\s+", " ", s)


def render(template, shot, shoot="", n=1):
    """'{YY}-{MM}-{DD}-{hh}-{mm}-{ss}' -> '26-11-17-21-02-59' for this shot."""
    t = shot["when"]
    values = {"YYYY": f"{t.year:04d}", "YY": f"{t.year % 100:02d}", "MM": f"{t.month:02d}", "DD": f"{t.day:02d}",
              "hh": f"{t.hour:02d}", "mm": f"{t.minute:02d}", "ss": f"{t.second:02d}", "name": shot["stem"],
              "shoot": shoot.strip(), "camera": (shot.get("camera") or "").strip(), "n": f"{n:03d}"}
    out = re.sub(r"\{(" + "|".join(TOKENS) + r")\}", lambda m: values[m.group(1)], template or "")
    out = re.sub(r"\s+", " ", out).strip(" -_")
    return _safe(out)


def year_root(roots, year):
    """Where a year's shoots go: the library folder named for that year ('2026 Photography'), or a new
    one like it next to the latest year's ('2027 Photography')."""
    online = [r for r in roots if r.get("path")]
    for r in online:
        if Path(r["path"]).name.startswith(str(year)):
            return r["path"]
    dated = sorted((r for r in online if re.match(r"(19|20)\d\d\b", Path(r["path"]).name)),
                   key=lambda r: Path(r["path"]).name)
    if dated:
        last = Path(dated[-1]["path"])
        return str(last.parent / (str(year) + last.name[4:]))
    return None


def plan(con, source, into="auto", shoot="", folder_t=FOLDER_PRESETS[0]["id"], name_t=NAME_PRESETS[0]["id"],
         per_day=True, lower_ext=True):
    """What would be copied where: [{key, when, kind, files: [{name, size, dst}], status, folder}] and totals.
    status: new / imported (already here or in your library)."""
    items = read_source(source)
    all_shots = shots(items)
    roots = scan.list_roots(con)
    src_root = Path(source).resolve()
    folders_for_day = {}
    taken = {}                                   # folder -> set of lower-case names used by this import
    in_lib = _library_matches(con, items)
    out, n = [], 0
    first_day = all_shots[0]["when"].date() if all_shots else None
    for s in all_shots:
        n += 1
        day = s["when"].date() if per_day else first_day
        if day not in folders_for_day:
            dayshot = {**s, "when": datetime.combine(day, datetime.min.time())}
            parent = year_root(roots, day.year) if into == "auto" else into
            sub = render(folder_t, dayshot, shoot) or day.strftime("%Y %m %d")
            folders_for_day[day] = str(Path(parent) / sub) if parent else None
        folder = folders_for_day[day]
        base = render(name_t, s, shoot, n) or _safe(s["stem"])
        entry = {"key": s["key"], "when": s["when"].isoformat(timespec="milliseconds"), "kind": s["kind"],
                 "camera": s["camera"], "folder": folder, "size": s["size"], "status": "new",
                 "files": [{"name": f["name"], "rel": f["rel"], "size": f["size"], "kind": f["kind"]} for f in s["files"]]}
        if folder is None:
            entry["status"] = "no_folder"
            out.append(entry)
            continue
        if Path(folder).resolve().is_relative_to(src_root):
            raise ValueError("The destination is on the card itself. Choose a folder on your photo drive.")
        used = taken.setdefault(folder, set())
        exts = [(f["ext"].lower() if lower_ext else f["ext"]) for f in s["files"]]
        k, name = 1, base
        while True:
            names = [name + e for e in exts]
            here = [Path(folder) / nm for nm in names]
            same = all(p.exists() and p.stat().st_size == f["size"] for p, f in zip(here, s["files"]))
            if same:                             # imported before, under this very name
                entry["status"] = "imported"
                entry["where"] = "already in " + Path(folder).name
                break
            if not any(nm.lower() in used for nm in names) and not any(p.exists() for p in here):
                break
            k += 1
            name = f"{base}-{k:02d}"
        if entry["status"] == "new" and all(f["path"] in in_lib for f in s["files"]):
            entry["status"] = "imported"
            entry["where"] = "already in your library (" + in_lib[s["files"][0]["path"]] + ")"
        for f, e, fe in zip(s["files"], exts, entry["files"]):
            fe["dst"] = name + e
        if entry["status"] == "new":
            used.update(nm.lower() for nm in (name + e for e in exts))
        out.append(entry)
    new = [e for e in out if e["status"] == "new"]
    return {"source": str(source), "shots": out, "folders": sorted({e["folder"] for e in new if e["folder"]}),
            "new": len(new), "new_files": sum(len(e["files"]) for e in new), "new_bytes": sum(e["size"] for e in new),
            "imported": sum(1 for e in out if e["status"] == "imported"),
            "photos": sum(1 for e in out if e["kind"] == "photo"), "videos": sum(1 for e in out if e["kind"] == "video")}


def _library_matches(con, items):
    """{card path: shoot folder} for card files already in the library (same size, same content sample)."""
    sizes = {it["size"] for it in items}
    if not sizes:
        return {}
    known = {}
    rows = con.execute(f"""SELECT f.size, f.quick_hash, fo.rel_path FROM files f JOIN folders fo ON fo.id = f.folder_id
                           WHERE f.missing = 0 AND f.size IN ({','.join('?' * len(sizes))})""", list(sizes)).fetchall()
    for r in rows:
        known.setdefault(r[0], {})[r[1]] = Path(r[2]).name
    out = {}
    for it in items:
        if it["size"] in known:
            try:
                h = scan.quick_hash(it["path"], it["size"])
            except OSError:
                continue
            if h in known[it["size"]]:
                out[it["path"]] = known[it["size"]][h]
    return out


# --- copying ---------------------------------------------------------------------------------------

def _copy_checked(src, dst):
    """Copy to a temporary name, compare the copy with the card byte for byte (by hash), then name it.
    Never overwrites: raises FileExistsError if `dst` appeared meanwhile."""
    dst = Path(dst)
    tmp = dst.with_name("." + dst.name + TMP_SUFFIX)
    h_src = hashlib.blake2b(digest_size=20)
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            while chunk := fi.read(COPY_CHUNK):
                h_src.update(chunk)
                fo.write(chunk)
                if _stop.is_set():
                    raise InterruptedError("stopped")
            fo.flush()
            os.fsync(fo.fileno())
        if scan.full_hash(tmp) != h_src.hexdigest():
            raise IOError("the copy didn't match the card (try another card reader or cable)")
        shutil.copystat(src, tmp)                # keep the camera's file date
        if dst.exists():
            raise FileExistsError(f"{dst.name} already exists")
        _name_it(tmp, dst)
    except BaseException:
        if tmp.exists():
            tmp.unlink()                         # only ever our own unfinished copy
        raise


def _name_it(tmp, dst):
    """Give the checked copy its real name without ever replacing a file that's there."""
    try:
        os.link(tmp, dst)                        # fails instead of replacing if the name was just taken
        tmp.unlink()
        return
    except FileExistsError:
        raise
    except OSError:
        pass                                     # exFAT / FAT drives (most external SSDs, cards) have no links
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # claim the name; FileExistsError if taken
    os.close(fd)
    try:
        os.replace(tmp, dst)                     # onto our own empty placeholder only
    except BaseException:
        os.unlink(dst)                           # remove our empty placeholder
        raise


def start(con, keys, after=None, **settings):
    """Copy the chosen shots (keys from plan) in the background."""
    p = plan(con, **settings)
    chosen = [s for s in p["shots"] if s["key"] in set(keys) and s["status"] == "new"]
    if not chosen:
        raise ValueError("Nothing new to import.")
    need = sum(s["size"] for s in chosen)
    for folder in {s["folder"] for s in chosen}:
        base = Path(folder)
        while not base.exists():
            base = base.parent
        free = shutil.disk_usage(base).free
        if free < need + (200 << 20):
            raise ValueError(f"Not enough space on {base}: {need / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free.")
    with _lock:
        if job["running"]:
            raise ValueError("An import is already running.")
        _stop.clear()
        job.update(running=True, phase="copying", done=0, todo=sum(len(s["files"]) for s in chosen), bytes_done=0,
                   bytes_todo=need, current="", copied=0, skipped=0, errors=[], folders=[], source=p["source"],
                   finished_at=None)
    threading.Thread(target=_run, args=(p["source"], chosen, after), daemon=True).start()
    return job


def stop():
    _stop.set()
    return job


def _run(source, chosen, after=None):
    folders = set()
    try:
        for s in chosen:
            for f in s["files"]:
                if _stop.is_set():
                    break
                job["current"] = f["name"]
                folder = Path(s["folder"])
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    folders.add(str(folder))
                    _copy_checked(Path(source) / f["rel"], folder / f["dst"])
                    job["copied"] += 1
                except FileExistsError as e:
                    job["skipped"] += 1
                    job["errors"].append(f"{f['name']}: {e}, so it was left alone")
                except InterruptedError:
                    break
                except Exception as e:
                    job["errors"].append(f"{f['name']}: {e}")
                job["done"] += 1
                job["bytes_done"] += f["size"]
        job["phase"] = "stopped" if _stop.is_set() else "done"
    except Exception as e:
        traceback.print_exc()
        job.update(phase="error")
        job["errors"].append(str(e))
    finally:
        job.update(current="", folders=sorted(folders))
        if after and folders:
            try:
                job["phase_after"] = after(sorted(folders))    # e.g. add the shoot to the Library and scan it
            except Exception as e:
                traceback.print_exc()
                job["errors"].append(f"Copied, but adding it to the Library failed: {e}")
        job.update(running=False, finished_at=time.time())


def add_to_library(con, folders):
    """Make sure the new shoot folders are in the library (adding their year folder if needed); root ids to scan."""
    roots = scan.list_roots(con)
    ids = set()
    for folder in folders:
        f = Path(folder)
        hit = [r for r in roots if r["path"] and (f == Path(r["path"]) or Path(r["path"]) in f.parents)]
        if hit:
            ids.add(max(hit, key=lambda r: len(r["path"]))["id"])
            continue
        scan.add_root(con, f.parent)
        roots = scan.list_roots(con)
        ids.update(r["id"] for r in roots if r["path"] and Path(r["path"]) == f.parent)
    return sorted(ids)


def eject(path):
    """Eject the card (after an import), like dragging it to the Bin."""
    import subprocess
    if not (Path(path) / "DCIM").is_dir():
        raise ValueError("That isn't a memory card.")
    r = subprocess.run(["diskutil", "eject", str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise ValueError((r.stderr or r.stdout).strip() or "Couldn't eject the card. Is a file on it still open?")
    return True
