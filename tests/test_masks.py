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


def test_drawn_masks_brush_and_gradients(tmp_path, monkeypatch):
    import io
    from PIL import Image
    monkeypatch.setattr("filmlook.library.LIBRARY_DIR", tmp_path)
    p = tmp_path / "x.dng"
    p.write_bytes(b"x")
    pic = np.zeros((50, 100), np.uint8)
    pic[:, :50] = 255                                                  # painted: the left half
    buf = io.BytesIO()
    Image.fromarray(pic).save(buf, "PNG")
    bid = masks.save_brush(buf.getvalue())
    assert masks.brush_file(bid) is not None and masks.brush_file("../etc/passwd") is None
    r = recipes.apply_adjust(recipes.load_look("neutral"), {"masks": [
        {"kind": "brush", "id": bid, "exposure": 0.5},
        {"kind": "brush", "id": "nonsense", "exposure": 0.5},
        {"kind": "linear", "x0": 0.5, "y0": 0.0, "x1": 0.5, "y1": 1.0, "exposure": -1},
        {"kind": "radial", "cx": 0.5, "cy": 0.5, "rx": 0.2, "ry": 0.2, "feather": 0.3, "exposure": 1}]})
    assert [m["kind"] for m in r["masks"]] == ["brush", "brush", "linear", "radial"] and r["masks"][1]["id"] == ""
    a = masks.alphas(p, r, None, (200, 100), (0, 0, 200, 100))
    assert a[0][50, 20] > 0.99 and a[0][50, 180] < 0.01               # the left half, at any size
    assert a[1].max() == 0                                            # unknown id: nothing
    assert a[2][2, 100] > 0.95 and a[2][97, 100] < 0.05               # linear: strong at the top, gone at the bottom
    assert a[3][50, 100] > 0.99 and a[3][5, 5] < 0.01                 # radial: the middle only
    strip = masks.alphas(p, r, None, (200, 100), (0, 60, 200, 70))[0]
    assert strip.shape == (10, 200) and strip[:, 20].min() > 0.99


def test_drawn_masks_follow_a_new_crop(tmp_path, monkeypatch):
    """Paint / place under one crop, change the crop: the mask stays on the same part of the photo."""
    import io
    from PIL import Image
    from filmlook import geometry, raw
    monkeypatch.setattr("filmlook.library.LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(raw, "full_size", lambda path: (400, 200))
    p = tmp_path / "x.dng"
    p.write_bytes(b"x")
    old = geometry.normalise({"crop": [0.5, 0.0, 0.5, 1.0]})          # painted on the right half of the photo
    pic = np.zeros((100, 100), np.uint8)
    pic[:, :50] = 255                                                  # ... its left half = photo x 200..300
    buf = io.BytesIO()
    Image.fromarray(pic).save(buf, "PNG")
    bid = masks.save_brush(buf.getvalue())
    r = recipes.apply_adjust(recipes.load_look("neutral"), {"masks": [{"kind": "brush", "id": bid, "geom": old, "exposure": 1}]})
    assert r["masks"][0]["geom"]["crop"] == old["crop"]
    a = masks.alphas(p, r, None, (400, 200), (0, 0, 400, 200))[0]      # now uncropped
    assert a[100, 250] > 0.99 and a[100, 150] < 0.01 and a[100, 350] < 0.01
    v = masks.brush_view(bid, old, geometry.normalise(None), (400, 200), edge=400)
    assert v.size == (400, 200) and np.asarray(v)[100, 250] > 250 and np.asarray(v)[100, 50] == 0
    # gradients: numbers moved from the old frame to the new one
    lin, rad = masks.refit([{"kind": "linear", "x0": 0.0, "y0": 0.5, "x1": 1.0, "y1": 0.5},
                            {"kind": "radial", "cx": 0.5, "cy": 0.5, "rx": 0.2, "ry": 0.4}], old, geometry.normalise(None), (400, 200))
    assert abs(lin["x0"] - 0.5) < 1e-6 and abs(lin["x1"] - 1.0) < 1e-6
    assert abs(rad["cx"] - 0.75) < 1e-6 and abs(rad["rx"] - 0.1) < 1e-6 and abs(rad["ry"] - 0.4) < 1e-6
