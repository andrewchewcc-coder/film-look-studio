"""Browse: every photo of a shoot, like a Finder window, with your marks and graded versions.

The app never deletes anything. "Show in Finder" opens a Finder window with the chosen photos'
files selected (the original, its copies and its ON1/XMP sidecars — graded versions are left
out), and you delete them there yourself. After a rescan, photos whose original is gone can be
removed from the library (only the library's record; there's nothing left on disk).
"""
import subprocess
from pathlib import Path, PurePath

from . import drives, search
from .scan import forget_photo

FILTERS = {"all": {}, "pick": {"flag": "pick"}, "reject": {"flag": "reject"}, "unmarked": {"flag": "none"},
           "graded": {"graded": True}, "ungraded": {"graded": False}}


def shoots(con):
    """Every shoot folder with photo, pick, reject and graded counts, newest first."""
    mounts = drives.online_mounts(con)
    rows = con.execute("""
        SELECT fo.id, fo.rel_path, fo.drive_id, d.name AS drive, COUNT(p.id) AS n,
               SUM(m.flag = 'pick') AS picks, SUM(m.flag = 'reject') AS rejects, SUM(p.graded) AS graded,
               SUM(f.missing) AS gone, MIN(p.capture_time) AS first
        FROM photos p JOIN folders fo ON fo.id = p.folder_id JOIN drives d ON d.id = fo.drive_id
        JOIN files f ON f.id = p.primary_file_id LEFT JOIN marks m ON m.photo_id = p.id
        GROUP BY fo.id ORDER BY fo.rel_path DESC""").fetchall()
    return [{"id": r["id"], "name": PurePath(r["rel_path"]).name, "path": r["rel_path"], "drive": r["drive"],
             "online": r["drive_id"] in mounts, "photos": r["n"], "picks": r["picks"] or 0,
             "rejects": r["rejects"] or 0, "graded": r["graded"] or 0, "gone": r["gone"] or 0,
             "year": (r["first"] or "")[:4]} for r in rows]


def photos(con, folder_id, filt="all"):
    """The shoot's photos in shooting order (oldest first, like the camera roll)."""
    res = search.search(con, "", limit=100000, categories=None, folder_id=folder_id, **FILTERS.get(filt, {}))
    out = res["results"][::-1]
    gone = {r[0] for r in con.execute("""SELECT p.id FROM photos p JOIN files f ON f.id = p.primary_file_id
                                         WHERE p.folder_id=? AND f.missing=1""", (folder_id,))}
    for r in out:
        r["deleted"] = r["id"] in gone
    return out


def reveal_files(con, photo_ids):
    """The files to select in Finder for these photos: (paths, offline drive names, graded versions left out)."""
    mounts = drives.online_mounts(con)
    paths, offline, exports = [], set(), 0
    for pid in photo_ids:
        files = con.execute("""SELECT f.drive_id, f.rel_path, f.folder_id, f.role, d.name FROM files f
                               JOIN drives d ON d.id = f.drive_id WHERE f.photo_id=? AND f.missing=0""",
                            (pid,)).fetchall()
        for f in files:
            if f["role"] == "export":
                exports += 1
                continue
            mount = mounts.get(f["drive_id"])
            if not mount:
                offline.add(f["name"])
                continue
            paths.append(str(Path(mount) / f["rel_path"]))
            for s in con.execute("SELECT rel_path FROM sidecars WHERE folder_id=? AND lower(target)=lower(?)",
                                 (f["folder_id"], PurePath(f["rel_path"]).name)):
                paths.append(str(Path(mount) / s["rel_path"]))
    seen, uniq = set(), []
    for p in paths:
        if p not in seen and Path(p).exists():
            seen.add(p); uniq.append(p)
    return uniq, sorted(offline), exports


_SCRIPT = """on run argv
  set fs to {}
  repeat with p in argv
    set end of fs to (POSIX file (contents of p)) as alias
  end repeat
  tell application "Finder"
    activate
    reveal fs
    select fs
  end tell
end run"""


def show_in_finder(paths):
    """Open Finder with these files selected. Falls back to revealing the first one."""
    if not paths:
        return False
    r = subprocess.run(["osascript", "-e", _SCRIPT, *paths], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["open", "-R", paths[0]], check=False)
    return r.returncode == 0


def forget_deleted(con, folder_id):
    """Remove library records of photos whose original is gone from an online drive."""
    mounts = drives.online_mounts(con)
    ids = [r["id"] for r in con.execute("""SELECT p.id, f.drive_id FROM photos p JOIN files f ON f.id = p.primary_file_id
                                           WHERE p.folder_id=? AND f.missing=1""", (folder_id,))
           if r["drive_id"] in mounts]
    for pid in ids:
        forget_photo(con, pid)
    con.commit()
    return len(ids)
