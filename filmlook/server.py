"""Local browser app. Binds to 127.0.0.1 only; photos never leave your Mac."""
import json
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path, PurePath

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image
from pydantic import BaseModel

from . import edits, engine, geometry, lut, pipeline, raw, recipes, reference

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Film Look Studio")
state = {"folder": None, "collection": None}   # collection: {"label", "paths"} (e.g. cull picks)
jobs = {}
_thumbs = {}


def _photos():
    if state["collection"]:
        return [p for p in state["collection"]["paths"] if p.exists()]
    folder = state["folder"]
    try:
        return raw.list_images(folder) if folder else []
    except OSError:  # folder's drive was unplugged
        return []


def _photo(name):
    for p in _photos():
        if p.name == name:
            return p
    raise HTTPException(404, f"photo '{name}' not in the open folder")


def _recipe(look, adjust):
    try:
        r = recipes.load_look(look)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return recipes.apply_adjust(r, json.loads(adjust) if isinstance(adjust, str) else adjust)


def _jpeg(img, quality=88):
    return Response(engine.jpeg_bytes(img, quality), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/api/state")
def get_state():
    coll = state["collection"]
    return {"folder": str(state["folder"]) if state["folder"] and not coll else "",
            "collection": coll["label"] if coll else None,
            "photos": [p.name for p in _photos()], "looks": recipes.list_looks()}


class FolderIn(BaseModel):
    folder: str


def _exact_or_trimmed(text):
    """Folder names can end in a space ("2026 03 20 Fire Dragon Festival "): use the path as
    given if it exists, and only fall back to trimming spaces/quotes (pasted paths)."""
    exact = Path(text).expanduser()
    return exact if exact.exists() else Path(text.strip().strip("'\"")).expanduser()


@app.post("/api/folder")
def set_folder(body: FolderIn):
    folder = _exact_or_trimmed(body.folder)
    if not folder.is_dir():
        raise HTTPException(400, f"not a folder: {folder}")
    state["folder"] = folder.resolve()
    state["collection"] = None
    return get_state()


@app.get("/api/thumb")
def thumb(name: str):
    p = _photo(name)
    g = edits.get(p)
    k = (p, geometry.key(g))
    if k not in _thumbs:
        _thumbs[k] = engine.jpeg_bytes(engine.render_preview(p, recipes.load_look(None), edge=360, geom=g), 80)
    return Response(_thumbs[k], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/render")
def render(name: str, look: str = "neutral", adjust: str = "{}", edge: int = 1400, crop: str = "saved",
           turn: str = ""):
    """crop=saved applies the stored crop/straighten; crop=none shows the whole frame (before
    view); crop=turn applies only flips / 90-degree turns (given in `turn` as JSON, else the
    stored ones) — the crop screen draws rotation and the crop box itself."""
    p = _photo(name)
    if crop == "saved":
        g = edits.get(p)
    elif crop == "turn":
        t = json.loads(turn) if turn else edits.get(p)
        g = {"rot90": t.get("rot90", 0), "flip_h": t.get("flip_h", False), "flip_v": t.get("flip_v", False)}
    else:
        g = None
    return _jpeg(engine.render_preview(p, _recipe(look, adjust), edge=min(edge, engine.PREVIEW_EDGE), geom=g))


class CopyRatioIn(BaseModel):
    source: str
    names: list[str]


@app.post("/api/edit/copy-ratio")
def copy_ratio(body: CopyRatioIn):
    """Give each named photo the source photo's crop ratio (largest centred crop)."""
    src = _photo(body.source)
    g = edits.get(src)
    if not g["crop"]:
        raise HTTPException(400, "Crop this photo first, then copy its ratio.")
    w, h = raw.full_size(src)
    ow, oh = geometry.output_size(w, h, g)
    done = []
    for name in body.names:
        if name == body.source:
            continue
        p = _photo(name)
        pw, ph = raw.full_size(p)
        edits.put(p, geometry.with_ratio(edits.get(p), pw, ph, ow / oh, g["aspect"]))
        _lib().refresh_later(p)
        done.append(name)
    return {"updated": done, "ratio": f"{g['aspect']}" if g["aspect"] not in ("free", "original") else f"{ow}:{oh}"}


@app.get("/api/render_region")
def render_region(name: str, look: str = "neutral", adjust: str = "{}", x: float = 0, y: float = 0,
                  w: float = 1, h: float = 1, out_w: int = 1200):
    """A sharp tile of the graded, cropped frame for zooming (box as fractions of the frame)."""
    p = _photo(name)
    img = engine.render_region(p, _recipe(look, adjust), edits.get(p), [x, y, w, h], min(max(out_w, 16), 3000))
    return _jpeg(img, 90)


@app.get("/api/edit")
def get_edit(name: str):
    p = _photo(name)
    w, h = raw.full_size(p)
    return {"geometry": edits.get(p), "full_size": [w, h]}


class EditIn(BaseModel):
    name: str
    geometry: dict | None = None


@app.post("/api/edit")
def put_edit(body: EditIn):
    p = _photo(body.name)
    g = edits.put(p, body.geometry)
    _lib().refresh_later(p)            # library thumbnail + search data follow the crop
    w, h = raw.full_size(p)
    return {"geometry": g, "full_size": [w, h], "output_size": geometry.output_size(w, h, g)}


class ExportIn(BaseModel):
    names: list[str]
    look: str = "neutral"
    adjust: dict = {}
    quality: int = 95


@app.post("/api/export")
def export(body: ExportIn):
    recipe = _recipe(body.look, body.adjust)
    files = [_photo(n) for n in body.names]
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"total": len(files), "done": 0, "current": "", "outputs": [], "error": None}

    def work():
        job = jobs[job_id]
        try:
            for p in files:
                job["current"] = p.name
                job["outputs"].append(str(engine.export(p, recipe, quality=body.quality, geom=edits.get(p))))
                job["done"] += 1
        except Exception as e:  # report to the UI instead of dying silently
            job["error"] = str(e)
        job["current"] = ""
        try:
            _lib().start_scan()           # quick: only the new exports are read; marks them graded
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return jobs[job_id]


class SaveIn(BaseModel):
    name: str
    look: str = "neutral"
    adjust: dict = {}


@app.post("/api/looks")
def save_look(body: SaveIn):
    if not body.name.strip():
        raise HTTPException(400, "give the look a name")
    r = _recipe(body.look, body.adjust)
    r["name"] = body.name.strip()
    r["description"] = f"Your tweak of {recipes.load_look(body.look)['name']}"
    path = recipes.save_user_look(body.name, r)
    return {"id": path.stem, "looks": recipes.list_looks()}


@app.post("/api/reference")
async def from_reference(image: UploadFile = File(...), name: str = Form(...), base: str = Form("")):
    suffix = Path(image.filename or "ref.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await image.read())
    try:
        look_id, _ = reference.make_look(tmp.name, name.strip() or "Reference match",
                                         base_look=base if base and base != "neutral" else None,
                                         label=image.filename)
    except Exception as e:
        raise HTTPException(400, f"couldn't read that image: {e}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return {"id": look_id, "looks": recipes.list_looks()}


@app.get("/api/lut")
def get_lut(look: str = "neutral", adjust: str = "{}", name: str = ""):
    recipe = _recipe(look, adjust)
    match_src = None
    if recipe.get("match") and name:
        p = _photo(name)
        lin, gain = engine.preview_linear(p)
        match_src = pipeline.match_source_stats(lin, recipe, gain, raw.is_display_referred(p))
    fname = f"{recipes.slug(recipe.get('name', 'look'))}.cube"
    out = Path(tempfile.gettempdir()) / fname
    lut.write_cube(recipe, out, match_src=match_src)
    return FileResponse(out, filename=fname, media_type="text/plain")


# --- Library: searchable archive ----------------------------------------------------

def _lib():
    from .library import service
    return service


@app.get("/api/library/state")
def library_state():
    from .library import scan, search
    lib = _lib()
    c = lib.con()
    return {"roots": scan.list_roots(c), "summary": scan.summary(c), "facets": search.facets(c),
            "job": lib.job}


class RootIn(BaseModel):
    path: str


@app.post("/api/library/roots")
def library_add_root(body: RootIn):
    from .library import scan
    try:
        scan.add_root(_lib().con(), str(_exact_or_trimmed(body.path)))
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    return library_state()


@app.delete("/api/library/roots/{root_id}")
def library_remove_root(root_id: int):
    from .library import scan
    if _lib().job["running"]:
        raise HTTPException(409, "Wait for the scan to finish (or pause it) first.")
    scan.remove_root(_lib().con(), root_id)
    return library_state()


@app.get("/api/library/browse")
def library_browse(path: str = ""):
    from .library import scan
    try:
        return scan.browse(path or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ScanIn(BaseModel):
    root_ids: list[int] | None = None


@app.post("/api/library/scan")
def library_scan(body: ScanIn = ScanIn()):
    return _lib().start_scan(body.root_ids)


@app.post("/api/library/pause")
def library_pause():
    return _lib().pause()


@app.get("/api/library/search")
def library_search(q: str = "", cats: str = "street,travel", year: str = "", date_from: str = "",
                   date_to: str = "", camera: str = "", graded: str = "", folder_id: int = 0,
                   flag: str = "", stars: int = 0, offset: int = 0, limit: int = 200):
    from .library import search
    return search.search(_lib().con(), q, limit=min(limit, 500), offset=offset,
                         categories=[c for c in cats.split(",") if c] or None, year=year or None,
                         date_from=date_from or None, date_to=date_to or None, camera=camera or None,
                         graded=None if graded == "" else graded == "1", folder_id=folder_id or None,
                         flag=flag or None, stars=stars or None)


@app.get("/api/library/thumb/{photo_id}")
def library_thumb(photo_id: int):
    from .library import thumbs
    row = _lib().con().execute("SELECT thumb FROM photos WHERE id=?", (photo_id,)).fetchone()
    p = thumbs.path(row["thumb"]) if row and row["thumb"] else None
    if not p or not p.exists():
        raise HTTPException(404, "no thumbnail yet")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/library/photo/{photo_id}")
def library_photo(photo_id: int):
    from .library import search
    p = search.photo(_lib().con(), photo_id)
    if not p:
        raise HTTPException(404, "unknown photo")
    return p


@app.post("/api/library/open/{photo_id}")
def library_open(photo_id: int):
    """Open a library photo in the grading view (its folder, with it selected)."""
    from .library import search
    p = search.photo(_lib().con(), photo_id)
    if not p:
        raise HTTPException(404, "unknown photo")
    if not p["online"]:
        raise HTTPException(409, f"Drive offline: plug in “{p['drive']}” to open this photo.")
    src = Path(p["path"])
    if not (raw.is_raw(src) or raw.is_jpeg(src)):
        raise HTTPException(415, "This kind of file can't be graded here.")
    set_folder(FolderIn(folder=str(src.parent)))
    return {**get_state(), "select": src.name}


class LibMarkIn(BaseModel):
    flag: str | None = "keep-as-is"
    rating: int | None = -1


@app.post("/api/library/mark/{photo_id}")
def library_mark(photo_id: int, body: LibMarkIn):
    """Pick / reject / unmark and stars from the Library (same marks as Cull and Second look)."""
    from .library import marks
    kw = {}
    if body.flag != "keep-as-is":
        kw["flag"] = body.flag or None
    if body.rating is None or body.rating >= 0:
        kw["rating"] = body.rating or None
    try:
        return marks.set_mark(_lib().con(), photo_id, **kw)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/library/folders")
def library_folders():
    from .library import search
    return search.folder_categories(_lib().con())


class CategoryIn(BaseModel):
    category: str | None = None


@app.post("/api/library/folders/{folder_id}")
def library_set_folder(folder_id: int, body: CategoryIn):
    from .library import search
    try:
        search.set_folder_category(_lib().con(), folder_id, body.category)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# --- Second look: overlooked photos ------------------------------------------------------

@app.get("/api/second/state")
def second_state():
    from .library import secondlook
    c = _lib().con()
    folders = [{"id": r["id"], "name": PurePath(r["rel_path"]).name} for r in c.execute(
        """SELECT DISTINCT fo.id, fo.rel_path FROM folders fo JOIN photos p ON p.folder_id = fo.id
           WHERE p.graded = 0 ORDER BY fo.rel_path DESC""")]
    years = [r[0] for r in c.execute("""SELECT DISTINCT substr(capture_time, 1, 4) FROM photos
                                         WHERE capture_time IS NOT NULL ORDER BY 1 DESC""")]
    return {"counts": secondlook.counts(c), "folders": folders, "years": years,
            "measured": c.execute("SELECT COUNT(*) FROM quality").fetchone()[0]}


@app.get("/api/second/list")
def second_list(view: str = "gems", cats: str = "street,travel", year: str = "", date_from: str = "",
                date_to: str = "", folder_id: int = 0, offset: int = 0, limit: int = 20):
    from .library import secondlook
    if view not in ("gems", "this_day", "period", "shortlist"):
        raise HTTPException(400, "unknown view")
    return secondlook.suggestions(_lib().con(), view, categories=[c for c in cats.split(",") if c] or None,
                                  year=year or None, date_from=date_from or None, date_to=date_to or None,
                                  folder_id=folder_id or None, offset=offset, limit=min(limit, 100))


class DecideIn(BaseModel):
    decision: str | None = None


@app.post("/api/second/decide/{photo_id}")
def second_decide(photo_id: int, body: DecideIn):
    from .library import secondlook
    try:
        secondlook.decide(_lib().con(), photo_id, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "counts": secondlook.counts(_lib().con())}


# --- Cull ----------------------------------------------------------------------------------

def _cull():
    from .library import cull
    return cull


def _photo_file(photo_id):
    """(path or None if drive offline, kind, row) for a library photo."""
    from .library import drives
    c = _lib().con()
    r = c.execute("""SELECT f.drive_id, f.rel_path, f.kind, f.missing FROM photos p JOIN files f ON f.id = p.primary_file_id
                     WHERE p.id = ?""", (photo_id,)).fetchone()
    if not r:
        raise HTTPException(404, "unknown photo")
    mount = drives.online_mounts(c).get(r["drive_id"])
    return (Path(mount) / r["rel_path"] if mount and not r["missing"] else None), r["kind"], r


def _focus_point(feat):
    """Where to zoom by default: the eyes of the best face, else the main subject's centre."""
    if feat.get("face_box"):
        b = feat["face_box"]
        return [round(b[0] + b[2] / 2, 4), round(b[1] + b[3] * 0.38, 4)]
    if feat.get("subject_box"):
        b = feat["subject_box"]
        return [round(b[0] + b[2] / 2, 4), round(b[1] + b[3] / 2, 4)]
    return [0.5, 0.5]


@app.get("/api/cull/state")
def cull_state():
    c = _lib().con()
    cull = _cull()
    sessions = []
    for r in c.execute("SELECT id, label, created_at, finished_at FROM cull_sessions ORDER BY id DESC LIMIT 12"):
        g = c.execute("SELECT COUNT(*), COALESCE(SUM(done), 0) FROM cull_groups WHERE session_id=?", (r["id"],)).fetchone()
        rate, n = cull.session_agreement(c, r["id"])
        sessions.append({"id": r["id"], "label": r["label"], "created": r["created_at"], "finished": r["finished_at"],
                         "groups": g[0], "done": g[1], "agreement": rate, "agreement_groups": n})
    shoots = [{"id": r[0], "name": PurePath(r[1]).name, "photos": r[2]} for r in c.execute(
        """SELECT fo.id, fo.rel_path, COUNT(p.id) FROM folders fo JOIN photos p ON p.folder_id = fo.id
           GROUP BY fo.id ORDER BY fo.rel_path DESC""")]
    new = len(cull.scope_photo_ids(c, {"new": True}))
    return {"sessions": sessions, "shoots": shoots, "new_since_last": new, "learning": cull.learning_info(c)}


class CullStartIn(BaseModel):
    scope: dict
    label: str = ""


@app.post("/api/cull/start")
def cull_start(body: CullStartIn):
    try:
        sid = _cull().start_session(_lib().con(), body.scope, body.label or "Cull")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"session": sid}


@app.get("/api/cull/session/{sid}")
def cull_session(sid: int):
    from .library import drives
    c = _lib().con()
    s = c.execute("SELECT * FROM cull_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "unknown cull")
    groups = [{"idx": r["idx"], "ids": json.loads(r["photo_ids"]), "suggested": r["suggested"],
               "order": json.loads(r["order_ids"] or "[]"), "unsure": bool(r["unsure"]),
               "reasons": json.loads(r["reasons"] or "[]"), "done": bool(r["done"])}
              for r in c.execute("SELECT * FROM cull_groups WHERE session_id=? ORDER BY idx", (sid,))]
    ids = [i for g in groups for i in g["ids"]]
    mounts = drives.online_mounts(c)
    frames = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        qs = ",".join("?" * len(chunk))
        for r in c.execute(f"""SELECT p.id, p.capture_time, p.rating AS on1, f.rel_path, f.drive_id, f.missing,
                                      m.flag, m.rating, sc.features
                               FROM photos p JOIN files f ON f.id = p.primary_file_id
                               LEFT JOIN marks m ON m.photo_id = p.id LEFT JOIN scene sc ON sc.photo_id = p.id
                               WHERE p.id IN ({qs})""", chunk):
            feat = json.loads(r["features"]) if r["features"] else {}
            frames[r["id"]] = {"id": r["id"], "time": r["capture_time"], "name": PurePath(r["rel_path"]).name,
                               "folder": PurePath(r["rel_path"]).parent.name, "on1": r["on1"],
                               "online": r["drive_id"] in mounts and not r["missing"], "flag": r["flag"],
                               "rating": r["rating"], "focus": _focus_point(feat)}
    first_open = next((k for k, g in enumerate(groups) if not g["done"]), 0)
    _warm_views([i for g in groups[first_open:] + groups[:first_open] for i in g["ids"]])
    return {"id": sid, "label": s["label"], "finished": s["finished_at"], "groups": groups, "frames": frames}


class MarkIn(BaseModel):
    session: int
    photo_id: int
    action: str
    value: int | None = None


@app.post("/api/cull/mark")
def cull_mark(body: MarkIn):
    try:
        _cull().mark(_lib().con(), body.session, body.photo_id, body.action, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class DoneIn(BaseModel):
    session: int
    idx: int
    done: bool = True


@app.post("/api/cull/done")
def cull_done(body: DoneIn):
    c = _lib().con()
    c.execute("UPDATE cull_groups SET done=? WHERE session_id=? AND idx=?", (int(body.done), body.session, body.idx))
    c.commit()
    return {"ok": True}


class FinishIn(BaseModel):
    session: int


@app.delete("/api/cull/session/{sid}")
def cull_delete(sid: int):
    _cull().delete_session(_lib().con(), sid)
    return cull_state()


class SendIn(BaseModel):
    session: int


@app.post("/api/cull/send")
def cull_send(body: SendIn):
    """Open this cull's picks in Grade, in culling order (across folders)."""
    c = _lib().con()
    s = c.execute("SELECT label FROM cull_sessions WHERE id=?", (body.session,)).fetchone()
    ids = _cull().session_picks(c, body.session)
    if not ids:
        raise HTTPException(400, "No picks in this cull yet — press P on the frames you want.")
    paths, offline = [], 0
    for pid in ids:
        path, _, _ = _photo_file(pid)
        if path is None:
            offline += 1
        elif raw.is_raw(path) or raw.is_jpeg(path):
            paths.append(path)
    if not paths:
        raise HTTPException(409, "The picks' drive is offline — plug it in to grade them.")
    state["collection"] = {"label": f"Picks · {s['label'] if s else 'cull'}", "paths": paths}
    return {**get_state(), "offline": offline}


@app.post("/api/cull/finish")
def cull_finish(body: FinishIn):
    c = _lib().con()
    c.execute("UPDATE cull_sessions SET finished_at=datetime('now') WHERE id=?", (body.session,))
    c.commit()
    rate, n = _cull().session_agreement(c, body.session)
    try:
        learned = _cull().retrain(c)           # learn from this cull for the next ones
    except Exception:
        learned = None
    ids = [i for r in c.execute("SELECT photo_ids FROM cull_groups WHERE session_id=?", (body.session,))
           for i in json.loads(r[0])]
    qs = ",".join("?" * len(ids)) or "NULL"
    counts = dict(c.execute(f"SELECT flag, COUNT(*) FROM marks WHERE photo_id IN ({qs}) AND flag IS NOT NULL GROUP BY flag", ids).fetchall())
    return {"agreement": rate, "agreement_groups": n, "picks": counts.get("pick", 0), "rejects": counts.get("reject", 0),
            "learned": learned}


VIEW_EDGE = 1400


def _view_file(photo_id):
    """Disk cache (internal disk) for large cull views; named after the source file version."""
    from .library import LIBRARY_DIR
    r = _lib().con().execute("""SELECT f.quick_hash FROM photos p JOIN files f ON f.id = p.primary_file_id
                                WHERE p.id = ?""", (photo_id,)).fetchone()
    d = LIBRARY_DIR / "views"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{photo_id}_{(r[0] if r else 'x')[:8]}.jpg"


def _make_view(photo_id):
    from .library import scene
    dst = _view_file(photo_id)
    if dst.exists():
        return dst
    path, kind, _ = _photo_file(photo_id)
    if path is None:
        return None
    im = scene.upright_preview(path, kind)
    im.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:6]}.tmp")   # unique: the background warm-up
    tmp.write_bytes(engine.jpeg_bytes(im, 85))                       # may be making the same view
    tmp.replace(dst)
    return dst


def _warm_views(ids):
    """Prepare every frame's large view in the background, in culling order."""
    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        try:
            _make_view(i)
        except Exception:
            pass

    def go():
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(one, ids))
    threading.Thread(target=go, daemon=True).start()


@app.get("/api/cull/view/{photo_id}")
def cull_view(photo_id: int):
    """A large view of the frame from the camera's preview (thumbnail if the drive is offline)."""
    dst = _view_file(photo_id)
    if not dst.exists():
        dst = _make_view(photo_id)
    if dst is None:
        return library_thumb(photo_id)
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


_full_lock = threading.Lock()


@app.get("/api/cull/full/{photo_id}")
def cull_full(photo_id: int, raw_decode: int = 0):
    """The whole frame at full size, for panning around at 100%. From the camera preview, or
    (raw_decode=1, when the preview is smaller than the sensor, e.g. Fuji) from the RAW with a
    quick demosaic. Cached on the internal disk (only the last few)."""
    from .library import scene
    path, kind, _ = _photo_file(photo_id)
    if path is None:
        raise HTTPException(409, "Drive offline — plug it in to zoom to 100%.")
    d = _view_file(photo_id).parent / "full"
    d.mkdir(exist_ok=True)
    dst = d / f"{_view_file(photo_id).stem}{'_raw' if raw_decode else ''}.jpg"
    fw, fh = raw.full_size(path)
    with _full_lock:                       # one big decode at a time
        if not dst.exists():
            if raw_decode and kind == "raw":
                rgb16 = raw.decode(path, fast=True)
                _, gain = engine.preview_linear(path)
                h = rgb16.shape[0]
                parts = [engine.to_image(pipeline.base(raw.to_float(rgb16[y:y + 512]), gain)) for y in range(0, h, 512)]
                im = Image.new("RGB", (rgb16.shape[1], h))
                for k, part in enumerate(parts):
                    im.paste(part, (0, k * 512))
            else:
                im = scene.upright_preview(path, kind, full=True)
            tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:6]}.tmp")
            im.save(tmp, "JPEG", quality=88)
            tmp.replace(dst)
            for old in sorted(d.glob("*.jpg"), key=lambda q: q.stat().st_mtime)[:-8]:
                old.unlink()               # keep the cache small
    with Image.open(dst) as im:
        w = im.width
    pct = round(100 * w / fw)
    can_raw = kind == "raw" and not raw_decode and pct < 95
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"X-Zoom": "100% (RAW)" if raw_decode else (f"{pct}% (camera preview)" if pct < 95 else "100%"),
                                 "X-Can-Full": "1" if can_raw else "0", "Cache-Control": "no-store"})


