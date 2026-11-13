"""AI masks for local adjustments (Grade → Masks): subject, people, sky.

Each mask is found once per photo on a ~1536 px neutral preview of the whole upright frame and
cached as a PNG in the library folder (never next to the photo):

    subject  Apple Vision's foreground instances (what stands out: people, a cyclist, a cat…)
    people   the subject instances that overlap a person Vision finds, plus Vision's person
             segmentation (which alone misses small, distant figures)
    sky      no Apple detector for this: bright, smooth regions joined to the top edge

Edges are refined with a guided filter (guided by the photo), so a mask hugs real edges.
`alphas` turns the cached masks into per-pixel strengths for any part of the final (cropped,
straightened) frame at any size, so previews, zoomed tiles and full-size export strips line up.
"""
import hashlib
import threading
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import geometry

KINDS = ("subject", "people", "sky")                     # found by Apple Vision / the sky finder
SHAPES = ("brush", "linear", "radial")                   # drawn by you (dodge / burn / brush, gradients)
NAMES = {"subject": "Subject", "people": "People", "sky": "Sky", "brush": "Brush", "linear": "Linear gradient",
         "radial": "Radial gradient"}
EDGE = 1536
VERSION = 1
_lock = threading.Lock()
_mem = {}                       # (path, mtime, kind, geometry key) -> float32 mask after the edit


def _dir():
    from .library import LIBRARY_DIR
    d = Path(LIBRARY_DIR) / "masks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(path, kind):
    p = Path(path)
    k = hashlib.sha1(f"{p.resolve()}|{p.stat().st_mtime_ns}|{VERSION}".encode()).hexdigest()[:16]
    return _dir() / f"{k}_{kind}.png"


# --- finding the masks ------------------------------------------------------------------------------

def _pixels(pb):
    import Quartz
    Quartz.CVPixelBufferLockBaseAddress(pb, 1)
    try:
        w, h = Quartz.CVPixelBufferGetWidth(pb), Quartz.CVPixelBufferGetHeight(pb)
        bpr = Quartz.CVPixelBufferGetBytesPerRow(pb)
        fmt = Quartz.CVPixelBufferGetPixelFormatType(pb).to_bytes(4, "big")
        raw = np.frombuffer(bytes(Quartz.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)), np.uint8).reshape(h, bpr)
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)
    if fmt == b"L00f":                                   # one float per pixel
        return raw[:, :w * 4].copy().view(np.float32)
    return raw[:, :w].astype(np.float32) / 255.0         # one byte per pixel


def _fit(a, shape):
    if a.shape == shape:
        return a
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((shape[1], shape[0]), Image.BILINEAR))


def _vision(im):
    """{'subject': mask, 'people': mask} (float32 0..1, image size) from Apple Vision."""
    import Foundation
    import Vision
    from .library import scene
    shape = (im.height, im.width)
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(scene._nsdata(im), None)
    fg = Vision.VNGenerateForegroundInstanceMaskRequest.alloc().init()
    humans = Vision.VNDetectHumanRectanglesRequest.alloc().init()
    humans.setUpperBodyOnly_(False)                       # whole people, not just head and shoulders
    seg = Vision.VNGeneratePersonSegmentationRequest.alloc().initWithCompletionHandler_(None)
    seg.setQualityLevel_(0)                                # accurate
    inst = Vision.VNGeneratePersonInstanceMaskRequest.alloc().init()   # up to 4 people, each on its own
    with scene._vision_lock:
        handler.performRequests_error_([fg, humans, seg, inst], None)
        subject = np.zeros(shape, np.float32)
        people = np.zeros(shape, np.float32)
        boxes = []
        for b in humans.results() or []:                  # normalised, origin bottom-left
            r = b.boundingBox()
            boxes.append((r.origin.x * im.width, (1 - r.origin.y - r.size.height) * im.height,
                          (r.origin.x + r.size.width) * im.width, (1 - r.origin.y) * im.height))
        obs = (fg.results() or [None])[0]
        if obs is not None:
            for i in obs.allInstances():
                one = Foundation.NSIndexSet.indexSetWithIndex_(i)
                pb, err = obs.generateScaledMaskForImageForInstances_fromRequestHandler_error_(one, handler, None)
                if pb is None:
                    continue
                m = _fit(_pixels(pb), shape)
                subject = np.maximum(subject, m)
                ys, xs = np.nonzero(m > 0.5)
                if len(xs) and any(_overlap(b, (xs.min(), ys.min(), xs.max(), ys.max())) > 0.5 for b in boxes):
                    people = np.maximum(people, m)
        pobs = (inst.results() or [None])[0]
        if pobs is not None and len(pobs.allInstances()):
            pb, err = pobs.generateScaledMaskForImageForInstances_fromRequestHandler_error_(pobs.allInstances(), handler, None)
            if pb is not None:
                people = np.maximum(people, _fit(_pixels(pb), shape))
        res = seg.results()
        if res:
            people = np.maximum(people, _fit(_pixels(res[0].pixelBuffer()), shape))
    return {"subject": subject, "people": people}


