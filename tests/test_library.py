import json

import pytest

from filmlook.library import db, pairing, scan, sidecars, thumbs


@pytest.fixture(autouse=True)
def isolated_library(tmp_path, monkeypatch):
    """Never let a test touch the real library's thumbnails."""
    monkeypatch.setattr(thumbs, "THUMB_DIR", tmp_path / "thumbs")


def f(i, name, kind, t=None):
    return {"id": i, "rel_path": f"shoot/{name}", "kind": kind, "capture_time": t}


def as_dict(groups):
    return {p: sorted(m) for p, m in groups}


def test_raw_copy_and_exports_are_one_photo():
    files = [
        f(1, "25-02-12-09-32-26.RAF", "raw", "2025-02-12T09:32:26"),
        f(2, "25-02-12-09-32-26-IG.jpg", "jpg", "2025-02-12T09:32:26"),
        f(3, "25-02-12-09-32-26 copy.RAF", "raw", "2025-02-12T09:32:26"),
        f(4, "25-02-12-09-32-26 copy-IG.jpg", "jpg", "2025-02-12T09:32:26"),
        f(5, "graded/25-02-12-09-32-26_fire-dragon-chrome.jpg", "jpg", "2025-02-12T09:32:26"),
    ]
    g = as_dict(pairing.group(files))
    assert g == {1: [(1, "original"), (2, "export"), (3, "copy"), (4, "export"), (5, "export")]}


def test_similar_names_dont_cross_pair():
    files = [
        f(1, "25-10-17-15-55-06-01.dng", "raw"),
        f(2, "25-10-17-15-55-06-011.dng", "raw"),
        f(3, "25-10-17-15-55-06-011-IG.jpg", "jpg"),
    ]
    g = as_dict(pairing.group(files))
    assert g[1] == [(1, "original")]
    assert g[2] == [(2, "original"), (3, "export")]


def test_name_match_rejected_when_capture_times_differ():
    files = [f(1, "A.dng", "raw", "2025-01-01T10:00:00"), f(2, "A-IG.jpg", "jpg", "2025-06-01T10:00:00")]
    assert as_dict(pairing.group(files)) == {1: [(1, "original")], 2: [(2, "original")]}


def test_renamed_export_pairs_by_exact_capture_time():
    files = [f(1, "25-01-01-10-00-00.dng", "raw", "2025-01-01T10:00:00"),
             f(2, "Chinatown final.jpg", "jpg", "2025-01-01T10:00:00")]
    assert as_dict(pairing.group(files)) == {1: [(1, "original"), (2, "export")]}


def test_phone_photos_stand_alone_and_unmatched_onphoto_is_dropped():
    files = [f(1, "20251026_163041.jpg", "jpg", "2025-10-26T16:30:51"),
             f(2, "20251026_163105.jpg", "jpg", "2025-10-26T16:31:05"),
             f(3, "25-12-25-13-52-56 Pano.onphoto", "onphoto")]
    assert as_dict(pairing.group(files)) == {1: [(1, "original")], 2: [(2, "original")]}


def test_sidecars_read_on1_and_xmp(tmp_path):
    on1 = tmp_path / "x.on1"
    on1.write_text(json.dumps({"photos": {"g": {"name": "x.dng", "metadata": {"Rating": 3, "UserFlag": 4}}}}))
    xmp = tmp_path / "y.xmp"
    xmp.write_text('<x xmp:Rating="2" photoshop:SidecarForExtension="raf"/>'
                   '<dc:subject><rdf:Bag><rdf:li>rain</rdf:li></rdf:Bag></dc:subject>')
    before = (on1.stat().st_mtime, xmp.stat().st_mtime)
    assert sidecars.read(on1) == {"target": "x.dng", "rating": 3, "flag": 4, "keywords": []}
    assert sidecars.read(xmp) == {"target": "y.raf", "rating": 2, "flag": None, "keywords": ["rain"]}
    assert (on1.stat().st_mtime, xmp.stat().st_mtime) == before