@app.get("/api/cull/zoom/{photo_id}")
def cull_zoom(photo_id: int, w: int = 1200, h: int = 800, full: int = 0):
    """100% crop centred on the face (or main subject). `full=1` decodes the RAW itself when the
    camera preview is smaller than the sensor (e.g. Fuji), for a true 100%."""
    from .library import scene
    path, kind, _ = _photo_file(photo_id)
    if path is None:
        raise HTTPException(409, "Drive offline — plug it in to zoom to 100%.")
    feat = _lib().con().execute("SELECT features FROM scene WHERE photo_id=?", (photo_id,)).fetchone()
    f = json.loads(feat[0]) if feat else {}
    box = f.get("face_box") or f.get("subject_box") or [0.4, 0.4, 0.2, 0.2]
    fw, fh = raw.full_size(path)
    eye_line = 0.38 if f.get("face_box") else 0.5          # faces: centre on the eyes

    def window(W, H):
        cx, cy = (box[0] + box[2] / 2) * W, (box[1] + box[3] * eye_line) * H
        x0 = int(max(0, min(W - w, cx - w / 2)))
        y0 = int(max(0, min(H - h, cy - h / 2)))
        return x0, y0, min(W, x0 + w), min(H, y0 + h)

    if full and kind == "raw":
        rgb16 = raw.decode(path, fast=True)                # full-res, lens-corrected, upright
        x0, y0, x1, y1 = window(rgb16.shape[1], rgb16.shape[0])
        _, gain = engine.preview_linear(path)              # tone only the visible crop
        crop = engine.to_image(pipeline.base(raw.to_float(rgb16[y0:y1, x0:x1]), gain))
        note = "100% (RAW)"
    else:
        im = scene.upright_preview(path, kind, full=True)
        pct = round(100 * im.width / fw)
        note = f"{pct}% (camera preview)" if pct < 95 else "100%"
        crop = im.crop(window(im.width, im.height))
    return Response(engine.jpeg_bytes(crop, 90), media_type="image/jpeg",
                    headers={"X-Zoom": note, "X-Can-Full": "1" if (kind == "raw" and not full and im.width < fw * 0.95) else "0",
                             "Cache-Control": "no-store"})


def run(folder=None, port=8765, open_browser=True):
    import uvicorn
    _lib().warm_model()
    if folder:
        set_folder(FolderIn(folder=folder))
    url = f"http://127.0.0.1:{port}"
    print(f"Film Look Studio running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