def _overlap(a, b):
    """Share of box a covered by box b (here: how much of a person is inside a subject)."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    return w * h / area


def _sky(rgb):
    """Smooth regions joined to the top edge that look like sky: blue, or bright and nearly
    colourless (cloud, haze, overcast). A wall reaching the top edge is usually neither, and
    a real sky spans a good part of the top edge (float32 0..1)."""
    from .color import linear_to_oklab, srgb_decode
    h, w = rgb.shape[:2]
    k = 384 / max(h, w)
    small = np.clip(ndimage.zoom(rgb, (k, k, 1), order=1), 0, 1)
    lab = linear_to_oklab(srgb_decode(small))
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    hue = np.degrees(np.arctan2(b, a)) % 360
    blue = (hue > 200) & (hue < 290) & (chroma > 0.02) & (L > 0.55)
    pale = (L > 0.8) & (chroma < 0.035)
    g = small @ np.float32([0.2126, 0.7152, 0.0722])
    tex = ndimage.uniform_filter(np.hypot(ndimage.sobel(g, 0), ndimage.sobel(g, 1)), 3)
    cand = (blue | pale) & (tex < 0.15)
    cand = ndimage.binary_opening(cand, iterations=1)
    lab_, n = ndimage.label(cand)
    sh, sw = g.shape
    keep = np.zeros_like(cand)
    for k_ in np.unique(lab_[0][lab_[0] > 0]):
        region = lab_ == k_
        top_share = region[0].mean()
        is_blue = blue[region].mean() > 0.4
        if region.sum() > 0.01 * region.size and (top_share > 0.15 or (is_blue and top_share > 0.05)):
            keep |= region
    keep = ndimage.binary_closing(keep, iterations=2)
    return _fit(keep.astype(np.float32), (h, w))


def _refine(m, guide, r=6, eps=1e-3):
    """Guided filter: soft edges that follow the photo's own edges."""
    if not m.any():
        return m
    box = lambda x: ndimage.uniform_filter(x, 2 * r + 1, mode="reflect")
    mi, mp = box(guide), box(m)
    cov = box(guide * m) - mi * mp
    var = box(guide * guide) - mi * mi
    a = cov / (var + eps)
    b = mp - a * mi
    return np.clip(box(a) * guide + box(b), 0, 1).astype(np.float32)


def _compute(path):
    from . import engine, recipes
    im = engine.render_preview(path, recipes.load_look("neutral"), edge=EDGE)
    rgb = np.asarray(im, np.float32) / 255
    guide = rgb @ np.float32([0.2126, 0.7152, 0.0722])
    found = _vision(im)
    found["sky"] = _sky(rgb) * (1 - np.clip(found["subject"] * 2, 0, 1))   # people against the sky aren't sky
    out = {}
    for kind, m in found.items():
        m = _refine(m, guide)
        Image.fromarray((m * 255 + 0.5).astype(np.uint8)).save(_file(path, kind))
        out[kind] = m
    return out


def get(path, kind):
    """The whole-frame mask (float32 0..1 at about 1536 px, upright, before crop), found if needed."""
    f = _file(path, kind)
    with _lock:
        if not f.exists():
            _compute(path)
    return np.asarray(Image.open(f), np.float32) / 255


def coverage(path, kind):
    return float(get(path, kind).mean())


# --- using them ---------------------------------------------------------------------------------

