"""A print-ready book / zine PDF of an essay.

Pages follow the essay's spreads: page 1 alone on the right, then facing pairs (2-3, 4-5 …),
exactly as the Spreads view shows. Each photo is placed at 300 dpi from your best version: the
largest graded JPG, else the RAW with your Grade-tab grade (or the essay's look) and its crop.
Optional: a cover, captions (your titles), page numbers, an index page, and 3 mm print bleed.

The PDF is written page by page (each page a high-quality JPEG), so a 60-page book never needs
more than one page in memory. Saved under ~/Pictures/Film Look Studio Essays; originals untouched.
"""
import io
import time
from datetime import datetime
from pathlib import Path, PurePath

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import drives, essay, essay_out

DPI = 300
SIZES = {"a4-portrait": (210 / 25.4, 297 / 25.4), "a4-landscape": (297 / 25.4, 210 / 25.4),
         "8x10-portrait": (8.0, 10.0), "8x10-landscape": (10.0, 8.0)}
SIZE_NAMES = {"a4-portrait": "A4 portrait", "a4-landscape": "A4 landscape",
              "8x10-portrait": "8x10 portrait", "8x10-landscape": "8x10 landscape"}
BLEED = 3 / 25.4            # inches added on every side when bleed is on
PAPER = (255, 255, 255)
INK = (34, 34, 34)
GREY = (120, 120, 120)


# --- a small streaming PDF writer: one JPEG per page ------------------------------------------

