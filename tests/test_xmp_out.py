import sqlite3

from filmlook.library import sidecars, xmp_out


def setup(tmp_path, monkeypatch, flag=None, rating=None):
    photo = tmp_path / "25-05-04-18-15-49.dng"
    photo.write_bytes(b"RAW")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE marks (photo_id INTEGER PRIMARY KEY, flag TEXT, rating INTEGER)")
    con.execute("CREATE TABLE xmp_dirty (photo_id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO marks VALUES (1, ?, ?)", (flag, rating))
    monkeypatch.setattr(xmp_out, "_original", lambda con, pid, mounts: (photo, None))
    return con, photo


def test_writes_stars_and_pick_and_keeps_the_photo(tmp_path, monkeypatch):
    con, photo = setup(tmp_path, monkeypatch, "pick", 4)
    assert xmp_out.write(con, 1, mounts={}) == "written"
    x = tmp_path / "25-05-04-18-15-49.xmp"
    text = x.read_text()
    assert 'xmp:Rating="4"' in text and "<rdf:li>Picked</rdf:li>" in text and 'SidecarForExtension="dng"' in text
    assert photo.read_bytes() == b"RAW" and xmp_out.is_ours(x)
    assert xmp_out.write(con, 1, mounts={}) == "unchanged"
    con.execute("UPDATE marks SET flag='reject', rating=NULL")
    assert xmp_out.write(con, 1, mounts={}) == "written" and 'xmp:Rating="-1"' in x.read_text()
    info = sidecars.read(x)                                   # the scan doesn't mistake it for ON1's stars
    assert info["ours"] and info["rating"] is None and info["target"] == "25-05-04-18-15-49"


def test_another_apps_xmp_is_never_touched(tmp_path, monkeypatch):
    con, _ = setup(tmp_path, monkeypatch, "pick", 5)
    x = tmp_path / "25-05-04-18-15-49.xmp"
    x.write_text('<x:xmpmeta x:xmptk="XMP Core 5.6.0"><rdf:Description xmp:Rating="2"/></x:xmpmeta>')
    before = x.read_bytes()
    assert xmp_out.write(con, 1, mounts={}) == "skipped" and x.read_bytes() == before


def test_unmarked_photo_gets_no_file_and_queue_empties(tmp_path, monkeypatch):
    con, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(xmp_out.drives, "online_mounts", lambda con: {})
    xmp_out.mark_dirty(con, [1])
    assert xmp_out.flush(con) == {"nothing": 1} and xmp_out.pending(con) == 0
    assert not list(tmp_path.glob("*.xmp"))
