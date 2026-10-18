import numpy as np
from PIL import Image, ImageDraw

from filmlook.library import essay


def test_features_see_lines_colour_and_light():
    stripes = Image.new("RGB", (400, 300), (200, 200, 200))
    d = ImageDraw.Draw(stripes)
    for x in range(0, 400, 40):
        d.rectangle((x, 0, x + 18, 300), fill=(30, 30, 30))
    assert essay.features_of(stripes)["lines"] == "vertical"
    red = Image.new("RGB", (400, 300), (120, 120, 120))
    ImageDraw.Draw(red).ellipse((150, 100, 250, 200), fill=(220, 30, 30))
    f = essay.features_of(red)
    assert f["colour"]["accent"]["name"] == "red"
    dark = essay.features_of(Image.new("RGB", (400, 300), (15, 15, 20)))
    assert dark["light"]["mean"] < 0.25 and dark["colour"]["accent"] is None


def test_gesture_labels_from_pose():
    analysis = {"poses": [{"neck": [0.5, 0.3, 1], "nose": [0.5, 0.25, 1], "left_shoulder": [0.45, 0.32, 1],
                           "right_shoulder": [0.55, 0.32, 1], "left_wrist": [0.4, 0.15, 1], "left_ankle": [0.35, 0.9, 1],
                           "right_ankle": [0.65, 0.9, 1], "left_hip": [0.47, 0.55, 1]}]}
    f = essay.features_of(Image.new("RGB", (300, 300), (128, 128, 128)), analysis, {"gesture": 0.6})
    assert set(f["gesture"]["labels"]) == {"raised arm", "stride"}


def _feat(colour=None, lines="vertical", mean=0.5, contrast=0.15, people=1, scale="medium", pose=None, gesture=0.0):
    hues = [0.0] * 12
    acc = None
    if colour:
        hues[0] = 1.2
        acc = {"name": colour, "lab": [0.6, 0.2, 0.1], "share": 0.05, "chroma": 0.2}
    hist = [0.3 if (i in (11, 0)) == (lines == "vertical") else 0.05 for i in range(12)]
    return {"shape": {"hist": hist, "layout": [1, -1] * 32, "vertical": 0.5, "horizontal": 0.1, "diagonal": 0.2, "edges": 0.02},
            "lines": lines, "colour": {"hues": hues, "accent": acc, "palette": [], "weights": [], "colourful": 0.1},
            "accent_on_person": False, "light": {"mean": mean, "contrast": contrast, "shadows": 0.1, "highlights": 0.05},
            "gesture": {"vec": pose, "labels": [], "score": gesture} if pose else None,
            "pace": {"people": people, "scale": scale, "busy": 0.02, "bright": mean}}


def test_pair_rhymes_and_contrasts():
    red_a, red_b = _feat("red"), _feat("red")
    s, why = essay._colour(red_a, red_b)
    assert "red" in why and s > essay._colour(_feat(), _feat())[0]
    s, why = essay._contrast(_feat(people=8, scale="wide"), _feat(people=0, scale="close"))
    assert why in ("crowded ↔ empty", "close ↔ wide")
    s, why = essay._light(_feat(mean=0.2, contrast=0.3), _feat(mean=0.25, contrast=0.3))
    assert "low-key" in why
    pose = [[0.1 * i, 0.05 * i] for i in range(13)]
    s_same, _ = essay._gesture(_feat(pose=pose, gesture=0.6), _feat(pose=pose, gesture=0.6))
    s_diff, _ = essay._gesture(_feat(pose=pose, gesture=0.6), _feat(pose=[[0.3, -0.2]] * 13, gesture=0.6))
    assert s_same > s_diff


def test_pair_marks_saved(tmp_path):
    from filmlook.library import db
    con = db.connect(tmp_path / "lib.db")
    essay.mark_pair(con, 9, 4, "good", "colour")
    assert tuple(con.execute("SELECT a, b, verdict FROM pair_marks").fetchone()) == (4, 9, "good")
    essay.mark_pair(con, 4, 9, "bad")
    assert con.execute("SELECT verdict FROM pair_marks").fetchone()[0] == "bad"
    essay.mark_pair(con, 4, 9, None)
    assert con.execute("SELECT COUNT(*) FROM pair_marks").fetchone()[0] == 0


def test_optimise_keeps_locks_avoids_duplicates_and_improves():
    rng = np.random.default_rng(1)
    n = 12
    T = rng.random((n, n)) * 0.5
    T = (T + T.T) / 2
    T[0, 1] = T[1, 0] = -2.0                       # 0 and 1 are near-identical
    np.fill_diagonal(T, -2.0)
    ids = list(range(100, 100 + n))
    idx = {p: k for k, p in enumerate(ids)}
    pace = [("medium", "quiet", "mid")] * n
    strength = {p: 0.0 for p in ids}
    start = [ids[0], ids[1]] + ids[2:]
    locked = {ids[5]}
    start[5], start[ids.index(ids[5])] = ids[5], start[5]
    before = essay.score_order(start, T, pace, strength, idx)
    out = essay._optimise(start, locked, T, pace, strength, idx, iters=3000)
    assert sorted(out) == sorted(ids) and out[5] == ids[5]
    assert all(abs(out.index(ids[0]) - out.index(ids[1])) > 1 for _ in [0])
    assert essay.score_order(out, T, pace, strength, idx) >= before


