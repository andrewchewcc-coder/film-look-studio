"""Getting an essay out: a PDF contact sheet and the photos in order as 01_title.jpg.

Everything is written to ~/Pictures/Film Look Studio Essays (the internal disk), never next
to the originals. Graded photos keep their grade (the newest graded JPG, resized); photos not
graded yet are rendered from the RAW with the essay's look and their crop. The contact sheet
uses the cached thumbnails, so it works with the drives unplugged; the export waits for them.
"""
import re
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import drives, essay

OUT_DIR = Path.home() / "Pictures" / "Film Look Studio Essays"
SIZES = {"web": 2048, "large": 3000, "full": None, "small": 1080}


def safe_name(text, limit=60):
    """A file/folder name that works everywhere: letters, digits, spaces, - and _."""
    t = re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:limit].strip() or "untitled"


def file_name(index, count, title, stem):
    digits = max(2, len(str(count)))
    slug = safe_name(title or stem).replace(" ", "-")
    return f"{index:0{digits}d}_{slug}.jpg"


def plan(con, essay_id):
    """One entry per photo in order: where its full-size source is and whether it's online."""
    mounts = drives.online_mounts(con)
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM drives")}
    items = essay.items(con, essay_id)
    out = []
    for n, it in enumerate(items, 1):
        pid = it["photo_id"]
        prim = con.execute("""SELECT f.drive_id, f.rel_path, f.missing FROM photos p
                              JOIN files f ON f.id = p.primary_file_id WHERE p.id=?""", (pid,)).fetchone()
        exp = con.execute("""SELECT drive_id, rel_path FROM files WHERE photo_id=? AND role='export'
                             AND kind='jpg' AND missing=0 ORDER BY mtime DESC LIMIT 1""", (pid,)).fetchone()
        use = exp or prim
        kind = "graded" if exp else "raw"
        mount = mounts.get(use["drive_id"]) if use else None
        out.append({"pos": n, "photo_id": pid, "title": it["title"] or "", "kind": kind,
                    "stem": Path(prim["rel_path"]).stem if prim else str(pid),
                    "src": str(Path(mount) / use["rel_path"]) if mount else None,
                    "drive": names.get(use["drive_id"]) if use else None,
                    "online": bool(mount) and not (use is prim and prim["missing"]),
                    "file": file_name(n, len(items), it["title"], Path(prim["rel_path"]).stem if prim else str(pid))})
    return out


def new_folder(name, parent=None):
    """A fresh folder for this export ("Name", else "Name 2", "Name 3"…): never overwrites."""
    parent = parent or OUT_DIR
    base = safe_name(name)
    for k in range(1, 1000):
        d = parent / (base if k == 1 else f"{base} {k}")
        if not d.exists():
            d.mkdir(parents=True)
            return d
    raise RuntimeError("too many exports of this essay")


def _resize(im, edge):
    if edge and max(im.size) > edge:
        im = im.copy()
        im.thumbnail((edge, edge), Image.LANCZOS)
    return im


def export_one(item, recipe, edge, dst, quality=92):
    """Write one photo to `dst`. Graded JPGs are resized; RAWs are rendered with look + crop."""
    from .. import edits, engine
    src = Path(item["src"])
    if item["kind"] == "graded":
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    else:
        im = Image.fromarray(engine.render_full(src, recipe, geom=edits.get(src)))
    _resize(im, edge).save(dst, "JPEG", quality=quality, subsampling=0, icc_profile=engine._SRGB_ICC)
    engine.copy_metadata(src, dst)
    return dst


