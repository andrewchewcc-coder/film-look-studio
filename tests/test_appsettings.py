import os
import sqlite3
import time

from filmlook import appsettings, engine


def test_settings_are_clamped(tmp_path, monkeypatch):
    monkeypatch.setattr(appsettings, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(appsettings, "LIBRARY_DIR", tmp_path)
    s = appsettings.save({"export_quality": 300, "cache_limit_gb": 0, "default_look": "tri-x-gold", "bogus": 1})
    assert s["export_quality"] == 100 and s["cache_limit_gb"] == 1 and s["default_look"] == "tri-x-gold"
    assert "bogus" not in appsettings.load()


def test_preview_cache_is_trimmed_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path)
    for k, age in enumerate((300, 200, 100)):                    # c0 opened longest ago
        for ext, size in ((".npy", 1000), (".json", 10)):
            f = tmp_path / f"c{k}{ext}"
            f.write_bytes(b"x" * size)
            t = time.time() - age
            os.utime(f, (t, t))
    freed = appsettings.prune_previews(limit_bytes=2100)
    assert freed == 1010 and not (tmp_path / "c0.npy").exists() and not (tmp_path / "c0.json").exists()
    assert (tmp_path / "c1.npy").exists() and (tmp_path / "c2.npy").exists()


def test_backup_copies_your_work_and_keeps_ten(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; lib.mkdir()
    monkeypatch.setattr(appsettings, "LIBRARY_DIR", lib)
    monkeypatch.setattr(appsettings, "SETTINGS", lib / "settings.json")
    (lib / "publish.json").write_text('{"site": "https://x"}')
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE marks (photo_id INT, flag TEXT)"); con.execute("INSERT INTO marks VALUES (1, 'pick')"); con.commit()
    mount = tmp_path / "drive"; mount.mkdir()
    root = mount / appsettings.BACKUP_FOLDER
    for k in range(12):                                          # older backups already there
        (root / f"2020-01-{k + 1:02d} 1200").mkdir(parents=True)
    r = appsettings.backup(con, mount)
    b = sqlite3.connect(os.path.join(r["path"], "library.db"))
    assert b.execute("SELECT flag FROM marks").fetchone()[0] == "pick"
    assert os.path.exists(os.path.join(r["path"], "publish.json")) and os.path.exists(os.path.join(r["path"], "README.txt"))
    assert len([p for p in root.iterdir() if p.is_dir()]) == 10   # the newest 10 kept
    assert appsettings.load()["last_backup_path"] == r["path"]
