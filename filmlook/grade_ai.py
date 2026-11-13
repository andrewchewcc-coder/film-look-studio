"""Grade in plain English (Grade → "Describe the grade"): Claude turns a sentence into the Grade tab's settings.

Claude sees the photo as it looks now (1024 px, no EXIF), the current look and sliders, your looks
with their descriptions, and the slider ranges; it answers with a complete grade — look, sliders,
colour mixer and masks — which the page shows at once (Undo goes back). Every value is clamped to
the slider's range here, so an odd answer can never produce a broken grade.
"""
from . import claude_ai, edits, engine, recipes

# Same ranges as the Grade tab's sliders (index.html SLIDERS / MIX_RANGE / MASK_SLIDERS).
SLIDERS = {"strength": (0, 1), "exposure": (-2, 2), "warmth": (-50, 50), "tint": (-30, 30), "contrast": (-0.5, 0.5),
           "highlights": (-1, 1), "shadows": (-1, 1), "whites": (-0.3, 0.3), "blacks": (-0.15, 0.2),
           "saturation": (-0.6, 0.6), "tone": (-1, 1), "grain": (-0.5, 1), "vignette": (-0.3, 0.6),
           "noise": (0, 1), "color_noise": (0, 1), "sharpen": (0, 1)}
MIX = {"hue": (-30, 30), "sat": (-1, 1), "lum": (-1, 1)}
MASK = {"exposure": (-2, 2), "contrast": (-0.5, 0.5), "warmth": (-50, 50), "saturation": (-1, 1)}
BANDS = recipes.HSL_BANDS
MASK_KINDS = ("subject", "people", "sky")

SYSTEM = """You are a colour grader working alongside a street photographer in their own grading app. They describe the \
grade they want in plain words; you set the app's controls to get there, starting from what the photo looks like now.

The controls (all relative to the chosen look; 0 = the look as designed):
- look: one of the photographer's looks (id), or keep the current one. Change it only if the request clearly calls \
for a different film character (e.g. "make it black and white", "more like the Superia 400-like look").
- strength 0..1 (how much of the look), exposure in EV, warmth −50..50 (+ = warmer), tint −30..30 (+ = magenta), \
contrast, highlights/shadows (−1..1, + = brighter), whites, blacks (− = crushed, + = faded/matte), saturation, \
tone (split-tone strength of the look), grain, vignette (+ = darker corners), noise / color_noise / sharpen 0..1.
- colour mixer: per colour band (red orange yellow green aqua blue purple magenta) hue in degrees, sat and lum −1..1. \
Use it for one colour only ("calm the blue sign", "deeper sky", "warmer skin" = orange).
- masks: local adjustments on the subject, people or sky (exposure, contrast, warmth, saturation; invert = everything \
else). Use them for requests about one part of the photo ("brighten the face", "darken the sky").

Change only what the request asks for; keep every other value as it is now. Prefer small, tasteful moves — the \
photographer can push further. Return the complete set of values (changed and unchanged). Always write \
`explanation` (never leave it empty): one or two plain sentences on what you changed and why, so they can fine-tune it."""


def _schema(look_ids):
    num = {"type": "number"}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["look", "sliders", "mixer", "masks", "explanation"],
        "properties": {
            "look": {"type": "string", "enum": look_ids},
            "sliders": {"type": "object", "additionalProperties": False, "required": list(SLIDERS),
                        "properties": {k: num for k in SLIDERS}},
            "mixer": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["band", "hue", "sat", "lum"],
                "properties": {"band": {"type": "string", "enum": list(BANDS)}, "hue": num, "sat": num, "lum": num}}},
            "masks": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "invert", *MASK],
                "properties": {"kind": {"type": "string", "enum": list(MASK_KINDS)}, "invert": {"type": "boolean"},
                               **{k: num for k in MASK}}}},
            "explanation": {"type": "string"},
        },
    }


def _clamp(v, lo_hi):
    lo, hi = lo_hi
    try:
        return round(max(lo, min(hi, float(v))), 3)
    except (TypeError, ValueError):
        return 0.0


