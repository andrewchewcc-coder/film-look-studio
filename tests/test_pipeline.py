import json

import numpy as np
import pytest

from filmlook import dng, lut, pipeline, recipes, reference


def ramp(h=64, w=96, seed=0):
    rng = np.random.default_rng(seed)
    return rng.uniform(0, 1, (h, w, 3)).astype(np.float32)


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(recipes, "USER_LOOKS_DIR", tmp_path)
    return tmp_path


def test_all_builtin_looks_resolve():
    looks = recipes.list_looks()
    ids = {lk["id"] for lk in looks}
    assert {"classic-chrome", "leica-m9", "tri-x-gold"} <= ids
    for lk in looks:
        r = recipes.load_look(lk["id"])
        out = pipeline.render(ramp(), r, gain=1.0)
        assert out.shape == (64, 96, 3) and out.min() >= 0 and out.max() <= 1


def test_neutral_look_stage_is_identity():
    d = ramp()
    out = pipeline.look(d, recipes.load_look(None))
    assert np.abs(out - d).max() < 2e-3


def test_neutral_lut_is_identity():
    table = lut.bake(recipes.load_look(None), size=9)
    g = np.linspace(0, 1, 9)
    assert np.allclose(table[1], [g[1], 0, 0], atol=2e-3)  # red varies fastest
    assert np.allclose(table[9], [0, g[1], 0], atol=2e-3)
    assert np.abs(table[-1] - 1).max() < 2e-3


def test_cube_file(tmp_path):
    p = lut.write_cube(recipes.load_look("classic-chrome"), tmp_path / "cc.cube", size=5)
    lines = p.read_text().splitlines()
    assert "LUT_3D_SIZE 5" in lines
    assert len([l for l in lines if l[:1].isdigit()]) == 125


def test_mono_without_toning_is_grey():
    r = recipes.apply_sets(recipes.load_look("fan-ho"), ["grain.amount=0", "vignette.amount=0"])
    out = pipeline.look(ramp(), r)
    assert np.abs(out[..., 0] - out[..., 1]).max() < 2e-3
    assert np.abs(out[..., 1] - out[..., 2]).max() < 2e-3


def test_tri_x_gold_is_warm():
    out = pipeline.look(np.full((4, 4, 3), 0.5, np.float32), recipes.load_look("tri-x-gold"))
    r, g, b = out[0, 0]
    assert r > g > b  # golden: red > green > blue


def test_strip_render_matches_whole_image():
    """Exports render in strips; grain and vignette must be seamless."""
    lin = ramp(200, 120, seed=3) * 0.5
    r = recipes.apply_sets(recipes.load_look("tri-x-gold"), ["grain.size=8"])
    whole = pipeline.render(lin, r, gain=1.2)
    strips = np.concatenate([
        pipeline.render(lin[y:y + 37], r, gain=1.2, y0=y, full_size=(200, 120))
        for y in range(0, 200, 37)])
    assert np.abs(whole - strips).max() < 1e-4


def test_strength_zero_returns_neutral():
    lin = ramp() * 0.6
    r = recipes.load_look("leica-m9"); r["strength"] = 0.0
    assert np.allclose(pipeline.render(lin, r, 1.0), pipeline.base(lin, 1.0), atol=1e-5)


def test_extends_and_overrides(user_dir):
    recipes.save_user_look("My Chrome", {"name": "My Chrome", "extends": "classic-chrome",
                                         "color": {"temp": 20}})
    r = recipes.load_look("my-chrome")
    assert r["color"]["temp"] == 20
    assert r["color"]["saturation"] == recipes.load_look("classic-chrome")["color"]["saturation"]
    r2 = recipes.apply_sets(r, ["tone.contrast=1.5", "mono.enabled=true"])
    assert r2["tone"]["contrast"] == 1.5 and r2["mono"]["enabled"] is True


def test_circular_extends(user_dir):
    recipes.save_user_look("a", {"extends": "b"})
    recipes.save_user_look("b", {"extends": "a"})
    with pytest.raises(ValueError):
        recipes.load_look("a")