def run_export(con, essay_id, size="web", job=None, cancelled=lambda: False, poll=3.0):
    """Export every photo in order into a new folder. Waits (polling) while a drive is offline.
    `job` is a dict updated for the page: total, done, current, waiting, folder, outputs."""
    from .. import recipes
    job = job if job is not None else {}
    e = con.execute("SELECT name, look FROM essays WHERE id=?", (essay_id,)).fetchone()
    edge = SIZES.get(size, SIZES["web"])
    items = plan(con, essay_id)
    job.update(total=len(items), done=0, current="", waiting=[], outputs=[], folder=None)
    while not all(i["online"] for i in items):       # full size needs the drives: wait for them
        job["waiting"] = sorted({i["drive"] or "a drive" for i in items if not i["online"]})
        if cancelled():
            job["waiting"] = []
            return None
        time.sleep(poll)
        items = plan(con, essay_id)
    job["waiting"] = []
    recipe = recipes.load_look(e["look"] or "neutral")
    folder = new_folder(e["name"])
    job["folder"] = str(folder)
    for it in items:
        if cancelled():
            break
        job["current"] = it["file"]
        export_one(it, recipe, edge, folder / it["file"])
        job["outputs"].append(it["file"])
        job["done"] += 1
    job["current"] = ""
    return folder


# --- contact sheet ----------------------------------------------------------------------------

def _font(size, bold=False):
    for path in (["/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc"]):
        try:
            return ImageFont.truetype(path, size, index=1 if bold else 0)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(con, essay_id, dst=None, cols=4, rows=3, dpi=150):
    """A multi-page A4 landscape PDF: the photos in order with their numbers and titles."""
    e = con.execute("SELECT name, look FROM essays WHERE id=?", (essay_id,)).fetchone()
    items = plan(con, essay_id)
    feats = essay.ensure_features(con, [i["photo_id"] for i in items])
    W, H = int(11.69 * dpi), int(8.27 * dpi)
    margin, head, cap = int(0.45 * dpi), int(0.55 * dpi), int(0.42 * dpi)
    gap = int(0.18 * dpi)
    cw = (W - 2 * margin - (cols - 1) * gap) // cols
    ch = (H - 2 * margin - head - rows * cap - (rows - 1) * gap) // rows
    f_title, f_small, f_num = _font(int(0.2 * dpi), True), _font(int(0.1 * dpi)), _font(int(0.13 * dpi), True)
    per = cols * rows
    npages = max(1, -(-len(items) // per))
    pages = []
    for pg in range(npages):
        page = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(page)
        d.text((margin, margin), e["name"], fill="#111", font=f_title)
        info = f"{len(items)} photos  ·  {datetime.now():%d %b %Y}  ·  page {pg + 1} of {npages}"
        d.text((W - margin, margin + int(0.06 * dpi)), info, fill="#777", font=f_small, anchor="ra")
        for k, it in enumerate(items[pg * per:(pg + 1) * per]):
            r, c = divmod(k, cols)
            x = margin + c * (cw + gap)
            y = margin + head + r * (ch + cap + gap)
            thumb = (feats.get(it["photo_id"]) or {}).get("thumb")
            tp = essay.ESSAY_THUMBS / thumb if thumb else None
            if tp is None or not tp.exists():
                tp = essay.display_source(con, it["photo_id"])[1]
            if tp and Path(tp).exists():
                im = ImageOps.exif_transpose(Image.open(tp)).convert("RGB")
                im.thumbnail((cw, ch), Image.LANCZOS)
                page.paste(im, (x + (cw - im.width) // 2, y + (ch - im.height)))
            else:
                d.rectangle([x, y, x + cw, y + ch], outline="#ccc")
                d.text((x + cw // 2, y + ch // 2), "no preview", fill="#999", font=f_small, anchor="mm")
            ty = y + ch + int(0.06 * dpi)
            d.text((x, ty), f"{it['pos']:02d}", fill="#111", font=f_num)
            label = it["title"] or it["stem"]
            d.text((x + int(0.32 * dpi), ty + int(0.02 * dpi)), _fit(d, label, f_small, cw - int(0.34 * dpi)),
                   fill="#333" if it["title"] else "#888", font=f_small)
            d.text((x + int(0.32 * dpi), ty + int(0.17 * dpi)), "graded" if it["kind"] == "graded" else "",
                   fill="#a07a1e", font=f_small)
        pages.append(page)
    if dst is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        dst = OUT_DIR / f"{safe_name(e['name'])} – contact sheet.pdf"
    pages[0].save(dst, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])
    return Path(dst)


def _fit(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"
