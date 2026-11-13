"""Looks are JSON recipes. A recipe can `extend` another look and override any field."""
import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOKS_DIR = ROOT / "looks"
USER_LOOKS_DIR = LOOKS_DIR / "user"

HSL_BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]

DEFAULTS = {
    "name": "Neutral",
    "order": 100,
    "description": "",
    "strength": 1.0,
    "exposure": 0.0,
    "tone": {
        "contrast": 1.0,
        "pivot": 0.45,
        "shadows": 0.0,
        "highlights": 0.0,
        "black": 0.0,
        "white": 1.0,
    },
    "color": {
        "temp": 0.0,
        "tint": 0.0,
        "saturation": 1.0,
        "vibrance": 0.0,
        "hsl": {},
        "split": {},
    },
    "mono": {"enabled": False, "mix": [0.30, 0.59, 0.11]},
    "match": None,
    "grain": {"amount": 0.0, "size": 1.0},
    "vignette": {"amount": 0.0, "midpoint": 0.5},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "look"


def _look_path(look_id):
    for d in (USER_LOOKS_DIR, LOOKS_DIR):
        p = d / f"{look_id}.json"
        if p.exists():
            return p
    return None


def list_looks():
    """Return [{id, name, description, user}]: built-in looks (by `order`), then yours."""
    looks = []
    for d, user in ((LOOKS_DIR, False), (USER_LOOKS_DIR, True)):
        found = []
        for p in d.glob("*.json"):
            data = json.loads(p.read_text())
            found.append((data.get("order", 100), p.stem, {
                "id": p.stem, "name": data.get("name", p.stem),
                "description": data.get("description", ""), "user": user}))
        looks += [lk for *_, lk in sorted(found, key=lambda t: t[:2])]
    return looks


def load_look(look_id, _seen=None):
    """Load a look by id, resolving `extends` chains, merged over DEFAULTS."""
    if look_id in (None, "", "neutral"):
        return copy.deepcopy(DEFAULTS)
    _seen = _seen or set()
    if look_id in _seen:
        raise ValueError(f"circular 'extends' at {look_id}")
    _seen.add(look_id)
    p = _look_path(look_id)
    if p is None:
        raise KeyError(f"unknown look '{look_id}'")
    return resolve(json.loads(p.read_text()), _seen)


def resolve(recipe, _seen=None):
    """Merge a (possibly partial) recipe over its `extends` parent and DEFAULTS."""
    recipe = copy.deepcopy(recipe)
    parent = recipe.pop("extends", None)
    base = load_look(parent, _seen) if parent else copy.deepcopy(DEFAULTS)
    return deep_merge(base, recipe)


def parse_value(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def apply_sets(recipe, sets):
    """Apply `dotted.path=value` overrides, e.g. color.saturation=0.8."""
    recipe = copy.deepcopy(recipe)
    for s in sets or []:
        key, _, raw = s.partition("=")
        if not _:
            raise ValueError(f"override must look like key=value, got '{s}'")
        node = recipe
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parse_value(raw.strip())
    return recipe


def save_user_look(look_id, recipe):
    USER_LOOKS_DIR.mkdir(parents=True, exist_ok=True)
    p = USER_LOOKS_DIR / f"{slug(look_id)}.json"
    p.write_text(json.dumps(recipe, indent=2) + "\n")
    return p


ADJUST_KEYS = ("strength", "exposure", "warmth", "tint", "contrast", "highlights", "shadows", "whites", "blacks",
               "saturation", "grain", "vignette", "tone", "curve", "hsl", "noise", "color_noise", "sharpen", "masks")


def _mask_entry(m):
    """One local-adjustment mask, cleaned: its four sliders plus whatever its kind needs (a painted
    mask's id, a gradient's points)."""
    import re

    def num(k, d, lo, hi):
        try:
            return min(hi, max(lo, float(m.get(k, d))))
        except (TypeError, ValueError):
            return d
    out = {"kind": m["kind"], "invert": bool(m.get("invert")),
           **{k: float(m.get(k) or 0.0) for k in ("exposure", "contrast", "warmth", "saturation")}}
    if m["kind"] == "brush":
        bid = str(m.get("id") or "")
        out["id"] = bid if re.fullmatch(r"[0-9a-f]{16}", bid) else ""
        if isinstance(m.get("geom"), dict):        # the crop / straighten it was painted under
            from . import geometry
            out["geom"] = geometry.normalise(m["geom"])
    elif m["kind"] == "linear":
        out.update(x0=num("x0", 0.5, -1, 2), y0=num("y0", 0.3, -1, 2), x1=num("x1", 0.5, -1, 2), y1=num("y1", 0.7, -1, 2))
    elif m["kind"] == "radial":
        out.update(cx=num("cx", 0.5, -1, 2), cy=num("cy", 0.5, -1, 2), rx=num("rx", 0.35, 0.01, 3),
                   ry=num("ry", 0.35, 0.01, 3), feather=num("feather", 0.5, 0, 1))
    return out


def apply_adjust(recipe, adj):
    """Apply relative slider adjustments (all 0 = unchanged) on top of a resolved recipe.

    strength: absolute 0..1      exposure: +EV         warmth/tint: added to color.temp/tint
    contrast/saturation: +-fraction                     grain/vignette: added to amount
    highlights/shadows: added to tone.highlights/shadows (-1..1; extremes clip / crush)
    whites/blacks: added to tone.white/black (whites > 0 brighter, blacks > 0 faded/matte, < 0 crushed)
    curve: [[x, y], ...] points of your tone curve, applied after the look's tone (tone.user_curve)
    tone: scales the split-tone strength (e.g. the gold of Tri-X Gold), -1..1
    hsl: {band: {hue (degrees), sat, lum (-1..1)}}: the colour mixer -> color.user_hsl (sharper bands than
         the look's own color.hsl, applied after it)
    noise / color_noise / sharpen: 0..1, added to the recipe's `detail` (see detail.py)
    masks: [{kind: subject / people / sky, invert, exposure, contrast, warmth, saturation}] -> recipe
           `masks` (local adjustments, see masks.py and pipeline.local)
    """
    r = copy.deepcopy(recipe)
    curve = (adj or {}).get("curve")
    if isinstance(curve, list) and len(curve) >= 2:
        r["tone"]["user_curve"] = [[float(x), float(y)] for x, y in curve]
    ms = (adj or {}).get("masks")
    if isinstance(ms, list):
        from .masks import KINDS, SHAPES
        r["masks"] = [_mask_entry(m) for m in ms[:12] if isinstance(m, dict) and m.get("kind") in KINDS + SHAPES]
    hsl = (adj or {}).get("hsl")
    if isinstance(hsl, dict) and hsl:
        bands = dict(r["color"].get("user_hsl") or {})
        for band, v in hsl.items():
            if band in HSL_BANDS and isinstance(v, dict):
                cur = dict(bands.get(band) or {})
                for k in ("hue", "sat", "lum"):
                    cur[k] = cur.get(k, 0.0) + float(v.get(k) or 0.0)
                bands[band] = cur
        r["color"]["user_hsl"] = bands
    adj = {k: float(v) for k, v in (adj or {}).items()
           if k in ADJUST_KEYS and k not in ("curve", "hsl", "masks") and v not in (None, "")}
    if "strength" in adj:
        r["strength"] = max(0.0, min(1.0, adj["strength"]))
    r["exposure"] = r.get("exposure", 0.0) + adj.get("exposure", 0.0)
    r["color"]["temp"] = r["color"].get("temp", 0.0) + adj.get("warmth", 0.0)
    r["color"]["tint"] = r["color"].get("tint", 0.0) + adj.get("tint", 0.0)
    r["tone"]["contrast"] = r["tone"].get("contrast", 1.0) * (1 + adj.get("contrast", 0.0))
    r["tone"]["highlights"] = r["tone"].get("highlights", 0.0) + adj.get("highlights", 0.0)
    r["tone"]["shadows"] = r["tone"].get("shadows", 0.0) + adj.get("shadows", 0.0)
    r["tone"]["white"] = r["tone"].get("white", 1.0) + adj.get("whites", 0.0)
    r["tone"]["black"] = r["tone"].get("black", 0.0) + adj.get("blacks", 0.0)
    r["color"]["saturation"] = r["color"].get("saturation", 1.0) * (1 + adj.get("saturation", 0.0))
    r["grain"]["amount"] = max(0.0, r["grain"].get("amount", 0.0) + adj.get("grain", 0.0))
    r["vignette"]["amount"] = r["vignette"].get("amount", 0.0) + adj.get("vignette", 0.0)
    if any(adj.get(k) for k in ("noise", "color_noise", "sharpen")):
        d = dict(r.get("detail") or {})
        for k in ("noise", "color_noise", "sharpen"):
            d[k] = max(0.0, min(1.0, d.get(k, 0.0) + adj.get(k, 0.0)))
        r["detail"] = d
    if adj.get("tone"):
        for zone in (r["color"].get("split") or {}).values():
            zone["amount"] = max(0.0, zone.get("amount", 0.0) * (1 + adj["tone"]))
    return r
