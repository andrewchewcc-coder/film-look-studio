"""Identify drives by volume UUID so photos survive renames, re-plugs and offline periods."""
import os
import plistlib
import subprocess
from datetime import datetime
from pathlib import Path

_uuid_cache = {}


def _mount_point(path):
    p = Path(path).resolve()
    while not os.path.ismount(p):
        p = p.parent
    return p


def _volume_info(mount):
    """(uuid, name) for a mount point, via diskutil."""
    mount = str(mount)
    if mount not in _uuid_cache:
        # The internal disk's photos live on the Data volume; "/" is the sealed system
        # snapshot, whose UUID changes with macOS updates.
        query = "/System/Volumes/Data" if mount == "/" else mount
        out = subprocess.run(["diskutil", "info", "-plist", query], capture_output=True, check=True).stdout
        info = plistlib.loads(out)
        uuid = info.get("VolumeUUID") or info.get("DiskUUID")
        if not uuid:
            raise ValueError(f"can't identify the drive at {mount}")
        name = "Macintosh HD" if mount == "/" else info.get("VolumeName") or Path(mount).name
        _uuid_cache[mount] = (uuid, name)
    return _uuid_cache[mount]


def locate(path):
    """Return (uuid, name, mount, rel_path) for a file or folder path."""
    path = Path(path).expanduser().resolve()
    mount = _mount_point(path)
    uuid, name = _volume_info(mount)
    return uuid, name, str(mount), str(path.relative_to(mount))


def ensure_drive(con, path):
    """Register the drive holding `path`; return (drive_id, mount, rel_path)."""
    uuid, name, mount, rel = locate(path)
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO drives (uuid, name, mount, last_seen) VALUES (?, ?, ?, ?)
                   ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, mount=excluded.mount,
                   last_seen=excluded.last_seen""", (uuid, name, mount, now))
    drive_id = con.execute("SELECT id FROM drives WHERE uuid=?", (uuid,)).fetchone()[0]
    return drive_id, mount, rel


def online_mounts(con):
    """{drive_id: mount} for drives plugged in right now (checked by UUID, not name)."""
    out = {}
    for d in con.execute("SELECT id, uuid, mount FROM drives"):
        mount = d["mount"]
        if not mount or not os.path.isdir(mount):
            continue
        try:
            if _volume_info(mount)[0] == d["uuid"]:
                out[d["id"]] = mount
        except (subprocess.CalledProcessError, ValueError):
            pass
    return out


def forget_cache():
    """Call when drives may have been plugged or unplugged."""
    _uuid_cache.clear()
