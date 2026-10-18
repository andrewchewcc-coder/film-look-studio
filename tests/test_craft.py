import numpy as np

from filmlook.library import craft


def _ctx(n, busy=(), scale=None, z=None, motifs=None, mono=(), sim=None, cd=None):
    ids = list(range(100, 100 + n))
    return {"ids": ids, "idx": {p: k for k, p in enumerate(ids)}, "T": np.zeros((n, n)),
            "z": list(z or [0.5] * n), "busy": [k in busy for k in range(n)],
            "scale": list(scale or ["medium", "wide", "close", "full"] * n)[:n],
            "light": ["mid", "bright", "dark"] * n, "cd": cd if cd is not None else np.zeros((n, n)),
            "sim": sim if sim is not None else np.zeros((n, n)), "contrast": np.zeros((n, n)),
            "mono": [k in mono for k in range(n)], "motifs": motifs or [set() for _ in range(n)],
            "weak": [True] * n}


def test_check_flags_busy_runs_motifs_and_mixed_look():
    mot = [set() for _ in range(8)]
    mot[3] = mot[4] = {"umbrellas"}
    ctx = _ctx(8, busy={1, 2, 3}, motifs=mot, mono={6})
    items = craft.check(ctx["ids"], ctx, {})
    bad = {x["rule"]: x for x in items if not x["ok"]}
    assert bad["Density"]["positions"] == [2, 3, 4] and bad["Density"]["fix"] == "move"
    assert "umbrellas" in bad["Motifs"]["text"]
    assert "1 black & white photo" in bad["One look"]["text"]
    assert {x["rule"] for x in items if x["ok"]} >= {"Opener", "Closer", "Shot size"}


def test_score_prefers_spaced_motifs_and_a_readable_opener():
    mot = [set() for _ in range(6)]
    mot[0] = mot[1] = {"dogs"}
    ctx = _ctx(6, busy={0}, motifs=mot)
    together = ctx["ids"]
    apart = [101, 102, 100, 103, 104, 105]      # busy opener moved, dogs no longer side by side
    assert craft.score(apart, ctx, "classic") > craft.score(together, ctx, "classic")


def test_chapters_and_heroes():
    assert craft.chapter_starts(12) == [0]
    assert craft.chapter_starts(24) == [0, 12]
    assert craft.chapter_starts(36) == [0, 12, 24]
    ctx = _ctx(10, z=[0, 0, 3, 0, 0, 0, 0, 0, 0, 0])
    assert craft.heroes(ctx, ctx["ids"]) == [102]