def _edited(path, kind, geom):
    g = geometry.normalise(geom)
    p = Path(path)
    key = (str(p), p.stat().st_mtime_ns, kind, geometry.key(g))
    if key not in _mem:
        m = get(path, kind)
        if not geometry.is_identity(g):
            m = geometry.apply(np.repeat(m[..., None], 3, axis=-1), g)[..., 0]
        if len(_mem) > 24:
            _mem.pop(next(iter(_mem)))
        _mem[key] = Image.fromarray(np.ascontiguousarray(m, np.float32), "F")
    return _mem[key]


# --- masks you draw ----------------------------------------------------------------------------------
# Brush: a small grey picture the page paints (the whole final, cropped frame, any size up to 2048 px);
# it is stored by content (a short id), so every stroke is a new id and Undo just goes back to the old one.
# Linear / radial gradients are a few numbers (fractions of the final frame). They live in the library
# folder or the grade itself, never next to your photos.
_brush_mem = {}


def _brush_dir():
    d = _dir() / "brush"
    d.mkdir(exist_ok=True)
    return d


def save_brush(data):
    """Keep a painted mask (PNG bytes from the page); returns its id."""
    import io
    im = Image.open(io.BytesIO(data)).convert("L")
    if max(im.size) > 2048:
        k = 2048 / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BILINEAR)
    bid = hashlib.sha1(im.tobytes() + str(im.size).encode()).hexdigest()[:16]
    f = _brush_dir() / f"{bid}.png"
    if not f.exists():
        im.save(f, "PNG", optimize=True)
    return bid


def brush_file(bid):
    import re
    if not re.fullmatch(r"[0-9a-f]{16}", str(bid or "")):
        return None
    f = _brush_dir() / f"{bid}.png"
    return f if f.exists() else None


def _brush_img(bid):
    if bid not in _brush_mem:
        f = brush_file(bid)
        if f is None:
            return None
        if len(_brush_mem) > 24:
            _brush_mem.pop(next(iter(_brush_mem)))
        _brush_mem[bid] = Image.fromarray(np.asarray(Image.open(f).convert("L"), np.float32) / 255, "F")
    return _brush_mem[bid]


def _smooth(t):
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def _brush_remapped(img, m, frame_size, box, geom, src_size):
    """A painting made under another crop / straighten (its m['geom']), read through the photo's current one:
    each pixel of the current frame is traced back to the photo and on to where it was in the painted frame."""
    from .dng import _bilinear
    fw, fh = frame_size
    x0, y0, x1, y1 = box
    X = ((x0 + 0.5 + np.arange(max(1, x1 - x0))) / fw)[None, :]
    Y = ((y0 + 0.5 + np.arange(max(1, y1 - y0))) / fh)[:, None]
    X, Y = np.broadcast_arrays(X, Y)
    sx, sy = geometry.frame_to_source(X, Y, *src_size, geom)
    u, v = geometry.source_to_frame(sx, sy, *src_size, m["geom"])
    a = np.asarray(img, np.float32)
    out = _bilinear(a, (u * img.width - 0.5).astype(np.float32), (v * img.height - 0.5).astype(np.float32))
    out[(u < 0) | (u > 1) | (v < 0) | (v > 1)] = 0          # outside what was painted
    return out.astype(np.float32)


def _shape_alpha(m, frame_size, box, geom=None, src_size=None):
    fw, fh = frame_size
    x0, y0, x1, y1 = box
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    if m["kind"] == "brush":
        img = _brush_img(m.get("id"))
        if img is None:
            return np.zeros((h, w), np.float32)
        if m.get("geom") and src_size and not geometry.same(m["geom"], geom):
            return _brush_remapped(img, m, frame_size, box, geom, src_size)
        sx, sy = img.width / fw, img.height / fh
        return np.asarray(img.resize((w, h), Image.BILINEAR, box=(x0 * sx, y0 * sy, x1 * sx, y1 * sy)), np.float32)
    X = ((x0 + 0.5 + np.arange(w, dtype=np.float32)) / fw)[None, :]
    Y = ((y0 + 0.5 + np.arange(h, dtype=np.float32)) / fh)[:, None]
    if m["kind"] == "linear":                              # full effect at the first point, none at the second
        ax, ay, bx, by = m.get("x0", 0.5), m.get("y0", 0.3), m.get("x1", 0.5), m.get("y1", 0.7)
        dx, dy = bx - ax, by - ay
        t = ((X - ax) * dx + (Y - ay) * dy) / max(dx * dx + dy * dy, 1e-6)
        return (1 - _smooth(t)).astype(np.float32)
    rx, ry = max(m.get("rx", 0.35), 0.01), max(m.get("ry", 0.35), 0.01)      # radial: inside full, fading out
    r = np.hypot((X - m.get("cx", 0.5)) / rx, (Y - m.get("cy", 0.5)) / ry)
    inner = 1 - min(max(m.get("feather", 0.5), 0.0), 1.0)
    return (1 - _smooth((r - inner) / max(1 - inner, 1e-3))).astype(np.float32)


