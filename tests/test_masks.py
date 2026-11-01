import numpy as np

from filmlook import edits, engine, masks, pipeline, recipes


def fake_photo(tmp_path, monkeypatch):
    """A 'photo' whose sky mask is its top half (no Vision needed)."""
    p = tmp_path / "x.dng"
    p.write_bytes(b"x")
    top = np.zeros((100, 150), np.float32)
    top[:50] = 1
    monkeypatch.setattr(masks, "get", lambda path, kind: top if kind == "sky" else np.zeros_like(top))
    masks._mem.clear()
    return p


def test_alphas_follow_any_box_of_the_final_frame(tmp_path, monkeypatch):
    p = fake_photo(tmp_path, monkeypatch)
    r = recipes.apply_adjust(recipes.load_look("neutral"), {"masks": [{"kind": "sky", "exposure": -1},
                                                                     {"kind": "sky", "invert": True, "saturation": 0.5},
                                                                     {"kind": "moon", "exposure": 1}]})
    assert [m["kind"] for m in r["masks"]] == ["sky", "sky"]          # unknown kinds dropped
    whole = masks.alphas(p, r, None, (300, 200), (0, 0, 300, 200))
    assert whole[0].shape == (200, 300) and whole[0][10].min() > 0.99 and whole[0][190].max() < 0.01
    assert np.allclose(whole[1], 1 - whole[0])                        # invert
    strip = masks.alphas(p, r, None, (300, 200), (0, 120, 300, 140))[0]   # an export strip
    assert strip.shape == (20, 300) and strip.max() < 0.01


def test_local_adjusts_only_inside_the_mask(tmp_path, monkeypatch):
    p = fake_photo(tmp_path, monkeypatch)
    r = recipes.apply_adjust(recipes.load_look("neutral"), {"masks": [{"kind": "sky", "exposure": -1}]})
    d = np.full((200, 300, 3), 0.6, np.float32)
    out = pipeline.local(d, r, masks.alphas(p, r, None, (300, 200), (0, 0, 300, 200)))
    assert out[20, 20, 0] < 0.5 and abs(out[180, 20, 0] - 0.6) < 1e-6


def test_engine_only_builds_masks_when_they_do_something(tmp_path, monkeypatch):
    p = fake_photo(tmp_path, monkeypatch)
    idle = recipes.apply_adjust(recipes.load_look("neutral"), {"masks": [{"kind": "sky"}]})
    assert engine._masks_at(p, idle, None, (300, 200)) is None
    assert edits.is_plain("neutral", {"masks": [{"kind": "sky", "exposure": 0}]})
    assert not edits.is_plain("neutral", {"masks": [{"kind": "sky", "exposure": 0.3}]})
