"""Import from a memory card: a pretend card in a temp folder, never a real drive."""
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from filmlook.library import db, importer, scan


def _card(tmp):
    card = tmp / "LEICA Q3"
    d = card / "DCIM" / "100LEICA"
    d.mkdir(parents=True)
    files = {"L1000001.DNG": b"raw1" * 5000, "L1000001.JPG": b"jpg1" * 3000,
             "L1000002.DNG": b"raw2" * 5000, "L1000002.JPG": b"jpg2" * 3000,       # same second as 1
             "L1000003.DNG": b"raw3" * 5000, "L1000004.MOV": b"mov4" * 8000,
             "L1000005.DNG": b"raw5" * 5000, "._L1000005.DNG": b"junk"}             # next day; AppleDouble skipped
    for n, b in files.items():
        (d / n).write_bytes(b)
    return card


TIMES = {"L1000001": ("2026:11:17 21:02:59", "100"), "L1000002": ("2026:11:17 21:02:59", "600"),
         "L1000003": ("2026:11:17 21:03:10", ""), "L1000005": ("2026:11:18 08:00:00", "")}


def _fake_exif(paths, tags=None):
    out = {}
    for p in paths:
        stem = Path(p).stem
        if stem in TIMES:
            out[p] = {"SourceFile": p, "DateTimeOriginal": TIMES[stem][0], "SubSecTimeOriginal": TIMES[stem][1],
                      "Make": "LEICA CAMERA AG", "Model": "LEICA Q3 43"}
    return out


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "read_exif", _fake_exif)
    monkeypatch.setattr(scan.drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    card = _card(tmp_path)
    mov = card / "DCIM" / "100LEICA" / "L1000004.MOV"
    t = time.mktime(datetime(2026, 11, 17, 21, 5, 0).timetuple())
    os.utime(mov, (t, t))                       # a video's time: the camera's file time
    year = tmp_path / "SSD" / "2026 Photography"
    year.mkdir(parents=True)
    con = db.connect(tmp_path / "lib.db")
    scan.add_root(con, year)
    importer._listing.update(key=None, items=None)
    return con, card, year


def _snapshot(folder):
    return {p: (p.stat().st_size, p.stat().st_mtime, p.read_bytes()) for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def _wait():
    for _ in range(200):
        if not importer.job["running"]:
            return importer.job
        time.sleep(0.05)
    raise AssertionError("import didn't finish")


def test_card_found_and_named_your_way(setup, tmp_path):
    con, card, year = setup
    assert importer.cards(tmp_path) == [{"path": str(card), "name": "LEICA Q3", "files": 7}]
    p = importer.plan(con, str(card), shoot="Ex-ACLP gathering")
    names = [[f["dst"] for f in s["files"]] for s in p["shots"]]
    assert names == [["26-11-17-21-02-59.dng", "26-11-17-21-02-59.jpg"],         # a pair keeps one name
                     ["26-11-17-21-02-59-02.dng", "26-11-17-21-02-59-02.jpg"],   # same second: -02, in shooting order
                     ["26-11-17-21-03-10.dng"],
                     ["26-11-17-21-05-00.mov"],                                    # video, from the card's file time
                     ["26-11-18-08-00-00.dng"]]
    folders = [Path(s["folder"]).relative_to(year.parent) for s in p["shots"]]
    assert [str(f) for f in folders] == ["2026 Photography/2026 11 17 Ex-ACLP gathering"] * 4 + \
        ["2026 Photography/2026 11 18 Ex-ACLP gathering"]                         # a folder per day
    assert (p["new"], p["photos"], p["videos"], p["new_files"]) == (5, 4, 1, 7)
    one = importer.plan(con, str(card), shoot="X", per_day=False, name_t="{YYYY}-{MM}-{DD}_{shoot}_{n}")
    assert {Path(s["folder"]).name for s in one["shots"]} == {"2026 11 17 X"}
    assert one["shots"][1]["files"][0]["dst"] == "2026-11-17_X_002.dng"
    keep = importer.plan(con, str(card), name_t="{name}")
    assert keep["shots"][0]["files"][0]["dst"] == "L1000001.dng"


def test_copy_is_checked_never_overwrites_and_leaves_the_card_alone(setup):
    con, card, year = setup
    before = _snapshot(card)
    day = year / "2026 11 17 Shoot"
    day.mkdir()
    (day / "26-11-17-21-03-10.dng").write_bytes(b"someone else's photo")   # name taken by a different file
    p = importer.plan(con, str(card), shoot="Shoot")
    assert p["shots"][2]["files"][0]["dst"] == "26-11-17-21-03-10-02.dng"
    importer.start(con, [s["key"] for s in p["shots"]], source=str(card), shoot="Shoot")
    j = _wait()
    assert (j["phase"], j["copied"], j["errors"]) == ("done", 7, [])
    assert (day / "26-11-17-21-03-10.dng").read_bytes() == b"someone else's photo"    # untouched
    assert (day / "26-11-17-21-02-59-02.jpg").read_bytes() == b"jpg2" * 3000
    assert not list(year.rglob("*" + importer.TMP_SUFFIX))
    assert _snapshot(card) == before                                         # the card is only read
    again = importer.plan(con, str(card), shoot="Shoot")
    assert (again["new"], again["imported"]) == (0, 5)                        # put it in again: nothing to do
    with pytest.raises(ValueError):
        importer.start(con, [s["key"] for s in again["shots"]], source=str(card), shoot="Shoot")
    ids = importer.add_to_library(con, j["folders"])
    assert ids == [con.execute("SELECT id FROM roots").fetchone()[0]]         # already inside "2026 Photography"


def test_a_failed_check_leaves_nothing(setup, monkeypatch):
    con, card, year = setup
    monkeypatch.setattr(scan, "full_hash", lambda p: "different")
    p = importer.plan(con, str(card), shoot="S")
    importer.start(con, [p["shots"][0]["key"]], source=str(card), shoot="S")
    j = _wait()
    assert j["copied"] == 0 and len(j["errors"]) == 2 and "didn't match" in j["errors"][0]
    assert not [f for f in year.rglob("*") if f.is_file()]


def test_new_year_folder_next_to_the_last_one(tmp_path):
    roots = [{"path": str(tmp_path / "2025 Photography")}, {"path": str(tmp_path / "2026 Photography")}]
    assert importer.year_root(roots, 2026) == str(tmp_path / "2026 Photography")
    assert importer.year_root(roots, 2027) == str(tmp_path / "2027 Photography")
    assert importer.year_root([], 2027) is None


def test_drives_without_links_still_get_the_copy(tmp_path, monkeypatch):
    """exFAT (most external SSDs) can't hard-link: the copy must still land, and still never overwrite."""
    from filmlook.library import importer
    src = tmp_path / "card.jpg"
    src.write_bytes(b"photo" * 1000)

    def no_links(a, b):
        raise OSError(45, "Operation not supported")
    monkeypatch.setattr(importer.os, "link", no_links)
    dst = tmp_path / "26-11-24-19-30-05.jpg"
    importer._copy_checked(src, dst)
    assert dst.read_bytes() == src.read_bytes()
    assert [p.name for p in tmp_path.iterdir() if importer.TMP_SUFFIX in p.name] == []
    with pytest.raises(FileExistsError):
        importer._name_it(tmp_path / "x", dst)
    assert dst.read_bytes() == src.read_bytes()
