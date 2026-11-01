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
