import json
from datetime import datetime, timedelta

import numpy as np

from filmlook.library import cull


def frame(i, t, vec, folder=1, **f):
    base = {"subject_sharp": 0.7, "gesture": 0.2, "edge_cut": 0.0, "edge_clutter": 1.0, "separation": 0.1,
            "saliency": 0.4, "subject_clipped": 0.0, "interaction": 0.0}
    base.update(f)
    return {"id": i, "folder_id": folder, "time": datetime(2025, 1, 1, 12) + timedelta(seconds=t), "graded": False,
            "thumb": None, "verdict": "ok", "sharpness": 0.7, "f": base, "vec": np.array(vec, float), "taste": 0.0}


def test_grouping_by_time_similarity_and_folder():
    a, b = [1, 0], [0, 1]
    fr = [frame(1, 0, a), frame(2, 5, a), frame(3, 12, a), frame(4, 15, b), frame(5, 100, b), frame(6, 101, b, folder=2)]
    sizes = [[f["id"] for f in g] for g in cull.group(fr)]
    assert sizes == [[1, 2, 3], [4], [5], [6]]


def test_sharper_face_with_open_eyes_wins_with_reason():
    g = [frame(1, 0, [1, 0], face_quality=0.6, face_size=0.01, face_sharp=0.5, eyes_open=0.3),
         frame(2, 1, [1, 0], face_quality=0.6, face_size=0.01, face_sharp=0.8, eyes_open=0.3),
         frame(3, 2, [1, 0], face_quality=0.7, face_size=0.01, face_sharp=0.8, eyes_open=0.05)]   # eyes shut
    r = cull.rank(g)
    assert r["pick"] == 2 and r["order"][-1] in (1, 3)
    assert any("sharpest" in x or "face" in x for x in r["reasons"])


def test_near_tie_is_unsure_and_single_frames_get_a_note():
    g = [frame(1, 0, [1, 0]), frame(2, 1, [1, 0], subject_sharp=0.705)]
    assert cull.rank(g)["unsure"]
    single = cull.rank([frame(3, 0, [1, 0], gesture=0.6)])
    assert single["pick"] == 3 and "strong gesture" in single["reasons"]


def test_fit_weights_learns_from_choices():
    rng = np.random.default_rng(0)
    groups, chosen = [], set()
    n = 0
    for k in range(40):                      # in every group you chose the frame with the most gesture
        g = [frame(n + i, i, [1, 0], folder=k, gesture=float(rng.random()), subject_sharp=float(0.6 + 0.2 * rng.random()))
             for i in range(3)]
        n += 3
        groups.append(g)
        chosen.add(max(g, key=lambda f: f["f"]["gesture"])["id"])
    w = cull.fit_weights(groups, chosen)
    assert w["gesture"] > cull.DEFAULT_WEIGHTS["gesture"]
    before, _ = cull.agreement(groups, chosen)
    after, _ = cull.agreement(groups, chosen, w)
    assert after >= before


def test_starting_the_same_shoot_again_resumes(tmp_path, monkeypatch):
    from filmlook.library import db
    con = db.connect(tmp_path / "lib.db")
    monkeypatch.setattr(cull, "scope_photo_ids", lambda con, scope: [1])
    monkeypatch.setattr(cull, "load_frames", lambda con, ids: [frame(1, 0, [1, 0])])
    a = cull.start_session(con, {"folder_id": 5, "categories": ["street"]}, "Shoot")
    b = cull.start_session(con, {"categories": ["street"], "folder_id": 5}, "Shoot")
    assert a == b
    con.execute("UPDATE cull_sessions SET finished_at=datetime('now') WHERE id=?", (a,))
    assert cull.start_session(con, {"folder_id": 5, "categories": ["street"]}, "Shoot") != a


def test_delete_session_keeps_marks_and_picks_in_order(tmp_path, monkeypatch):
    from filmlook.library import db
    con = db.connect(tmp_path / "lib.db")
    con.execute("INSERT INTO drives (id, uuid, name) VALUES (1, 'U', 'D')")
    con.execute("INSERT INTO folders (id, drive_id, rel_path) VALUES (1, 1, 'f')")
    for i in (1, 2, 3):
        con.execute("INSERT INTO files (id, drive_id, folder_id, rel_path, kind, size, mtime) VALUES (?,1,1,?,'raw',1,0)", (i, f"f/{i}.dng"))
        con.execute("INSERT INTO photos (id, primary_file_id, folder_id) VALUES (?,?,1)", (i, i))
    monkeypatch.setattr(cull, "scope_photo_ids", lambda con, scope: [1, 2, 3])
    monkeypatch.setattr(cull, "load_frames", lambda con, ids: [frame(3, 0, [1, 0]), frame(1, 100, [0, 1]), frame(2, 101, [0, 1])])
    sid = cull.start_session(con, {"folder_id": 1}, "Shoot")
    cull.mark(con, sid, 2, "pick"); cull.mark(con, sid, 3, "pick"); cull.mark(con, sid, 1, "reject")
    assert cull.session_picks(con, sid) == [3, 2]           # group order, then shooting order
    cull.delete_session(con, sid)
    assert con.execute("SELECT COUNT(*) FROM cull_sessions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM marks WHERE flag='pick'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM cull_log").fetchone()[0] == 3


def test_retrain_learns_from_cull_picks_and_records_history(tmp_path, monkeypatch):
    from filmlook.library import db
    con = db.connect(tmp_path / "lib.db")
    con.execute("PRAGMA foreign_keys=OFF")    # synthetic frames, no photo rows behind them
    rng = np.random.default_rng(3)
    frames, chosen, gid = {}, set(), 0
    con.execute("INSERT INTO cull_sessions (id, label, scope, weights) VALUES (1, 'Shoot', '{}', '{}')")
    for k in range(30):                       # you always pick the frame with the best gesture
        g = [frame(100 + 3 * k + i, i, [1, 0], folder=k, gesture=float(rng.random())) for i in range(3)]
        for f in g:
            frames[f["id"]] = f
        best = max(g, key=lambda f: f["f"]["gesture"])
        con.execute("INSERT INTO cull_groups (session_id, idx, photo_ids, suggested) VALUES (1, ?, ?, ?)",
                    (k, json.dumps([f["id"] for f in g]), g[0]["id"]))
        con.execute("INSERT INTO marks (photo_id, flag) VALUES (?, 'pick')", (best["id"],))
    con.commit()
    monkeypatch.setattr(cull, "load_frames", lambda con, ids: [frames[i] for i in ids if i in frames])
    info = cull.retrain(con)
    assert info["groups"] == 30 and info["after"] >= info["before"]
    assert cull.current_weights(con)["gesture"] > cull.DEFAULT_WEIGHTS["gesture"]
    hist = cull.learning_info(con)["history"]
    assert hist and hist[0]["groups"] == 30