def test_scan_is_incremental_and_marks_missing(tmp_path, monkeypatch):
    photos = tmp_path / "photos" / "2025 01 01 Test"
    photos.mkdir(parents=True)
    (photos / "A.dng").write_bytes(b"raw-a" * 1000)
    (photos / "A-IG.jpg").write_bytes(b"jpg-a" * 1000)
    (photos / "B.dng").write_bytes(b"raw-b" * 1000)
    (photos / "A.on1").write_text(json.dumps({"photos": {"g": {"name": "A.dng", "metadata": {"Rating": 4}}}}))
    monkeypatch.setattr(scan, "read_exif", lambda paths: {})
    monkeypatch.setattr(scan.drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    con = db.connect(tmp_path / "lib.db")
    scan.add_root(con, tmp_path / "photos")

    first = scan.scan(con)
    assert (first["new"], first["unchanged"]) == (3, 0)
    s = scan.summary(con)
    assert (s["photos"], s["graded"], s["rated"]) == (2, 1, 1)

    again = scan.scan(con)
    assert (again["new"], again["changed"], again["unchanged"]) == (0, 0, 3)

    (photos / "B.dng").unlink()
    scan.scan(con)
    assert con.execute("SELECT missing FROM files WHERE rel_path LIKE '%B.dng'").fetchone()[0] == 1


def test_filters_and_folder_override(tmp_path, monkeypatch):
    from filmlook.library import search
    monkeypatch.setattr(scan.drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    monkeypatch.setattr(scan, "read_exif", lambda paths: {})
    shoot = tmp_path / "photos" / "2025 02 12 Wendy"
    shoot.mkdir(parents=True)
    for n in ("A.dng", "B.dng"):
        (shoot / n).write_bytes(n.encode() * 500)
    con = db.connect(tmp_path / "lib.db")
    scan.add_root(con, tmp_path / "photos")
    scan.scan(con)
    con.execute("UPDATE photos SET category='street'")
    con.commit()
    assert search.search(con)["total"] == 2                      # street + travel by default
    assert search.search(con, categories=["portrait"])["total"] == 0
    folder = search.folder_categories(con)[0]
    assert folder["suggested"] == "street" and folder["override"] is None
    search.set_folder_category(con, folder["id"], "portrait")
    assert search.search(con)["total"] == 0                      # hidden from the default view
    assert search.search(con, categories=["portrait"])["total"] == 2
    assert search.search(con, categories=None)["total"] == 2     # "everything"


def test_pause_halfway_then_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(scan.drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    monkeypatch.setattr(scan, "read_exif", lambda paths: {})
    for shoot in ("2025 01 01 A", "2025 01 02 B", "2025 01 03 C"):
        d = tmp_path / "photos" / shoot
        d.mkdir(parents=True)
        (d / "X.dng").write_bytes(shoot.encode() * 400)
    con = db.connect(tmp_path / "lib.db")
    scan.add_root(con, tmp_path / "photos")
    calls = []

    def stop_after_first_folder():
        calls.append(1)
        return len(calls) > 2
    first = scan.scan(con, should_stop=stop_after_first_folder)
    assert first["stopped"] and 0 < scan.summary(con)["photos"] < 3
    second = scan.scan(con)
    assert not second["stopped"] and scan.summary(con)["photos"] == 3
    assert second["unchanged"] >= 1        # the folder done before pausing isn't re-read


def test_blocked_folders_and_remove_root(tmp_path, monkeypatch):
    import pytest
    from pathlib import Path
    for bad in ("/", str(Path.home()), "/System", "/Volumes"):
        with pytest.raises(ValueError):
            scan.check_folder(bad)
    monkeypatch.setattr(scan.drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    monkeypatch.setattr(scan, "read_exif", lambda paths: {})
    d = tmp_path / "photos" / "2025 01 01 A"
    d.mkdir(parents=True)
    (d / "X.dng").write_bytes(b"x" * 999)
    con = db.connect(tmp_path / "lib.db")
    scan.add_root(con, tmp_path / "photos")
    scan.scan(con)
    assert scan.summary(con)["photos"] == 1
    root_id = scan.list_roots(con)[0]["id"]
    assert scan.remove_root(con, root_id) == 1
    assert scan.summary(con)["photos"] == 0 and not scan.list_roots(con)
    assert (d / "X.dng").exists()          # the photo itself is untouched