def test_adjust_sliders():
    base = recipes.load_look("tri-x-gold")
    r = recipes.apply_adjust(base, {"warmth": 10, "contrast": 0.1, "tone": -1, "strength": 0.5, "bogus": 3})
    assert r["color"]["temp"] == base["color"]["temp"] + 10
    assert r["tone"]["contrast"] == pytest.approx(base["tone"]["contrast"] * 1.1)
    assert all(z["amount"] == 0 for z in r["color"]["split"].values())
    assert r["strength"] == 0.5


def test_reference_match_moves_toward_reference(tmp_path, user_dir):
    from PIL import Image
    warm = np.zeros((50, 50, 3), np.uint8); warm[...] = (200, 140, 80)
    warm[::2] = (120, 80, 40)
    Image.fromarray(warm).save(tmp_path / "warm.jpg")
    look_id, _ = reference.make_look(tmp_path / "warm.jpg", "Warm ref", amount=1.0)
    r = recipes.load_look(look_id)
    assert not r["mono"]["enabled"]
    grey = np.full((8, 8, 3), 0.4, np.float32) + ramp(8, 8) * 0.1
    out = pipeline.look(grey, r)
    assert out[..., 0].mean() > out[..., 2].mean() + 0.1


def test_toned_bw_reference_makes_mono_look(tmp_path, user_dir):
    from PIL import Image
    g = np.linspace(30, 220, 64).astype(np.uint8)
    img = np.stack([g * 1.0, g * 0.93, g * 0.8], -1).astype(np.uint8)
    Image.fromarray(np.tile(img[None], (32, 1, 1))).save(tmp_path / "sepia.png")
    look_id, _ = reference.make_look(tmp_path / "sepia.png", "Sepia ref")
    data = json.loads((user_dir / f"{look_id}.json").read_text())
    assert data["mono"]["enabled"] and "midtones" in data["color"]["split"]


def test_warp_identity_and_crop():
    img = (ramp(40, 60) * 65535).astype(np.uint16)
    info = dng.DngInfo(crop_origin=(4, 2), crop_size=(50, 30),
                       warp=[([(1.0, 0, 0, 0, 0, 0)], 0.5, 0.5)])
    out = dng.apply(img, info)
    assert out.shape == (30, 50, 3)
    assert np.abs(out.astype(int) - img[2:32, 4:54].astype(int)).max() <= 1
    half = dng.apply(img, info, scale=0.5)
    assert half.shape == (15, 25, 3)


def test_warp_barrel_correction_pulls_from_inside():
    img = np.zeros((101, 101, 3), np.float32)
    img[50, :, :] = 1.0  # horizontal line through the centre stays straight
    out = dng.warp_rectilinear(img, ([(0.9, 0, 0, 0, 0, 0)], 0.5, 0.5))
    assert out[50, 10, 0] > 0.99 and out[40, 50, 0] < 0.01


@pytest.mark.parametrize("look_id", ["classic-chrome", "leica-m9", "tri-x-gold", "daido-moriyama"])
def test_baked_lut_matches_exact_look(look_id):
    """Exports use a 65^3 LUT; it must match the exact maths used by previews."""
    r = recipes.load_look(look_id)
    d = ramp(80, 80, seed=5)
    exact = pipeline.look(d, r)
    fast = lut.apply(d, lut.bake(r, size=65, blend=False))
    err = np.abs(exact - fast)
    # Max error only occurs where very saturated colours clip at the sRGB gamut edge.
    assert err.mean() < 0.5 / 255 and err.max() < 8 / 255


def test_gold_toned_reference_is_mono_and_warm(tmp_path, user_dir):
    from PIL import Image
    g = np.linspace(20, 235, 256).astype(np.float32)  # tint grows with brightness, like a real print
    img = np.stack([g, g * 0.9, g * 0.72], -1).astype(np.uint8)
    Image.fromarray(np.tile(img[None], (40, 1, 1))).save(tmp_path / "gold.jpg")
    look_id, _ = reference.make_look(tmp_path / "gold.jpg", "Gold")
    r = recipes.load_look(look_id)
    assert r["mono"]["enabled"]
    out = pipeline.look(np.full((4, 4, 3), 0.5, np.float32), r)
    assert out[0, 0, 0] > out[0, 0, 2] + 0.03


def test_real_colour_photo_is_not_mono():
    rng = np.random.default_rng(1)
    assert not reference.looks_monochrome(rng.uniform(0, 1, (60, 60, 3)).astype(np.float32))