def test_pace_reason_describes_the_change():
    a, b = _feat(scale="wide"), _feat(scale="close")
    assert essay._pace_reason(a, b) == "change of pace: wide → close-up"
    a["pace"]["scale_sure"] = False                       # unsure: say something else
    assert "close-up" not in essay._pace_reason(a, b)
    assert essay._short("gesture", _feat(), _feat()) is None      # nothing to claim


def test_export_names_and_folders(tmp_path, monkeypatch):
    from filmlook.library import essay_out
    assert essay_out.file_name(3, 20, "Café / at night?", "L1000123") == "03_Café-at-night.jpg"
    assert essay_out.file_name(7, 120, "", "L1000123") == "007_L1000123.jpg"
    assert essay_out.file_name(1, 5, "???", "x") == "01_untitled.jpg"
    monkeypatch.setattr(essay_out, "OUT_DIR", tmp_path)
    a, b = essay_out.new_folder("Lisbon: streets"), essay_out.new_folder("Lisbon: streets")
    assert a.name == "Lisbon streets" and b.name == "Lisbon streets 2"     # never overwrites


def test_choose_from_a_brief(monkeypatch):
    import numpy as np
    from filmlook.library import essay

    def v(*xs):
        a = np.array(xs, dtype=np.float32); return a / np.linalg.norm(a)
    pool = {   # id: (z, folder, mono, vec)
        1: (2.0, 1, False, v(1, 0, 0, 0)), 2: (1.9, 1, False, v(1, .02, 0, 0)),     # 2 = near-copy of 1
        3: (1.5, 1, False, v(0, 1, 0, 0)), 4: (1.2, 1, False, v(0, 0, 1, 0)),
        5: (1.0, 2, True, v(0, 0, 0, 1)), 6: (0.2, 3, False, v(.5, .5, .5, .5)), 7: (-1.0, 3, True, v(0, .3, 0, 1)),
    }
    info = {i: {"z": z, "fit": 0.0, "mono": m, "folder": f, "shoot": f"shoot {f}", "graded": True,
                "flag": None, "rating": 0, "scale": None, "vec": vec} for i, (z, f, m, vec) in pool.items()}
    monkeypatch.setattr(essay, "_pool_info", lambda con, ids, theme: {i: dict(info[i]) for i in ids})
    r = essay.choose(None, list(pool), {"count": 3})
    got = [x["id"] for x in r["chosen"]]
    assert got[0] == 1 and 2 not in got                          # best first, never its near-copy
    bench = {x["id"]: x for x in r["bench"]}
    assert bench[2]["reason"].startswith("almost the same") and bench[2]["like_no"] == 1
    r = essay.choose(None, list(pool), {"count": 4, "per_shoot": 2})
    from collections import Counter
    assert max(Counter(info[x["id"]]["folder"] for x in r["chosen"]).values()) <= 2
    r = essay.choose(None, list(pool), {"count": 4, "colour": "bw"})
    assert {x["id"] for x in r["chosen"]} == {5, 7} and "Only 2" in r["note"]
    r = essay.choose(None, list(pool), {"count": 2}, keep=[6])
    assert [x["id"] for x in r["chosen"]][0] == 6 and "you locked it" in r["chosen"][0]["reasons"]


def test_book_pdf_writer_makes_valid_pages(tmp_path):
    from PIL import Image
    from filmlook.library import essay_book
    p = tmp_path / "book.pdf"
    w = essay_book.PdfWriter(p)
    for colour in ("white", "red", "blue"):
        w.add_page(Image.new("RGB", (300, 400), colour), 8.0, 10.0, trim_in=0.1)
    w.close()
    data = p.read_bytes()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert data.count(b"/Type /Page ") == 3 and b"/Count 3" in data
    assert b"/MediaBox [0 0 576.000 720.000]" in data and b"/TrimBox [7.200 7.200 568.800 712.800]" in data
    xref = int(data.rsplit(b"startxref", 1)[1].split()[0])
    assert data[xref:xref + 4] == b"xref"                           # the index points where it should
    assert set(essay_book.SIZES) == {"a4-portrait", "a4-landscape", "8x10-portrait", "8x10-landscape"}


def test_shot_scale_from_people_and_ai():
    face = {"faces": [{"box": [0.4, 0.2, 0.2, 0.25]}]}
    assert essay.shot_scale(face) == ("close", True)
    walker = {"people": [{"box": [0.4, 0.1, 0.2, 0.8], "conf": 0.9}],
              "poses": [{"left_ankle": [0.5, 0.88, 1], "neck": [0.5, 0.2, 1]}]}
    assert essay.shot_scale(walker) == ("full", True)
    cut = {"people": [{"box": [0.3, 0.3, 0.4, 0.7], "conf": 0.9}], "faces": [{"box": [0.45, 0.32, 0.08, 0.08]}]}
    assert essay.shot_scale(cut) == ("medium", True)
    assert essay.shot_scale({}) == ("wide", False)                 # needs the AI to decide
    assert essay.shot_scale({}, {"close": 2.0, "medium": 0.2, "wide": -0.5})[0] == "close"
    assert essay.shot_scale({}, {"close": -0.5, "medium": 1.5, "wide": 0.2})[0] == "medium"
    small = {"people": [{"box": [0.5, 0.6, 0.03, 0.1], "conf": 0.8}]}
    assert essay.shot_scale(small, {"close": -0.5, "medium": 1.5, "wide": 0.2}) == ("wide", True)