def suggest(path, request, look, adjust):
    """Claude's grade for `request` as {"look", "adjust", "explanation", "cost"} (adjust in the page's shape)."""
    request = (request or "").strip()
    if not request:
        raise ValueError("Describe the grade you'd like first.")
    looks = recipes.list_looks()
    ids = ["neutral"] + [lk["id"] for lk in looks]
    look = look if look in ids else "neutral"
    adjust = adjust or {}
    try:
        recipe = recipes.apply_adjust(recipes.load_look(look), adjust)
    except KeyError:
        recipe = recipes.load_look(None)
    now = engine.render_preview(path, recipe, edge=claude_ai.PREVIEW_EDGE, geom=edits.get(path))
    current = {
        "look": look,
        "sliders": {k: adjust.get(k, 1.0 if k == "strength" else 0.0) for k in SLIDERS},
        "mixer": [{"band": b, **{k: (adjust.get("hsl") or {}).get(b, {}).get(k, 0) for k in MIX}}
                  for b in BANDS if (adjust.get("hsl") or {}).get(b)],
        "masks": [m for m in adjust.get("masks") or [] if m.get("kind") in MASK_KINDS],   # the ones Claude may change
    }
    looks_text = "\n".join(f"- {lk['id']}: {lk['name']}" + (f" — {lk['description']}" if lk["description"] else "")
                           for lk in looks)
    content = [
        {"type": "text", "text": "The photo as it looks now:"},
        claude_ai.preview_block(now),
        {"type": "text", "text": f"Available looks (plus 'neutral' = no look):\n{looks_text}\n\n"
                                 f"Current settings:\n{current}\n\nRequest: {request}\n\n"
                                 "Remember to explain your changes in `explanation`."},
    ]
    answer, cost = claude_ai.ask_json(SYSTEM, content, _schema(ids), effort="medium", max_tokens=8000)

    new = {k: _clamp(answer["sliders"].get(k), r) for k, r in SLIDERS.items()}
    new["curve"] = adjust.get("curve") or [[0, 0], [1, 1]]               # the curve stays yours
    new["hsl"] = {}
    for m in answer.get("mixer") or []:
        if m.get("band") in BANDS:
            band = {k: _clamp(m.get(k), r) for k, r in MIX.items()}
            band = {k: v for k, v in band.items() if v}
            if band:
                new["hsl"][m["band"]] = band
    new["masks"] = [{"kind": m["kind"], "invert": bool(m.get("invert")), **{k: _clamp(m.get(k), r) for k, r in MASK.items()}}
                    for m in (answer.get("masks") or [])[:6] if m.get("kind") in MASK_KINDS]
    new["masks"] += [m for m in adjust.get("masks") or [] if m.get("kind") in ("brush", "linear", "radial")]   # what you drew stays
    new_look = answer["look"] if answer.get("look") in ids else look
    explanation = (answer.get("explanation") or "").strip() or _summary(look, adjust, new_look, new, looks)
    return {"look": new_look, "adjust": new, "explanation": explanation, "cost": round(cost, 3)}


NAMES = {"exposure": "exposure", "warmth": "warmth", "tint": "tint", "contrast": "contrast", "highlights": "highlights",
         "shadows": "shadows", "whites": "whites", "blacks": "blacks", "saturation": "saturation", "tone": "toning",
         "grain": "grain", "vignette": "vignette", "strength": "look strength", "noise": "noise reduction",
         "color_noise": "colour noise", "sharpen": "sharpening"}


def _summary(old_look, old, new_look, new, looks):
    """What changed, in words — used when Claude's explanation comes back empty."""
    bits = []
    if new_look != old_look:
        bits.append("look → " + next((lk["name"] for lk in looks if lk["id"] == new_look), new_look))
    for k, name in NAMES.items():
        a, b = float(old.get(k, 1.0 if k == "strength" else 0.0) or 0), new.get(k, 0.0)
        if abs(b - a) > 1e-6:
            bits.append(f"{name} {'+' if b > a else '−'}{abs(b - a):g}")
    for band, v in new.get("hsl", {}).items():
        bits.append(f"{band} " + ", ".join(f"{k} {x:+g}" for k, x in v.items()))
    for m in new.get("masks", []):
        moves = ", ".join(f"{k} {m[k]:+g}" for k in MASK if m.get(k))
        if moves:
            bits.append(f"{m['kind']}{' (inverted)' if m['invert'] else ''} mask: {moves}")
    return ("Changed: " + "; ".join(bits) + ".") if bits else "No changes were needed."
