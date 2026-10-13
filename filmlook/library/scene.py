"""What's in a frame, for culling: people, faces, eyes, body pose and the main subject.

Uses Apple's Vision framework (on-device, Neural Engine; via pyobjc), run on the photo's
embedded preview turned upright. All boxes are fractions of the upright frame, origin
top-left: [x, y, w, h].
"""
import io

import numpy as np
from PIL import Image

ANALYSIS_EDGE = 1600     # px on the long side handed to Vision
# Vision's internal joint keys -> plain names
JOINT_NAMES = {"head": "nose", "neck_1": "neck", "left_shoulder_1": "left_shoulder", "right_shoulder_1": "right_shoulder",
               "left_forearm": "left_elbow", "right_forearm": "right_elbow", "left_hand": "left_wrist",
               "right_hand": "right_wrist", "left_upleg": "left_hip", "right_upleg": "right_hip", "root": "root",
               "left_leg": "left_knee", "right_leg": "right_knee", "left_foot": "left_ankle", "right_foot": "right_ankle",
               "left_eye": "left_eye", "right_eye": "right_eye", "left_ear": "left_ear", "right_ear": "right_ear"}


def _nsdata(im):
    from Foundation import NSData
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=92)
    return NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))


def _box(bb):
    """Vision's normalised rect (origin bottom-left) -> [x, y, w, h] with origin top-left."""
    x, y, w, h = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
    return [round(float(x), 4), round(float(1 - y - h), 4), round(float(w), 4), round(float(h), 4)]


def _eye_openness(face_obs):
    """Eye height / width from the eye landmark contours (open ~0.25+, closed ~0.1)."""
    lm = face_obs.landmarks()
    if lm is None:
        return None
    vals = []
    for region in (lm.leftEye(), lm.rightEye()):
        if region is None or region.pointCount() < 4:
            continue
        pts = region.normalizedPoints()
        xs = [pts[i].x for i in range(region.pointCount())]
        ys = [pts[i].y for i in range(region.pointCount())]
        w = max(xs) - min(xs)
        if w > 1e-6:
            vals.append((max(ys) - min(ys)) / w)
    return round(float(np.mean(vals)), 3) if vals else None


_vision_lock = __import__("threading").Lock()


def analyse(im):
    """Run Vision on an upright PIL image. Returns a JSON-able dict. One call at a time."""
    with _vision_lock:
        return _analyse(im)


def _analyse(im):
    import Vision
    im = im.copy()
    im.thumbnail((ANALYSIS_EDGE, ANALYSIS_EDGE), Image.LANCZOS)
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(_nsdata(im), None)

    humans = Vision.VNDetectHumanRectanglesRequest.alloc().init()
    humans.setUpperBodyOnly_(False)
    faces = Vision.VNDetectFaceLandmarksRequest.alloc().init()
    pose = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
    salient = Vision.VNGenerateObjectnessBasedSaliencyImageRequest.alloc().init()
    ok, err = handler.performRequests_error_([humans, faces, pose, salient], None)
    if not ok:
        raise RuntimeError(f"Vision failed: {err}")

    face_obs = list(faces.results() or [])
    quality = {}
    if face_obs:
        fq = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        fq.setInputFaceObservations_(face_obs)
        handler.performRequests_error_([fq], None)
        for i, o in enumerate(fq.results() or []):
            quality[i] = float(o.faceCaptureQuality() or 0)

    out = {"size": list(im.size), "people": [], "faces": [], "poses": [], "subjects": []}
    for o in humans.results() or []:
        out["people"].append({"box": _box(o.boundingBox()), "conf": round(float(o.confidence()), 3)})
    for i, o in enumerate(face_obs):
        out["faces"].append({"box": _box(o.boundingBox()), "quality": round(quality.get(i, 0.0), 3),
                             "eyes": _eye_openness(o),
                             "yaw": round(float(o.yaw() or 0), 3) if o.yaw() is not None else None})
    for o in pose.results() or []:
        pts, _ = o.recognizedPointsForJointsGroupName_error_(Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None)
        joints = {}
        for key, p in (pts or {}).items():
            raw_name = str(key).replace("_joint", "").replace("VNHLK", "").lower()
            name = JOINT_NAMES.get(raw_name, raw_name)
            if p.confidence() > 0.2:
                joints[name] = [round(float(p.location().x), 4), round(float(1 - p.location().y), 4),
                                round(float(p.confidence()), 2)]
        if joints:
            out["poses"].append(joints)
    for o in salient.results() or []:
        for s in o.salientObjects() or []:
            out["subjects"].append({"box": _box(s.boundingBox()), "conf": round(float(s.confidence()), 3)})
    return out


# --- loading ----------------------------------------------------------------------------