def alphas(path, recipe, geom, frame_size, box):
    """Per-pixel strength of each of the recipe's masks for `box` = (x0, y0, x1, y1) in a final frame
    of frame_size = (w, h) pixels: [float32 (y1 - y0, x1 - x0)], in recipe['masks'] order."""
    out = []
    fw, fh = frame_size
    x0, y0, x1, y1 = box
    src = None
    for m in recipe.get("masks") or []:
        if m["kind"] in SHAPES:
            if m.get("geom") and src is None:
                from . import raw
                src = raw.full_size(path)
            a = _shape_alpha(m, frame_size, box, geom, src)
        else:
            img = _edited(path, m["kind"], geom)
            sx, sy = img.width / fw, img.height / fh
            a = np.asarray(img.resize((max(1, x1 - x0), max(1, y1 - y0)), Image.BILINEAR,
                                      box=(x0 * sx, y0 * sy, x1 * sx, y1 * sy)), np.float32)
        out.append(1 - a if m.get("invert") else a)
    return out


def overlay(path, kind, geom, width, invert=False):
    """RGBA picture of a mask for the Grade view: red where it applies."""
    img = _edited(path, kind, geom)
    h = max(1, round(width * img.height / img.width))
    a = np.asarray(img.resize((width, h), Image.BILINEAR), np.float32)
    if invert:
        a = 1 - a
    rgba = np.zeros((h, width, 4), np.uint8)
    rgba[..., 0] = 255
    rgba[..., 1] = 40
    rgba[..., 2] = 40
    rgba[..., 3] = (a * 150).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def brush_view(bid, m_geom, geom, src_size, edge=1024):
    """The painting as it lies on the photo's current frame (for the page to show and keep painting on)."""
    img = _brush_img(bid)
    if img is None:
        return None
    fw, fh = geometry.output_size(*src_size, geom)
    k = edge / max(fw, fh)
    w, h = max(1, round(fw * k)), max(1, round(fh * k))
    a = _shape_alpha({"kind": "brush", "id": bid, "geom": m_geom}, (w, h), (0, 0, w, h), geom, src_size)
    return Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), "L")


def refit(masks_in, old_geom, new_geom, src_size):
    """Move gradients (numbers in fractions of the frame) from one crop / straighten to another, so they stay
    on the same part of the photo. Paintings keep their own geom and are traced at render time."""
    W, H = src_size
    ofw, ofh = geometry.output_size(W, H, old_geom)
    nfw, nfh = geometry.output_size(W, H, new_geom)

    def move(x, y):
        sx, sy = geometry.frame_to_source(np.array([x]), np.array([y]), W, H, old_geom)
        u, v = geometry.source_to_frame(sx, sy, W, H, new_geom)
        return float(u[0]), float(v[0])
    out = []
    for m in masks_in:
        m = dict(m)
        if m.get("kind") == "linear":
            m["x0"], m["y0"] = move(m.get("x0", 0.5), m.get("y0", 0.3))
            m["x1"], m["y1"] = move(m.get("x1", 0.5), m.get("y1", 0.7))
        elif m.get("kind") == "radial":
            cx, cy, rx, ry = m.get("cx", 0.5), m.get("cy", 0.5), m.get("rx", 0.35), m.get("ry", 0.35)
            ncx, ncy = move(cx, cy)
            px, py = move(cx + rx, cy)                 # radii: lengths in pixels, back to fractions of the new frame
            qx, qy = move(cx, cy + ry)
            m["cx"], m["cy"] = ncx, ncy
            m["rx"] = float(np.hypot((px - ncx) * nfw, (py - ncy) * nfh) / nfw)
            m["ry"] = float(np.hypot((qx - ncx) * nfw, (qy - ncy) * nfh) / nfh)
        elif m.get("kind") == "brush" and not m.get("geom"):
            m["geom"] = geometry.normalise(old_geom)  # painted before paintings remembered their crop
        out.append(m)
    return out