class PdfWriter:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.offsets, self.pages = [], []
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self._reserve(2)                      # 1 = catalog, 2 = page tree (written at the end)

    def _reserve(self, n):
        self.offsets += [None] * n

    def _obj(self, num, body):
        self.offsets[num - 1] = self.f.tell()
        self.f.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

    def add_page(self, img, width_in, height_in, trim_in=0.0):
        """img: the whole page (incl. bleed) as RGB at DPI. trim_in: bleed to mark as TrimBox."""
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=94, subsampling=0, dpi=(DPI, DPI))
        data = buf.getvalue()
        base = len(self.offsets) + 1
        self._reserve(3)
        im_n, content_n, page_n = base, base + 1, base + 2
        self._obj(im_n, (f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
                         f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\n"
                         "stream\n").encode() + data + b"\nendstream")
        W, H = width_in * 72, height_in * 72
        content = f"q {W:.3f} 0 0 {H:.3f} 0 0 cm /Im0 Do Q".encode()
        self._obj(content_n, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        t = trim_in * 72
        trim = f" /TrimBox [{t:.3f} {t:.3f} {W - t:.3f} {H - t:.3f}]" if t else ""
        self._obj(page_n, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:.3f} {H:.3f}]{trim} "
                           f"/Resources << /XObject << /Im0 {im_n} 0 R >> >> /Contents {content_n} 0 R >>").encode())
        self.pages.append(page_n)

    def close(self):
        kids = " ".join(f"{n} 0 R" for n in self.pages)
        self._obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode())
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        xref = self.f.tell()
        self.f.write(f"xref\n0 {len(self.offsets) + 1}\n0000000000 65535 f \n".encode())
        for off in self.offsets:
            self.f.write(f"{off:010d} 00000 n \n".encode())
        self.f.write(f"trailer\n<< /Size {len(self.offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        self.f.close()


# --- pictures ---------------------------------------------------------------------------------

def _font(px, bold=False):
    for path in ("/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(path, max(8, int(px)), index=1 if bold else 0)
        except OSError:
            continue
    return ImageFont.load_default()


def _largest_export(con, photo_id, mounts):
    for r in con.execute("""SELECT drive_id, rel_path FROM files WHERE photo_id=? AND role='export' AND kind='jpg'
                            AND missing=0 ORDER BY size DESC""", (photo_id,)):
        if r["drive_id"] in mounts:
            p = Path(mounts[r["drive_id"]]) / r["rel_path"]
            if p.exists():
                return p
    return None


def photo_image(con, item, recipe, long_edge):
    """The photo at print size: the largest graded JPG, else the RAW with its grade + crop."""
    from .. import edits, engine
    mounts = drives.online_mounts(con)
    exp = _largest_export(con, item["photo_id"], mounts)
    if exp is not None:
        im = Image.open(exp)
        im.draft("RGB", (long_edge, long_edge))
        im = ImageOps.exif_transpose(im).convert("RGB")
    else:
        src = Path(item["src"])
        im = Image.fromarray(engine.render_full(src, essay_out._own_recipe(src) or recipe, geom=edits.get(src)))
    if max(im.size) > long_edge:
        im.thumbnail((long_edge, long_edge), Image.LANCZOS)
    return im


def _fit(im, w, h):
    k = min(w / im.width, h / im.height)
    return im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)


def _cover_crop(im, w, h):
    return ImageOps.fit(im, (w, h), Image.LANCZOS, centering=(0.5, 0.5))


def _text_fit(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


# --- the book ---------------------------------------------------------------------------------

def make_book(con, essay_id, size="a4-portrait", layout="border", cover=True, captions=True,
              page_numbers=True, index=True, bleed=False, cover_photo=None, job=None,
              cancelled=lambda: False, poll=3.0):
    """Write the book PDF; returns its path (None if cancelled). Waits while a drive is offline."""
    from .. import recipes
    job = job if job is not None else {}
    e = con.execute("SELECT name, look FROM essays WHERE id=?", (essay_id,)).fetchone()
    if size not in SIZES:
        raise ValueError(f"unknown size {size}")
    wi, hi = SIZES[size]
    b = BLEED if bleed else 0.0
    PW, PH = round((wi + 2 * b) * DPI), round((hi + 2 * b) * DPI)      # whole page incl. bleed, pixels
    bx = round(b * DPI)                                                 # bleed in pixels
    tw, th = PW - 2 * bx, PH - 2 * bx                                   # the trimmed page
    items = essay_out.plan(con, essay_id)
    if not items:
        raise ValueError("This essay has no photos yet.")
    job.update(total=len(items) + int(cover) + int(index), done=0, current="", waiting=[], folder=None, pdf=None)
    while not all(i["online"] for i in items):
        job["waiting"] = sorted({i["drive"] or "a drive" for i in items if not i["online"]})
        if cancelled():
            job["waiting"] = []
            return None
        time.sleep(poll)
        items = essay_out.plan(con, essay_id)
    job["waiting"] = []
    recipe = recipes.load_look(e["look"] or "neutral")
    meta = {r["id"]: r for r in con.execute(
        f"""SELECT p.id, p.capture_time, fo.rel_path AS folder FROM photos p JOIN folders fo ON fo.id = p.folder_id
            WHERE p.id IN ({','.join('?' * len(items))})""", [i["photo_id"] for i in items])}

    essay_out.OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = essay_out.OUT_DIR / f"{essay_out.safe_name(e['name'])} – book {SIZE_NAMES[size]}{' bleed' if bleed else ''}.pdf"
    pdf = PdfWriter(dst)
    short = min(tw, th)
    margin = round(short * 0.11)
    cap_px = round(short * 0.017)
    f_cap, f_num = _font(cap_px), _font(cap_px * 0.9)
    long_edge = max(PW, PH)

    def blank():
        return Image.new("RGB", (PW, PH), PAPER)

    def add(img):
        pdf.add_page(img, wi + 2 * b, hi + 2 * b, trim_in=b)

    # cover
    if cover:
        job["current"] = "cover"
        page, d = blank(), None
        pid = cover_photo if cover_photo in [i["photo_id"] for i in items] else items[0]["photo_id"]
        it = next(i for i in items if i["photo_id"] == pid)
        im = photo_image(con, it, recipe, long_edge)
        box_w, box_h = tw - 2 * margin, round((th - 2 * margin) * 0.72)
        ph = _fit(im, box_w, box_h)
        page.paste(ph, (bx + (tw - ph.width) // 2, bx + margin + (box_h - ph.height) // 2))
        d = ImageDraw.Draw(page)
        f_title, f_sub = _font(short * 0.055, True), _font(short * 0.022)
        ty = bx + margin + box_h + round(short * 0.05)
        title = _text_fit(d, e["name"], f_title, tw - 2 * margin)
        d.text((bx + tw // 2, ty), title, fill=INK, font=f_title, anchor="ma")
        years = sorted({meta[i["photo_id"]]["capture_time"][:4] for i in items
                        if i["photo_id"] in meta and meta[i["photo_id"]]["capture_time"]})
        sub = f"{len(items)} photographs" + (f" · {years[0]}" if len(years) == 1 else f" · {years[0]}–{years[-1]}" if years else "")
        d.text((bx + tw // 2, ty + round(short * 0.08)), sub, fill=GREY, font=f_sub, anchor="ma")
        add(page)
        job["done"] += 1

    # photo pages: page 1 is a right-hand page; even pages are on the left
    for n, it in enumerate(items, 1):
        if cancelled():
            pdf.close()
            return None
        job["current"] = f"page {n}: {it['file']}"
        im = photo_image(con, it, recipe, long_edge)
        page = blank()
        if layout == "full":
            page.paste(_cover_crop(im, PW, PH), (0, 0))
            d = ImageDraw.Draw(page)
        else:
            caption = (it["title"] or "").strip() if captions else ""
            room = round(cap_px * 2.6) if caption else 0
            ph = _fit(im, tw - 2 * margin, th - 2 * margin - room)
            x = bx + (tw - ph.width) // 2
            y = bx + margin + (th - 2 * margin - room - ph.height) // 2
            page.paste(ph, (x, y))
            d = ImageDraw.Draw(page)
            if caption:
                d.text((x, y + ph.height + round(cap_px * 0.9)), _text_fit(d, caption, f_cap, ph.width), fill=INK, font=f_cap)
        if page_numbers:
            right = n % 2 == 1
            px = bx + (tw - margin * 0.6 if right else margin * 0.6)
            ink = PAPER if layout == "full" else GREY
            d.text((px, bx + th - margin * 0.45), str(n), fill=ink, font=f_num, anchor="rs" if right else "ls")
        add(page)
        job["done"] += 1

    # index: every photo with its number, title, shoot and date
    pages_so_far = len(items)
    if index:
        job["current"] = "index"
        cols = 4 if tw < th else 6
        cell_w = (tw - 2 * margin) // cols
        thumb_h = round(cell_w * 0.62)
        row_h = thumb_h + cap_px * 4
        rows = max(1, (th - 2 * margin - cap_px * 4) // row_h)
        per = cols * rows
        f_head, f_small = _font(cap_px * 1.6, True), _font(cap_px * 0.85)
        feats = essay.ensure_features(con, [i["photo_id"] for i in items])
        for start in range(0, len(items), per):
            page = blank()
            d = ImageDraw.Draw(page)
            d.text((bx + margin, bx + margin), "Index" if start == 0 else "Index (continued)", fill=INK, font=f_head)
            for k, it in enumerate(items[start:start + per]):
                r_, c_ = divmod(k, cols)
                x = bx + margin + c_ * cell_w
                y = bx + margin + cap_px * 4 + r_ * row_h
                tn = (feats.get(it["photo_id"]) or {}).get("thumb")
                if tn and (essay.ESSAY_THUMBS / tn).exists():
                    t = Image.open(essay.ESSAY_THUMBS / tn).convert("RGB")
                    t = _fit(t, cell_w - cap_px, thumb_h)
                    page.paste(t, (x, y + thumb_h - t.height))
                m = meta.get(it["photo_id"]) or {}
                when = (m["capture_time"] or "")[:10] if m else ""
                place = PurePath(m["folder"]).name.strip() if m else ""
                label = f"{it['pos']}  {it['title'] or it['stem']}"
                d.text((x, y + thumb_h + cap_px * 0.5), _text_fit(d, label, f_small, cell_w - cap_px), fill=INK, font=f_small)
                d.text((x, y + thumb_h + cap_px * 1.8), _text_fit(d, " · ".join(v for v in (place, when) if v), f_small, cell_w - cap_px),
                       fill=GREY, font=f_small)
            add(page)
            pages_so_far += 1
        job["done"] += 1

    if pages_so_far % 2:                         # printers bind in pairs of pages: end on a left page
        add(blank())
    pdf.close()
    job["current"] = ""
    job["pdf"] = str(dst)
    return dst