def upright_preview(path, kind, full=False):
    """The camera's embedded preview (or the JPG itself), upright. Very large previews are
    decoded at half size unless `full` (100% zoom)."""
    import rawpy
    from PIL import ImageOps
    from .thumbs import RAW_FLIP
    if kind != "raw":
        im = Image.open(path)
        if max(im.size) > 5000 and not full:
            im.draft("RGB", (im.size[0] // 2, im.size[1] // 2))
        return ImageOps.exif_transpose(im).convert("RGB")
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        try:
            th = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            th = None
        if th is not None and th.format == rawpy.ThumbFormat.JPEG:
            im = Image.open(io.BytesIO(th.data))
            if max(im.size) > 5000 and not full:   # 60 MP Leica previews: half size is plenty, 4x faster
                im.draft("RGB", (im.size[0] // 2, im.size[1] // 2))
            if im.getexif().get(0x0112, 1) != 1:
                return ImageOps.exif_transpose(im).convert("RGB")
        elif th is not None and th.format == rawpy.ThumbFormat.BITMAP:
            im = Image.fromarray(th.data)
        else:
            return Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
    im = im.convert("RGB")
    return im.transpose(RAW_FLIP[flip]) if flip in RAW_FLIP else im


# --- street-photography features ----------------------------------------------------------

def _iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax2, bx2) - max(a[0], b[0])), max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1e-9)


def _clip(b):
    x, y = max(0.0, b[0]), max(0.0, b[1])
    return [x, y, max(0.0, min(1.0, b[0] + b[2]) - x), max(0.0, min(1.0, b[1] + b[3]) - y)]


def people(a):
    """One box per person, combining the person detector, poses and faces."""
    found = [p["box"] for p in a["people"] if p["conf"] > 0.3]
    for joints in a["poses"]:
        xs = [v[0] for v in joints.values()]
        ys = [v[1] for v in joints.values()]
        if len(xs) >= 4:
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            found.append(_clip([min(xs) - 0.15 * w, min(ys) - 0.15 * h - (0.1 * h if "nose" in joints else 0),
                                1.3 * w, 1.3 * h + (0.1 * h if "nose" in joints else 0)]))
    for f in a["faces"]:
        x, y, w, h = f["box"]
        found.append(_clip([x - w, y - 0.5 * h, 3 * w, 6 * h]))    # a face implies a body around it
    merged = []
    for b in sorted(found, key=lambda b: -b[2] * b[3]):
        if not any(_iou(b, m) > 0.3 or (b[0] >= m[0] and b[1] >= m[1] and b[0] + b[2] <= m[0] + m[2]
                                        and b[1] + b[3] <= m[1] + m[3]) for m in merged):
            merged.append(b)
    return merged


def main_subject(a, persons):
    """The box the photo is 'about': the most prominent person, else the top salient object."""
    if persons:
        def weight(b):
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            central = 1 - 0.6 * (abs(cx - 0.5) + abs(cy - 0.5))
            return b[2] * b[3] * central
        return max(persons, key=weight), "person"
    if a["subjects"]:
        return max(a["subjects"], key=lambda s: s["conf"] * s["box"][2] * s["box"][3])["box"], "object"
    return [0.25, 0.25, 0.5, 0.5], "centre"


def _crop(im, box, max_edge=900, pad=0.0):
    W, H = im.size
    x, y, w, h = box
    x0, y0 = int(max(0, (x - pad * w) * W)), int(max(0, (y - pad * h) * H))
    x1, y1 = int(min(W, (x + w + pad * w) * W)), int(min(H, (y + h + pad * h) * H))
    c = im.crop((x0, y0, max(x0 + 8, x1), max(y0 + 8, y1)))
    c.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return c


def _sharpness(im_crop):
    from .quality import measure_image
    g = im_crop.convert("L")
    if min(g.size) < 48:
        return None
    return measure_image(g)["sharpness"]


def _gesture(joints):
    """0..1 'moment' from one pose: stride and arm movement relative to body height."""
    def j(n):
        return joints.get(n)
    top = j("nose") or j("neck")
    bottom = [p for p in (j("left_ankle"), j("right_ankle")) if p]
    if not top or not bottom:
        height = None
    else:
        height = max(abs(top[1] - b[1]) for b in bottom)
    if not height or height < 0.05:
        ys = [v[1] for v in joints.values()]
        height = max(ys) - min(ys) or 1e-3
    stride = 0.0
    if j("left_ankle") and j("right_ankle"):
        stride = abs(j("left_ankle")[0] - j("right_ankle")[0]) / height
    arms = 0.0
    for side in ("left", "right"):
        w, s = j(f"{side}_wrist"), j(f"{side}_shoulder")
        if w and s:
            arms = max(arms, float(np.hypot(w[0] - s[0], w[1] - s[1])) / height)
            if w[1] < s[1]:                 # hand raised above the shoulder: an expressive moment
                arms += 0.25
    return float(min(1.0, 0.6 * min(stride, 0.6) / 0.6 + 0.4 * min(arms, 1.0)))


def features(a, preview):
    """Scores (0..1, higher is better unless noted) for culling, from an analysis + the upright
    full-size preview. Pure numbers; ranking and reasons live in cull.py."""
    from .. import color
    persons = people(a)
    subj, kind = main_subject(a, persons)
    out = {"subject_kind": kind, "subject_box": [round(v, 4) for v in subj], "people": len(persons)}

    # 1. Focus on the subject (and on the best face if there is one).
    out["subject_sharp"] = _sharpness(_crop(preview, subj))
    faces = sorted(a["faces"], key=lambda f: -f["box"][2] * f["box"][3])
    if faces:
        best = max(faces, key=lambda f: f["quality"] + 2 * f["box"][2] * f["box"][3])
        out["face_quality"] = best["quality"]
        out["eyes_open"] = best["eyes"]
        out["face_sharp"] = _sharpness(_crop(preview, best["box"], pad=0.3))
        out["face_box"] = best["box"]
        out["face_size"] = round(best["box"][2] * best["box"][3], 5)

    # 3. Gesture / moment: liveliest pose, plus people interacting (close together).
    poses = a["poses"]
    out["gesture"] = round(max((_gesture(p) for p in poses), default=0.0), 3)
    if len(persons) >= 2:
        cs = [(b[0] + b[2] / 2, b[1] + b[3] / 2, b[3]) for b in persons]
        close = sum(1 for i in range(len(cs)) for k in range(i + 1, len(cs))
                    if abs(cs[i][0] - cs[k][0]) < 0.6 * (cs[i][2] + cs[k][2]) / 2 and abs(cs[i][2] - cs[k][2]) < 0.5 * max(cs[i][2], cs[k][2]))
        out["interaction"] = round(min(1.0, close / 2), 3)
    else:
        out["interaction"] = 0.0

    # 4. Edges: people who matter, awkwardly cut by the frame edge (penalty), and clutter in the
    # border band. Background figures much smaller than the subject don't count; a frame ending
    # below the waist is normal framing; slicing someone at the side or cutting into a head is worst.
    cut = 0.0
    subj_area = max(subj[2] * subj[3], 1e-6) if kind == "person" else 0.05
    face_tops = [f["box"][1] for f in a["faces"]]
    for b in persons:
        area = b[2] * b[3]
        if area < 0.2 * subj_area:
            continue
        left, top, right, bottom = b[0] < 0.01, b[1] < 0.01, b[0] + b[2] > 0.99, b[1] + b[3] > 0.99
        weight = min(1.0, area / subj_area)
        if left or right:
            cut += 0.6 * weight
        if top and any(abs(ft - b[1]) < 0.05 for ft in face_tops):
            cut += 0.8 * weight                  # head cut off at the top
        if bottom and not (left or right):
            cut += 0.1 * weight
    out["edge_cut"] = round(min(1.0, cut), 3)            # 0 clean .. 1 bad
    g = np.asarray(preview.convert("L").resize((400, max(1, int(400 * preview.height / preview.width)))), np.float32) / 255
    e = np.abs(np.diff(g, axis=1))[:-1] + np.abs(np.diff(g, axis=0))[:, :-1]
    h, w = e.shape
    band = max(2, int(0.07 * min(h, w)))
    border = np.concatenate([e[:band].ravel(), e[-band:].ravel(), e[:, :band].ravel(), e[:, -band:].ravel()])
    inner = e[band:-band, band:-band]
    out["edge_clutter"] = round(float(border.mean() / max(inner.mean(), 1e-6)), 3)   # >1 = busier edges

    # 5. Separation: subject vs its surroundings (lightness and colour), in OKLab.
    small = np.asarray(preview.resize((300, max(1, int(300 * preview.height / preview.width)))), np.float32) / 255
    lab = color.linear_to_oklab(color.srgb_decode(small))
    H2, W2 = lab.shape[:2]
    x0, y0 = int(subj[0] * W2), int(subj[1] * H2)
    x1, y1 = max(x0 + 2, int((subj[0] + subj[2]) * W2)), max(y0 + 2, int((subj[1] + subj[3]) * H2))
    mask = np.zeros((H2, W2), bool)
    mask[y0:y1, x0:x1] = True
    ring = np.zeros_like(mask)
    pad = max(3, int(0.08 * min(H2, W2)))
    ring[max(0, y0 - pad):min(H2, y1 + pad), max(0, x0 - pad):min(W2, x1 + pad)] = True
    ring &= ~mask
    if mask.sum() > 4 and ring.sum() > 4:
        d = lab[mask].mean(0) - lab[ring].mean(0)
        out["separation"] = round(float(np.hypot(d[0] * 1.5, np.hypot(d[1], d[2]) * 3)), 4)
    else:
        out["separation"] = 0.0
    out["saliency"] = round(max((s["conf"] for s in a["subjects"]), default=0.0), 3)

    # 6. Exposure on the subject itself.
    sg = np.asarray(_crop(preview, subj, max_edge=300).convert("L"), np.float32) / 255
    out["subject_clipped"] = round(float(((sg > 0.985) | (sg < 0.015)).mean()), 4)
    return out


def measure(path, kind):
    """(analysis, features) for one photo file."""
    pv = upright_preview(path, kind)
    a = analyse(pv)
    return a, features(a, pv)
