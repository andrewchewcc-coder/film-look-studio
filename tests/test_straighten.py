import numpy as np
from PIL import Image, ImageDraw

from filmlook import straighten


def street(tilt_ccw=0.0, keystone=0.0, w=1600, h=1067):
    """Building edges: vertical bars that converge (looking up) by `keystone`, then the whole
    picture turned counter-clockwise by `tilt_ccw` degrees (as a tilted camera would)."""
    im = Image.new("L", (w + 400, h + 400), 200)
    d = ImageDraw.Draw(im)
    for i, x in enumerate(np.linspace(300, w + 100, 9)):
        off = (x - (w + 400) / 2) / (w / 2)
        lean = keystone * off * (h / 2)                     # tops lean in towards the middle
        d.line([(x, 200), (x - lean, h + 200)], fill=40 + 10 * i, width=6)
    im = im.rotate(tilt_ccw, resample=Image.BICUBIC, fillcolor=200).crop((200, 200, w + 200, h + 200))
    return np.asarray(im, np.float32) / 255


def test_level_photo_stays_level():
    angle, conf = straighten.estimate(street())
    assert abs(angle) < 0.2 and conf >= straighten.SURE


def test_tilted_photo_is_turned_back():
    angle, _ = straighten.estimate(street(tilt_ccw=2.0))
    assert abs(angle - 2.0) < 0.3                            # rotate clockwise 2 degrees to fix it
    angle, _ = straighten.estimate(street(tilt_ccw=-1.5))
    assert abs(angle + 1.5) < 0.3


def test_looking_up_is_not_mistaken_for_a_tilt():
    angle, _ = straighten.estimate(street(tilt_ccw=1.0, keystone=0.06))
    assert abs(angle - 1.0) < 0.4


def test_no_lines_no_answer():
    angle, conf = straighten.estimate(np.full((800, 1200), 0.5, np.float32))
    assert angle is None and conf == 0


def _fixed_k(img, v):
    """Keystone left after the app's Vertical perspective v."""
    from filmlook import geometry
    rgb = np.repeat(img[:, :, None], 3, axis=2).astype(np.float32)
    out = geometry.apply(rgb, {"perspective": {"v": v, "h": 0}})
    k, _, _ = straighten.keystone(out)
    return k


def test_auto_perspective_stands_converging_verticals_up():
    from filmlook import geometry
    for keystone in (0.06, -0.04):                       # street(): + = tops lean out (looking down), − = looking up
        img = street(keystone=keystone)
        k, tilt, conf = straighten.keystone(img)
        assert conf >= straighten.SURE and abs(tilt) < 0.3
        assert (k > 0) == (keystone > 0)                 # tops lean out = positive k
        v = straighten.vertical_slider(k, img.shape[1], img.shape[0], geometry.PERSP_K)
        assert (v < 0) == (keystone > 0)                 # looking down: − widens the bottom
        assert abs(_fixed_k(img, round(v))) < abs(k) * 0.25


def test_auto_perspective_leaves_straight_photos_alone():
    from filmlook import geometry
    k, _, _ = straighten.keystone(street())
    assert abs(straighten.vertical_slider(k, 1600, 1067, geometry.PERSP_K)) < 3
