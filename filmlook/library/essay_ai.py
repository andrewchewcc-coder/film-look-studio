"""The AI picture editor (Essays → Arrange → AI editor): Claude reviews an essay like a photobook editor.

Claude sees each photo once, as a 1024 px copy of what the essay shows (the graded JPG, else the RAW
with your grade or the essay's look) with no EXIF, numbered in essay order, plus the essay's name, the shoot folder
names and month/year — never GPS or camera data. The answer (verdicts, moves, title ideas,
captions, an intro draft) is kept in essays.source['ai_review'] so it survives a restart.
"""
import json
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .. import claude_ai
from . import essay, thumbs

MAX_PHOTOS = 60

SYSTEM = """You are an experienced photobook editor and picture editor for street photography, in the tradition of the \
editors behind books by Robert Frank, Saul Leiter, Alex Webb, Daido Moriyama and Martin Parr. A street photographer \
has sent you the photos of a photo essay in their current order, numbered from 1. Review it as you would in a real \
editing session: honest, specific, generous where it works, direct where it doesn't.

Look at: the opening and the ending; pairs and sequences that rhyme or clash on purpose; rhythm (busy vs quiet, \
near vs far, light vs dark); repetition and near-duplicates; weak frames that dilute the set; whether the essay \
has a point of view. Refer to photos only by their number. Describe only what is visible — don't invent names, \
places or stories. Moves must use the current numbering (before any other move). Keep notes short: one or two \
sentences each. Captions are short and plain (a few words, the way a photobook caption reads), not clichés. The \
intro is 60–120 words in a quiet first-person voice the photographer could edit and use."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall", "opening", "ending", "photos", "moves", "titles", "intro"],
    "properties": {
        "overall": {"type": "string"},
        "opening": {"type": "string"},
        "ending": {"type": "string"},
        "photos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["number", "verdict", "note", "caption"],
            "properties": {"number": {"type": "integer"},
                           "verdict": {"type": "string", "enum": ["keep", "maybe", "cut"]},
                           "note": {"type": "string"},
                           "caption": {"type": "string"}}}},
        "moves": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["number", "to_position", "why"],
            "properties": {"number": {"type": "integer"}, "to_position": {"type": "integer"},
                           "why": {"type": "string"}}}},
        "titles": {"type": "array", "items": {"type": "string"}},
        "intro": {"type": "string"},
    },
}


def _source(con, essay_id):
    r = con.execute("SELECT source FROM essays WHERE id=?", (essay_id,)).fetchone()
    return json.loads(r[0] or "{}") if r else {}


def _save_source(con, essay_id, src):
    con.execute("UPDATE essays SET source=?, updated_at=datetime('now') WHERE id=?", (json.dumps(src), essay_id))
    con.commit()


def saved(con, essay_id):
    return _source(con, essay_id).get("ai_review")


def _image(con, photo_id, look):
    """What the essay shows for this photo: the newest graded JPG; else the RAW rendered at 1024 px with
    your Grade-tab grade (or the essay's look) and crop; else the library thumbnail. None if unavailable."""
    from .. import edits, engine, recipes
    from . import drives, essay_out
    kind, path, _ = essay.display_source(con, photo_id)
    if kind == "graded" and path is not None and Path(path).exists():
        im = Image.open(path)
        im.draft("RGB", (2 * claude_ai.PREVIEW_EDGE, 2 * claude_ai.PREVIEW_EDGE))
        return ImageOps.exif_transpose(im).convert("RGB")
    r = con.execute("""SELECT f.drive_id, f.rel_path FROM photos p JOIN files f ON f.id = p.primary_file_id
                       WHERE p.id=? AND f.missing=0""", (photo_id,)).fetchone()
    mount = drives.online_mounts(con).get(r["drive_id"]) if r else None
    if mount:
        src = Path(mount) / r["rel_path"]
        try:
            recipe = essay_out._own_recipe(src) or recipes.load_look(look)
        except KeyError:
            recipe = recipes.load_look(None)
        return engine.render_preview(src, recipe, edge=claude_ai.PREVIEW_EDGE, geom=edits.get(src))
    t = con.execute("SELECT thumb FROM photos WHERE id=?", (photo_id,)).fetchone()
    p = thumbs.path(t[0]) if t and t[0] else None
    return Image.open(p).convert("RGB") if p is not None and Path(p).exists() else None


def _context(con, ids):
    rows = {r["id"]: r for r in con.execute(
        f"""SELECT p.id, p.capture_time, fo.rel_path AS folder FROM photos p JOIN folders fo ON fo.id = p.folder_id
            WHERE p.id IN ({','.join('?' * len(ids))})""", ids)}
    out = []
    for pid in ids:
        r = rows.get(pid)
        when = (r["capture_time"] or "")[:7] if r else ""
        folder = Path(r["folder"]).name.strip() if r else ""
        out.append((when, folder))
    return out


def plan(con, essay_id):
    """How many photos would go, and the rough cost — shown before asking."""
    n = min(len(essay.items(con, essay_id)), MAX_PHOTOS)
    return {"photos": n, "estimate": round(claude_ai.estimate(n), 2), "month": round(claude_ai.month_cost(), 2),
            "too_many": len(essay.items(con, essay_id)) > MAX_PHOTOS}


def review(con, essay_id):
    name, look = con.execute("SELECT name, look FROM essays WHERE id=?", (essay_id,)).fetchone()
    ids = [i["photo_id"] for i in essay.items(con, essay_id)][:MAX_PHOTOS]
    if len(ids) < 3:
        raise ValueError("An essay needs at least 3 photos for a review.")
    content = [{"type": "text", "text": f"Essay: “{name}”, {len(ids)} photos in their current order."}]
    sent = []
    for n, (pid, (when, folder)) in enumerate(zip(ids, _context(con, ids)), 1):
        im = _image(con, pid, look)
        if im is None:
            continue
        content.append({"type": "text", "text": f"Photo {n}" + (f" — {folder}, {when}" if folder or when else "")})
        content.append(claude_ai.preview_block(im))
        sent.append(n)
    if len(sent) < 3:
        raise RuntimeError("Too few of the photos are available (is the photo drive connected?).")
    content.append({"type": "text", "text": "Please review this essay. Give every photo above a verdict, note and caption; "
                                            "suggest only the moves that clearly help (none is fine); 3 title ideas."})
    answer, cost = claude_ai.ask_json(SYSTEM, content, SCHEMA)
    by_number = {n: pid for n, pid in enumerate(ids, 1)}
    for p in answer.get("photos", []):
        p["photo_id"] = by_number.get(p.get("number"))
    for m in answer.get("moves", []):
        m["photo_id"] = by_number.get(m.get("number"))
    result = {**answer, "order": ids, "sent": sent, "cost": round(cost, 3),
              "at": datetime.now().isoformat(timespec="minutes"), "model": claude_ai.MODEL}
    src = _source(con, essay_id)
    src["ai_review"] = result
    _save_source(con, essay_id, src)
    return result


def use_for_post(con, essay_id, field, text):
    """Put a title idea or the draft intro into the website post (Publish tab)."""
    if field not in ("title", "intro"):
        raise ValueError("unknown field")
    src = _source(con, essay_id)
    src.setdefault("publish", {})[field] = text
    _save_source(con, essay_id, src)
