"""App-wide settings, storage housekeeping and library backups.

Settings live in the library folder (internal disk). Caches are rebuilt on demand, so trimming them
is always safe. Backups copy *your work* (the library database: picks, culls, grades, essays,
crops; your saved looks; the website address) to a "Film Look Studio Backups" folder on a drive you
choose — never touching your photos.
"""
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .library import LIBRARY_DIR

SETTINGS = LIBRARY_DIR / "settings.json"
DEFAULTS = {"default_look": "leica-m9", "export_size": "web", "export_quality": 95, "cache_limit_gb": 4,
            "last_backup": None, "last_backup_path": None}
BACKUP_FOLDER = "Film Look Studio Backups"
KEEP_BACKUPS = 10


def load():
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(changes):
    s = load()
    for k, v in (changes or {}).items():
        if k in ("default_look", "export_size"):
            s[k] = str(v)
        elif k == "export_quality":
            s[k] = max(60, min(100, int(v)))
        elif k == "cache_limit_gb":
            s[k] = max(1, min(200, float(v)))
    _write(s)
    return s


def _write(s):
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2))


# --- storage ----------------------------------------------------------------------------------

def _size(p):
    p = Path(p)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def storage():
    """What the app keeps on the internal disk, in bytes."""
    from . import engine
    old = [f for f in LIBRARY_DIR.glob("*.db") if f.name != "library.db"]
    return {"previews": _size(engine.CACHE_DIR), "views": _size(LIBRARY_DIR / "views"),
            "thumbs": _size(LIBRARY_DIR / "thumbs"), "essay_thumbs": _size(LIBRARY_DIR / "essay_thumbs"),
            "database": sum(_size(LIBRARY_DIR / n) for n in ("library.db", "library.db-wal", "library.db-shm")),
            "old_backups": sum(_size(f) for f in old), "old_backup_names": [f.name for f in old],
            "cache_limit_gb": load()["cache_limit_gb"]}


def prune_previews(limit_bytes=None):
    """Keep the preview cache under its limit: the photos you opened longest ago go first (each is
    simply decoded again, in a second or two, if you open it later). Returns bytes freed."""
    from . import engine
    if limit_bytes is None:
        limit_bytes = load()["cache_limit_gb"] * 1024 ** 3
    d = engine.CACHE_DIR
    if not d.exists():
        return 0
    groups = {}
    for f in d.iterdir():
        if f.is_file():
            groups.setdefault(f.stem, []).append(f)       # x.npy + x.json belong together
    entries = []
    for stem, fs in groups.items():
        st = [f.stat() for f in fs]
        entries.append((max(max(s.st_atime, s.st_mtime) for s in st), sum(s.st_size for s in st), fs))
    total = sum(e[1] for e in entries)
    freed = 0
    for _, size, fs in sorted(entries):                  # oldest use first
        if total - freed <= limit_bytes:
            break
        for f in fs:
            try:
                f.unlink()
            except OSError:
                pass
        freed += size
    engine._mem_cache.clear()
    return freed


def clear_views():
    """Large views for Cull / the Library viewer (made again when needed)."""
    d = LIBRARY_DIR / "views"
    freed = _size(d)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return freed


def remove_old_backups():
    """Safety copies of the database the app made inside its own folder (e.g. before the -IG fix)."""
    freed = 0
    for f in LIBRARY_DIR.glob("*.db"):
        if f.name != "library.db":
            freed += _size(f)
            f.unlink()
    return freed


# --- backups ----------------------------------------------------------------------------------

def backup(con, mount):
    """Copy your work to <mount>/Film Look Studio Backups/<date time>/ and keep the last 10."""
    root = Path(mount) / BACKUP_FOLDER
    dst = root / datetime.now().strftime("%Y-%m-%d %H%M")
    dst.mkdir(parents=True, exist_ok=False)
    out = sqlite3.connect(str(dst / "library.db"))
    con.backup(out)                                     # a consistent copy, even while the app runs
    out.close()
    from . import recipes
    if recipes.USER_LOOKS_DIR.exists():
        shutil.copytree(recipes.USER_LOOKS_DIR, dst / "my looks", ignore=shutil.ignore_patterns(".*"))
    for name in ("publish.json", "settings.json"):
        if (LIBRARY_DIR / name).exists():
            shutil.copy2(LIBRARY_DIR / name, dst / name)
    (dst / "README.txt").write_text(
        "Film Look Studio backup — your work, not your photos.\n\n"
        "library.db: picks, rejects, stars, culls, grades, crops, essays, search index.\n"
        "my looks: the looks you saved.\n"
        "publish.json / settings.json: website address and app settings (no passwords).\n\n"
        f"To restore: quit the app, then copy library.db into\n  {LIBRARY_DIR}\n"
        "(and the looks into the app's looks/user folder). Thumbnails rebuild themselves.\n")
    olds = sorted(p for p in root.iterdir() if p.is_dir())
    for p in olds[:-KEEP_BACKUPS]:                      # only ever our own dated backup folders
        shutil.rmtree(p, ignore_errors=True)
    s = load()
    s.update(last_backup=datetime.now().isoformat(timespec="minutes"), last_backup_path=str(dst))
    _write(s)
    return {"path": str(dst), "size": _size(dst), "kept": min(len(olds), KEEP_BACKUPS)}
