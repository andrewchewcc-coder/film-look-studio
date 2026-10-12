import math

import numpy as np
import pytest
from PIL import Image

from filmlook import edits, engine, geometry, raw, recipes


def img(h=60, w=90):
    a = np.zeros((h, w, 3), np.float32)
    a[:, :, 0] = np.linspace(0, 1, w)[None, :]
    a[:, :, 1] = np.linspace(0, 1, h)[:, None]
    return a


def test_identity_and_normalise():
    a = img()
    assert geometry.apply(a, None) is a
    g = geometry.normalise({"rot90": 450, "angle": 99, "crop": [-0.2, 0.5, 2, 0.8]})
    assert g["rot90"] == 90 and g["angle"] == 45.0
    assert g["crop"] == [0.0, 0.2, 1.0, 0.8]
    assert geometry.normalise({"crop": [0, 0, 1, 1]})["crop"] is None


def test_turns_flips_and_straight_crop_are_exact():
    a = img()
    out = geometry.apply(a, {"rot90": 90})
    assert out.shape == (90, 60, 3) and np.allclose(out[0, -1], a[0, 0])   # top-left goes top-right
    assert np.allclose(geometry.apply(a, {"flip_h": True})[:, 0], a[:, -1])
    c = geometry.apply(a, {"crop": [0.5, 0.25, 0.25, 0.5]})
    assert c.shape == (30, 22, 3) or c.shape == (30, 23, 3)
    assert np.allclose(c[0, 0], a[15, 45])
    assert geometry.output_size(90, 60, {"rot90": 270, "crop": [0, 0, 0.5, 0.5]}) == (30, 45)


def test_rotation_is_clockwise():
    a = np.zeros((101, 101, 3), np.float32)
    a[50, 90] = 1.0                       # a dot to the right of centre
    out = geometry.apply(a, {"angle": 30})
    y, x = np.unravel_index(out[..., 0].argmax(), out.shape[:2])
    assert y > 55 and x > 70              # clockwise turn moves it down-right


def test_max_crop_has_no_empty_corners():
    for angle in (3, -12.5, 30):
        x, y, w, h = geometry.max_crop(900, 600, angle, ratio=1.5)
        for px, py in [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]:
            assert geometry.valid(px * 900, py * 600, 900, 600, angle)
        assert abs((w * 900) / (h * 600) - 1.5) < 1e-6
    a = np.ones((600, 900, 3), np.float32)
    out = geometry.apply(a, {"angle": 12.5, "crop": geometry.max_crop(900, 600, 12.5)})
    assert out.min() > 0.99               # nothing empty pulled in


def test_same_edit_same_framing_at_any_size():
    big = img(600, 900)
    small = np.asarray(Image.fromarray((big * 255).astype("uint8")).resize((180, 120), Image.LANCZOS), np.float32) / 255
    g = {"angle": 7, "crop": [0.1, 0.2, 0.6, 0.5]}
    a = geometry.apply(big, g)
    b = geometry.apply(small, g)
    a_small = np.asarray(Image.fromarray((a * 255).astype("uint8")).resize(b.shape[1::-1], Image.LANCZOS), np.float32) / 255
    assert np.abs(a_small - b).mean() < 0.02


@pytest.fixture
def library(tmp_path, monkeypatch):
    from filmlook.library import db, drives
    monkeypatch.setattr(db, "LIBRARY_DIR", tmp_path / "lib")
    monkeypatch.setattr(drives, "_volume_info", lambda mount: ("TEST-UUID", "Test"))
    monkeypatch.setattr(edits, "_local", type(edits._local)())
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path / "cache")
    engine._mem_cache.clear()
    return tmp_path


def test_edits_persist_and_reset(library):
    photo = library / "a.jpg"
    Image.new("RGB", (60, 40), (120, 80, 40)).save(photo)
    assert geometry.is_identity(edits.get(photo))
    edits.put(photo, {"angle": 2.5, "crop": [0.1, 0.1, 0.5, 0.5], "aspect": "1:1"})
    edits._local = type(edits._local)()   # a fresh connection, like reopening the app
    g = edits.get(photo)
    assert g["angle"] == 2.5 and g["aspect"] == "1:1" and g["crop"] == [0.1, 0.1, 0.5, 0.5]
    edits.reset(photo)
    assert geometry.is_identity(edits.get(photo))
    assert photo.read_bytes()[:2] == b"\xff\xd8"   # the photo itself is untouched


def test_jpg_neutral_is_unchanged_and_export_matches_preview(library):
    rng = np.random.default_rng(3)
    base = (np.clip(rng.normal(0.5, 0.2, (40, 60, 3)), 0, 1) * 255).astype("uint8")
    src = library / "phone.jpg"
    Image.fromarray(base).resize((1200, 800), Image.BICUBIC).save(src, quality=97)
    orig = np.asarray(Image.open(src), np.float32)
    neutral = np.asarray(engine.render_preview(src, recipes.load_look(None), edge=1200), np.float32)
    assert np.abs(neutral - orig).mean() < 1.5          # no auto exposure / contrast on JPGs
    assert raw.full_size(src) == (1200, 800)
    g = {"angle": -6, "crop": geometry.max_crop(1200, 800, -6, ratio=4 / 5), "aspect": "4:5"}
    look = recipes.load_look("classic-chrome")
    look["grain"]["amount"] = 0                          # grain is random per size; compare the rest
    out = engine.export(src, look, out_dir=library / "graded", geom=g)
    exported = Image.open(out)
    assert exported.size == geometry.output_size(1200, 800, g)
    prev = engine.render_preview(src, look, edge=max(exported.size), geom=g)
    a = np.asarray(exported.resize(prev.size, Image.LANCZOS), np.float32)
    assert np.abs(a - np.asarray(prev, np.float32)).mean() < 3.0


def test_with_ratio_keeps_turns_and_angle():
    g = geometry.with_ratio({"rot90": 90, "angle": 3.0}, 900, 600, 4 / 5, "4:5")
    assert g["rot90"] == 90 and g["angle"] == 3.0 and g["aspect"] == "4:5"
    ow, oh = geometry.output_size(900, 600, g)
    assert abs(ow / oh - 0.8) < 0.01
