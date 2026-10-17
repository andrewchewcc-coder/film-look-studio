"""Local browser app. Binds to 127.0.0.1 only; photos never leave your Mac."""
import json
import tempfile
import shutil
import threading
import uuid
import webbrowser
from pathlib import Path, PurePath

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, ImageOps
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
            "photos": [p.name for p in _photos()], "looks": recipes.list_looks(),
            "edited": edits.edited_names(_photos()), "exported": edits.exported_names(_photos())}


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


_warm_queue = []
_warm_lock = threading.Lock()


class WarmIn(BaseModel):
    names: list[str]


@app.post("/api/warm")
def warm(body: WarmIn):
    """Decode the next photos in the background, so clicking them shows the preview at once."""
    paths = []
    for n in body.names[:4]:
        try:
            paths.append(_photo(n))
        except HTTPException:
            pass
    with _warm_lock:
        _warm_queue[:] = paths                # only the latest request matters
        start = not getattr(warm, "running", False)
        warm.running = True
    if start:
        def work():
            while True:
                with _warm_lock:
                    if not _warm_queue:
                        warm.running = False
                        return
                    p = _warm_queue.pop(0)
                try:
                    engine.preview_linear(p)
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


class WarmFullIn(BaseModel):
    name: str


@app.post("/api/warm_full")
def warm_full(body: WarmFullIn):
    """Prepare the full-size image of the photo you're looking at, so 100% zoom is ready."""
    p = _photo(body.name)
    threading.Thread(target=lambda: engine.warm_full(p, edits.get(p)), daemon=True).start()
    return {"ok": True}


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
    return {"geometry": edits.get(p), "full_size": [w, h], "grade": edits.get_grade(p)}


class GradeIn(BaseModel):
    name: str
    look: str = "neutral"
    adjust: dict = {}


@app.post("/api/grade")
def put_grade(body: GradeIn):
    """Remember this photo's look and sliders (the Grade tab saves as you work)."""
    return {"grade": edits.put_grade(_photo(body.name), body.look, body.adjust)}


class GradeCopyIn(BaseModel):
    names: list[str]
    look: str = "neutral"
    adjust: dict = {}


@app.post("/api/grade/copy")
def copy_grade(body: GradeCopyIn):
    """Give the ticked photos this grade (their own crops are kept)."""
    for n in body.names:
        edits.put_grade(_photo(n), body.look, body.adjust)
    return {"copied": len(body.names), "edited": get_state()["edited"]}


def _saved_recipe(p, look=None, adjust=None):
    """A photo's own remembered grade, else the look/adjust given (a deleted look falls back to neutral)."""
    g = edits.get_grade(p)
    look, adjust = (g["look"], g["adjust"]) if g else (look or "neutral", adjust or {})
    try:
        return _recipe(look, adjust)
    except HTTPException:
        return _recipe("neutral", adjust)


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
    quality: int | None = None  # default: Settings → export quality
    own: bool = False          # each photo with its own remembered grade (batch export)


@app.post("/api/export")
def export(body: ExportIn):
    from . import appsettings
    body.quality = body.quality or int(appsettings.load()["export_quality"])
    recipe = _recipe(body.look, body.adjust)
    files = [_photo(n) for n in body.names]
    recipes_for = {p: (_saved_recipe(p, body.look, body.adjust) if body.own else recipe) for p in files}
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"total": len(files), "done": 0, "current": "", "outputs": [], "error": None,
                    "kind": "export", "label": f"Exporting {len(files)} photo(s)", "finished": False, "cancel": False}

    def work():
        job = jobs[job_id]
        try:
            for p in files:
                job["current"] = p.name
                job["outputs"].append(str(engine.export(p, recipes_for[p], quality=body.quality, geom=edits.get(p))))
                job["done"] += 1
        except Exception as e:  # report to the UI instead of dying silently
            job["error"] = str(e)
        job["current"] = ""
        job["finished"] = True
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


@app.get("/api/library/new")
def library_new():
    """New (or changed) shoots on plugged-in drives, for the Library's "new shoot" notice."""
    from .library import scan
    lib = _lib()
    if lib.job["running"]:
        return {"shoots": [], "scanning": True}
    return {"shoots": scan.new_shoots(lib.con()), "scanning": False}


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
                   flag: str = "", stars: int = 0, offset: int = 0, limit: int = 200, order: str = "newest"):
    from .library import search
    return search.search(_lib().con(), q, limit=min(limit, 3000), offset=offset, order=order,
                         categories=[c for c in cats.split(",") if c] or None, year=year or None,
                         date_from=date_from or None, date_to=date_to or None, camera=camera or None,
                         graded=None if graded == "" else "edited" if graded == "edited" else graded == "1",
                         folder_id=folder_id or None,
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


# --- Browse: a shoot like a Finder window ---------------------------------------------------

@app.get("/api/browse/shoots")
def browse_shoots():
    from .library import browse
    return browse.shoots(_lib().con())


@app.get("/api/browse/photos/{folder_id}")
def browse_photos(folder_id: int, filter: str = "all"):
    from .library import browse
    return browse.photos(_lib().con(), folder_id, filter)


@app.get("/api/browse/thumb/{photo_id}")
def browse_thumb(photo_id: int):
    """The graded version's thumbnail when there is one (made once, then cached), else the RAW's."""
    from .library import essay
    kind, path, key = essay.display_source(_lib().con(), photo_id)
    if kind == "graded":
        name = essay._essay_thumb(photo_id, kind, path, key)
        if name:
            return FileResponse(essay.ESSAY_THUMBS / name, media_type="image/jpeg",
                                headers={"Cache-Control": "max-age=86400"})
    return library_thumb(photo_id)


@app.get("/api/browse/graded/{photo_id}")
def browse_graded(photo_id: int):
    """The newest graded version, large (cached on the internal disk)."""
    from .library import essay
    kind, path, key = essay.display_source(_lib().con(), photo_id)
    if kind != "graded":
        raise HTTPException(404, "not graded")
    d = _view_file(photo_id).parent / "graded"
    d.mkdir(exist_ok=True)
    import hashlib
    dst = d / f"{photo_id}_{hashlib.sha1(key.encode()).hexdigest()[:10]}.jpg"
    if not dst.exists():
        if path is None or not Path(path).exists():
            return browse_thumb(photo_id)          # drive offline: the small copy
        im = Image.open(path)
        im.draft("RGB", (2400, 2400))       # decode at a reduced size: much faster
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((2400, 2400), Image.LANCZOS)
        tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:6]}.tmp")
        im.save(tmp, "JPEG", quality=88)
        tmp.replace(dst)
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/browse/graded_full/{photo_id}")
def browse_graded_full(photo_id: int):
    """The graded JPG itself, full size, for 100% zoom in the Library viewer."""
    from .library import drives
    c = _lib().con()
    mounts = drives.online_mounts(c)
    for r in c.execute("""SELECT drive_id, rel_path FROM files WHERE photo_id=? AND role='export' AND kind='jpg'
                          AND missing=0 ORDER BY size DESC""", (photo_id,)):   # the largest (not an Instagram copy)
        if r["drive_id"] in mounts and (Path(mounts[r["drive_id"]]) / r["rel_path"]).exists():
            return FileResponse(Path(mounts[r["drive_id"]]) / r["rel_path"], media_type="image/jpeg",
                                headers={"Cache-Control": "no-cache"})
    raise HTTPException(409, "The graded JPG's drive is offline.")


class IdsIn(BaseModel):
    photo_ids: list[int]


@app.post("/api/browse/reveal")
def browse_reveal(body: IdsIn):
    """Open Finder with these photos' files selected (you delete them there; the app never does)."""
    from .library import browse
    paths, offline, exports = browse.reveal_files(_lib().con(), body.photo_ids)
    ok = browse.show_in_finder(paths)
    return {"files": len(paths), "selected": ok, "offline": offline, "exports_left": exports}


@app.post("/api/browse/forget_deleted/{folder_id}")
def browse_forget_deleted(folder_id: int):
    from .library import browse
    return {"removed": browse.forget_deleted(_lib().con(), folder_id)}


@app.on_event("startup")
def _trim_cache_on_start():
    """Keep the Grade preview cache under its limit (Settings → Storage); runs quietly in the background."""
    def work():
        try:
            from . import appsettings
            appsettings.prune_previews()
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


# --- Settings, storage, backups, activity --------------------------------------------------------

@app.get("/api/settings")
def settings_get():
    from . import appsettings
    return appsettings.load()


class SettingsIn(BaseModel):
    changes: dict


@app.post("/api/settings")
def settings_save(body: SettingsIn):
    from . import appsettings
    s = appsettings.save(body.changes)
    if "cache_limit_gb" in body.changes:
        threading.Thread(target=appsettings.prune_previews, daemon=True).start()
    return s


@app.get("/api/settings/storage")
def settings_storage():
    from . import appsettings
    return appsettings.storage()


class CleanIn(BaseModel):
    what: str


@app.post("/api/settings/clean")
def settings_clean(body: CleanIn):
    """Trim the app's own caches (they rebuild themselves) or its old safety copies."""
    from . import appsettings
    fn = {"previews": appsettings.prune_previews, "views": appsettings.clear_views,
          "old_backups": appsettings.remove_old_backups}.get(body.what)
    if not fn:
        raise HTTPException(400, "unknown")
    return {"freed": fn(), "storage": appsettings.storage()}


@app.get("/api/settings/drives")
def settings_drives():
    """Plugged-in drives you could back up to, with free space."""
    from .library import drives
    c = _lib().con()
    out = []
    for did, mount in drives.online_mounts(c).items():
        name = c.execute("SELECT name FROM drives WHERE id=?", (did,)).fetchone()[0]
        try:
            free = shutil.disk_usage(mount).free
        except OSError:
            free = None
        out.append({"id": did, "name": name, "mount": mount, "free": free})
    return out


class BackupIn(BaseModel):
    drive_id: int


@app.post("/api/settings/backup")
def settings_backup(body: BackupIn):
    """Back up your work (not your photos) to a folder on the chosen drive."""
    from . import appsettings
    from .library import drives
    c = _lib().con()
    mount = drives.online_mounts(c).get(body.drive_id)
    if not mount:
        raise HTTPException(409, "That drive isn't plugged in.")
    try:
        return appsettings.backup(c, mount)
    except OSError as e:
        raise HTTPException(500, f"Couldn't write the backup: {e}")


@app.get("/api/activity")
def activity():
    """Everything running in the background (scan, exports, book PDFs, uploads), for the header."""
    import time as _t
    out = []
    lib = _lib()
    if lib.job.get("running"):
        out.append({"id": "scan", "kind": "scan", "label": "Scanning for new photos", "done": lib.job.get("done", 0),
                    "total": lib.job.get("todo", 0), "current": lib.job.get("phase", ""), "finished": False, "cancel": True})
    now = _t.time()
    for jid, j in list(jobs.items()):
        if "kind" not in j:
            continue
        if j.get("finished") and now - j.setdefault("_ended", now) > 90:
            continue
        out.append({"id": jid, "kind": j["kind"], "label": j.get("label", ""), "done": j.get("done", 0),
                    "total": j.get("total", 0), "current": j.get("current", ""), "waiting": j.get("waiting", []),
                    "error": j.get("error"), "finished": bool(j.get("finished")), "cancel": "cancel" in j})
    return out


# --- Publish to WordPress -----------------------------------------------------------------------

@app.get("/api/publish/settings")
def publish_settings():
    from . import publish
    st = publish.load_settings()
    return {"site": st.get("site", ""), "user": st.get("user", ""), "has_password": bool(publish.get_password(st))}


class PublishSettingsIn(BaseModel):
    site: str
    user: str
    password: str | None = None


@app.post("/api/publish/settings")
def publish_save(body: PublishSettingsIn):
    """Save the website + username (and the application password into the Keychain), then test."""
    from . import publish
    try:
        st = publish.save_settings(body.site, body.user, body.password)
        return {"site": st["site"], "user": st["user"], **publish.test_connection(st)}
    except (ValueError, RuntimeError, publish.WPError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/publish/test")
def publish_test():
    from . import publish
    try:
        return publish.test_connection()
    except publish.WPError as e:
        raise HTTPException(400, str(e))


PUBLISH_DEFAULTS = {"title": "", "category": None, "intro": "", "closing": "", "cover": None, "captions": True,
                    "size": 2048, "strip_gps": True, "post_id": None, "link": None, "uploaded": None}


@app.get("/api/essays/{eid}/publish")
def essay_publish_get(eid: int):
    """This essay's post details for WordPress (saved with the essay)."""
    e = _essay_row(eid)
    src = json.loads(e["source"] or "{}")
    pub = {**PUBLISH_DEFAULTS, "title": e["name"], **(src.get("publish") or {})}
    return pub


class PublishDetailsIn(BaseModel):
    details: dict


@app.post("/api/essays/{eid}/publish")
def essay_publish_save(eid: int, body: PublishDetailsIn):
    c = _lib().con()
    e = _essay_row(eid)
    src = json.loads(e["source"] or "{}")
    keep = {k: v for k, v in body.details.items() if k in PUBLISH_DEFAULTS and k not in ("post_id", "link", "uploaded")}
    src["publish"] = {**PUBLISH_DEFAULTS, **(src.get("publish") or {}), **keep}
    c.execute("UPDATE essays SET source=?, updated_at=datetime('now') WHERE id=?", (json.dumps(src), eid))
    c.commit()
    return src["publish"]


@app.get("/api/essays/{eid}/preview")
def essay_publish_preview(eid: int):
    """A local preview of the WordPress post: title, write-up, then a fade slideshow with
    thumbnails (like your existing essays' Visual Portfolio slider), captions, closing text."""
    import html as H
    from fastapi.responses import HTMLResponse
    c, es = _lib().con(), _essay()
    e = _essay_row(eid)
    pub = essay_publish_get(eid)
    its = es.items(c, eid)
    cats = {}
    try:
        from . import publish
        cats = {x["id"]: x["name"] for x in publish.test_connection()["categories"]}
    except Exception:
        pass
    slides = []
    for it in its:
        pid = it["photo_id"]
        graded = c.execute("SELECT 1 FROM files WHERE photo_id=? AND role='export' AND kind='jpg' AND missing=0", (pid,)).fetchone()
        src = f"/api/browse/graded/{pid}" if graded else f"/api/cull/view/{pid}"
        slides.append((src, f"/api/essays/thumb/{pid}", (it["title"] or "") if pub["captions"] else ""))
    cover = pub.get("cover") or (its[0]["photo_id"] if its else None)
    para = lambda t: "".join(f"<p>{H.escape(x.strip())}</p>" for x in (t or "").split("\n\n") if x.strip())
    cat = cats.get(pub.get("category"), "")
    slide_html = "".join(f'<figure class="s{" on" if k == 0 else ""}"><img src="{a}" alt="{H.escape(t)}" loading="lazy">'
                         f'{f"<figcaption>{H.escape(t)}</figcaption>" if t else ""}</figure>' for k, (a, _, t) in enumerate(slides))
    thumbs = "".join(f'<img src="{b}" data-k="{k}" alt="">' for k, (_, b, _) in enumerate(slides))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preview · {H.escape(pub['title'] or e['name'])}</title><style>
body{{margin:0;background:#fff;color:#334155;font:17px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}}
.bar{{background:#111;color:#ddd;font-size:13px;padding:8px 16px;text-align:center}}
.bar b{{color:#e0b34a}} header{{border-bottom:1px solid #e5e7eb;padding:18px 24px;font-weight:600;color:#1e293b;font-size:22px}}
main{{max-width:1200px;margin:0 auto;padding:40px 24px 80px}} .cat{{color:#046bd2;font-size:14px}}
h1{{color:#1e293b;font-size:36px;font-weight:600;margin:6px 0 18px;line-height:1.25}} .intro p,.closing p{{max-width:760px}}
.show{{position:relative;background:#fafafa;margin:28px 0 10px;aspect-ratio:3/2}}
.show figure{{position:absolute;inset:0;margin:0;opacity:0;transition:opacity .3s;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.show figure.on{{opacity:1}} .show img{{max-width:100%;max-height:calc(100% - 30px);object-fit:contain}}
.show figcaption{{font-size:14px;color:#64748b;padding-top:6px}}
.dots{{text-align:center;margin:8px 0}} .dots span{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#cbd5e1;margin:0 3px;cursor:pointer}} .dots span.on{{background:#1e293b}}
.thumbs{{display:flex;gap:10px;overflow-x:auto;padding:6px 0}} .thumbs img{{height:70px;cursor:pointer;opacity:.6}} .thumbs img.on{{opacity:1;outline:2px solid #1e293b}}
.grid{{margin-top:60px;border-top:1px solid #e5e7eb;padding-top:20px;font-size:14px;color:#64748b}}
.grid .card{{display:inline-block;width:230px;margin:10px 14px 0 0;vertical-align:top;border-radius:6px;overflow:hidden;box-shadow:0 1px 4px #0002}}
.grid .card img{{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}} .grid .card div{{padding:8px 10px;color:#1e293b;font-weight:600}}
</style></head><body>
<div class="bar"><b>Preview</b> — roughly how the post will look on your site (your theme decides the exact fonts and spacing). Nothing has been uploaded.</div>
<header>Andrew Chew CC</header>
<main><div class="cat">{H.escape(cat)}</div><h1>{H.escape(pub['title'] or e['name'])}</h1>
<div class="intro">{para(pub['intro']) or '<p style="color:#94a3b8"><i>(No write-up yet.)</i></p>'}</div>
<div class="show">{slide_html}</div><div class="dots">{"".join(f'<span data-k="{k}"></span>' for k in range(len(slides)))}</div>
<div class="thumbs">{thumbs}</div>
<div class="closing">{para(pub['closing'])}</div>
<div class="grid">How it appears on the {H.escape(cat or "gallery")} page:<br>
<div class="card"><img src="{f"/api/essays/thumb/{cover}" if cover else ""}" alt=""><div>{H.escape(pub['title'] or e['name'])}</div></div></div>
</main><script>
const S=[...document.querySelectorAll('.show figure')],D=[...document.querySelectorAll('.dots span')],T=[...document.querySelectorAll('.thumbs img')];let k=0,t;
function go(n){{k=(n+S.length)%S.length;S.forEach((s,i)=>s.classList.toggle('on',i===k));D.forEach((s,i)=>s.classList.toggle('on',i===k));T.forEach((s,i)=>s.classList.toggle('on',i===k));
const B=document.querySelector('.thumbs');if(T[k])B.scrollLeft=T[k].offsetLeft-B.offsetLeft-B.clientWidth/2+T[k].clientWidth/2;clearTimeout(t);t=setTimeout(()=>go(k+1),6000)}}
D.concat(T).forEach(el=>el.onclick=()=>go(+el.dataset.k));document.onkeydown=e=>{{if(e.key==='ArrowRight')go(k+1);if(e.key==='ArrowLeft')go(k-1)}};if(S.length)go(0);
</script></body></html>"""
    return HTMLResponse(page)


@app.post("/api/essays/{eid}/upload")
def essay_upload(eid: int):
    """Upload as draft: web-size photos to the Media Library + a DRAFT post (background job).
    Nothing on the site is edited or deleted; you publish the draft in WordPress yourself."""
    from . import publish
    _essay_row(eid)
    job_id = uuid.uuid4().hex[:8]
    job = jobs[job_id] = {"kind": "upload", "label": f"Uploading “{_essay_row(eid)['name']}” to your website",
                          "total": 0, "done": 0, "current": "", "waiting": [], "uploaded": [], "result": None,
                          "error": None, "note": None, "finished": False, "cancel": False}

    def work():
        try:
            job["result"] = publish.upload_essay(_lib().con(), eid, job=job, cancelled=lambda: job["cancel"])
        except Exception as e:
            job["error"] = str(e)
            if job["uploaded"]:
                job["note"] = (f"{len(job['uploaded'])} photo(s) had already been uploaded to your Media Library; "
                               "no post was created. You can delete them in WordPress → Media, or just try again.")
        job["current"] = ""
        job["finished"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


class PublishPhotosIn(BaseModel):
    name: str
    photo_ids: list[int] = []
    view: dict | None = None
    look: str = "leica-m9"


@app.post("/api/publish/from_photos")
def publish_from_photos(body: PublishPhotosIn):
    """Library → Publish…: an essay with all these photos, in shooting order, ready to publish."""
    c, es = _lib().con(), _essay()
    ids = body.photo_ids or (_view_ids(c, body.view) if body.view else [])
    if not ids:
        raise HTTPException(400, "Select some photos first.")
    times = {r[0]: r[1] or "" for r in c.execute(
        f"SELECT id, capture_time FROM photos WHERE id IN ({','.join('?' * len(ids))})", ids)}
    ids = sorted(dict.fromkeys(ids), key=lambda i: times.get(i, ""))
    try:
        eid, _ = es.create_from_brief(c, body.name.strip() or "Untitled", ids, {"count": len(ids)}, body.look,
                                      origin={"type": "library-publish"})
    except ValueError as e:
        raise HTTPException(400, str(e))
    es.set_order(c, eid, ids)                  # shooting order, as in the shoot
    return {"id": eid, "count": len(ids)}


@app.post("/api/publish/forget")
def publish_forget():
    from . import publish
    publish.forget()
    return {"ok": True}


# --- Second look: overlooked photos ------------------------------------------------------

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


@app.get("/api/cull/for_shoot/{folder_id}")
def cull_for_shoot(folder_id: int):
    """This shoot's cull, if there is one: to resume or review it straight from the Library."""
    c = _lib().con()
    sid = _cull().shoot_session(c, folder_id)
    if not sid:
        return {"session": None}
    s = c.execute("SELECT finished_at FROM cull_sessions WHERE id=?", (sid,)).fetchone()
    g = c.execute("SELECT COUNT(*), COALESCE(SUM(done), 0) FROM cull_groups WHERE session_id=?", (sid,)).fetchone()
    return {"session": sid, "finished": s["finished_at"], "groups": g[0], "done": g[1]}


@app.get("/api/cull/second/{sid}")
def cull_second(sid: int):
    """After a cull: frames you didn't decide on that deserve another look, plus high-scoring rejects."""
    c = _lib().con()
    out = _cull().second_look(c, sid)
    names = {r[0]: PurePath(r[1]).name for r in c.execute(
        f"""SELECT p.id, f.rel_path FROM photos p JOIN files f ON f.id = p.primary_file_id
            WHERE p.id IN ({','.join('?' * len(out['unmarked'] + out['rejects'])) or 'NULL'})""",
        [x["id"] for x in out["unmarked"] + out["rejects"]])}
    for x in out["unmarked"] + out["rejects"]:
        x["name"] = names.get(x["id"], "")
    return out


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
    # Photos can leave the library after a cull started (merged into their RAW, or deleted and
    # removed): leave them out, and drop groups that end up empty.
    for g in groups:
        g["ids"] = [i for i in g["ids"] if i in frames]
        g["order"] = [i for i in g["order"] if i in frames]
        if g["suggested"] not in frames:
            g["suggested"] = (g["order"] or g["ids"] or [None])[0]
    groups = [g for g in groups if g["ids"]]
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
    return _open_in_grade(ids, f"Picks · {s['label'] if s else 'cull'}")


def _open_in_grade(ids, label):
    """Open these library photos in Grade as a collection, in the order given."""
    paths, offline = [], 0
    for pid in ids:
        path, _, _ = _photo_file(pid)
        if path is None:
            offline += 1
        elif raw.is_raw(path) or raw.is_jpeg(path):
            paths.append(path)
    if not paths:
        raise HTTPException(409, "These photos' drive is offline — plug it in to grade them.")
    state["collection"] = {"label": label, "paths": paths}
    return {**get_state(), "offline": offline}


class LibGradeIn(BaseModel):
    photo_ids: list[int]
    label: str = "Selected"


@app.post("/api/library/grade")
def library_grade(body: LibGradeIn):
    """Library → Grade: the picks of a shoot, or the photos you selected."""
    if not body.photo_ids:
        raise HTTPException(400, "Select some photos first.")
    return _open_in_grade(body.photo_ids, body.label)


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


# --- Essays ---------------------------------------------------------------------------------

def _essay():
    from .library import essay
    return essay


def _essay_row(eid):
    r = _lib().con().execute("SELECT * FROM essays WHERE id=?", (eid,)).fetchone()
    if not r:
        raise HTTPException(404, "unknown essay")
    return r


@app.get("/api/essays")
def essays_list():
    c = _lib().con()
    rows = c.execute("""SELECT e.id, e.name, e.target, e.updated_at, COUNT(i.photo_id) AS n FROM essays e
                        LEFT JOIN essay_items i ON i.essay_id = e.id GROUP BY e.id ORDER BY e.updated_at DESC""")
    return [{"id": r["id"], "name": r["name"], "target": r["target"], "count": r["n"], "updated": r["updated_at"]} for r in rows]


class EssayNewIn(BaseModel):
    name: str
    source: dict
    target: int = 20
    look: str = "leica-m9"
    brief: dict = {}


@app.post("/api/essays")
def essays_new(body: EssayNewIn):
    """From the Essays tab: gather the source's photos, then choose the best `target` to the brief."""
    c, es = _lib().con(), _essay()
    try:
        ids = es.source_photo_ids(c, body.source)
        if not ids:
            raise ValueError("No photos match — try other words, or another source.")
        eid, note = es.create_from_brief(c, body.name.strip() or "Untitled essay", ids,
                                         {**body.brief, "count": body.target}, body.look, origin=body.source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": eid, "note": note, "pool": len(ids)}


class EssayBriefIn(BaseModel):
    name: str
    photo_ids: list[int] = []
    view: dict | None = None        # the Library's current filters, when nothing is selected
    brief: dict = {}
    look: str = "leica-m9"


def _view_ids(c, v):
    """Every photo in a Library view (not just the page on screen); with search words, the clear matches."""
    from .library import search
    text = (v.get("q") or "").strip()
    graded = v.get("graded", "")
    res = search.search(c, text, limit=100000, categories=None, year=v.get("year") or None,
                        date_from=v.get("date_from") or None, date_to=v.get("date_to") or None,
                        camera=v.get("camera") or None,
                        graded=None if graded == "" else "edited" if graded == "edited" else graded == "1",
                        folder_id=int(v["folder_id"]) if v.get("folder_id") else None, flag=v.get("flag") or None,
                        stars=int(v.get("stars") or 0) or None, order="oldest")
    rows = res["results"]
    return [r["id"] for r in rows if not text or r.get("strong")]


@app.post("/api/essays/brief")
def essay_from_brief(body: EssayBriefIn):
    """Library → Make an essay: choose the best N from a selection or view, in a suggested order."""
    c = _lib().con()
    ids = body.photo_ids or (_view_ids(c, body.view) if body.view else [])
    try:
        eid, note = _essay().create_from_brief(c, body.name.strip() or "Untitled essay", ids, body.brief, body.look)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": eid, "note": note, "pool": len(ids)}


@app.post("/api/essays/count")
def essay_pool_count(body: EssayBriefIn):
    """How many photos a view holds (for the brief form)."""
    c = _lib().con()
    return {"count": len(body.photo_ids) if body.photo_ids else len(_view_ids(c, body.view or {}))}


@app.get("/api/essays/{eid}")
def essay_get(eid: int):
    from .library import drives
    c, es = _lib().con(), _essay()
    e = _essay_row(eid)
    its = es.items(c, eid)
    ids = [i["photo_id"] for i in its]
    feats = es.ensure_features(c, ids)
    mounts = drives.online_mounts(c)
    info = {r["id"]: r for r in c.execute(f"""SELECT p.id, p.capture_time, f.rel_path, f.drive_id, f.missing FROM photos p
                                             JOIN files f ON f.id = p.primary_file_id
                                             WHERE p.id IN ({','.join('?' * len(ids)) or 'NULL'})""", ids)}
    why = (json.loads(e["source"] or "{}").get("reasons") or {})
    photos = []
    for it in its:
        pid, r = it["photo_id"], info.get(it["photo_id"])
        fv = feats.get(pid, {})
        photos.append({"id": pid, "locked": bool(it["locked"]), "title": it["title"] or "",
                       "name": PurePath(r["rel_path"]).stem if r else str(pid), "time": r["capture_time"] if r else None,
                       "image": fv.get("image", "raw"), "online": bool(r and r["drive_id"] in mounts and not r["missing"]),
                       "scale": (fv.get("pace") or {}).get("scale"), "why": why.get(str(pid), [])})
    versions = [{"id": v["id"], "name": v["name"], "created": v["created_at"]}
                for v in c.execute("SELECT id, name, created_at FROM essay_versions WHERE essay_id=? ORDER BY id DESC", (eid,))]
    src = json.loads(e["source"] or "{}")
    brief = src.get("brief") or {"count": e["target"] or len(photos), "theme": "", "colour": "mix", "per_shoot": None}
    return {"id": eid, "name": e["name"], "target": e["target"], "look": e["look"],
            "source": {k: v for k, v in src.items() if k not in ("ids", "reasons")}, "brief": brief,
            "pool": len(src.get("ids") or []), "photos": photos, "versions": versions}


class EssayPatchIn(BaseModel):
    name: str | None = None
    target: int | None = None
    look: str | None = None
    brief: dict | None = None


@app.patch("/api/essays/{eid}")
def essay_patch(eid: int, body: EssayPatchIn):
    c = _lib().con()
    _essay_row(eid)
    for k in ("name", "target", "look"):
        v = getattr(body, k)
        if v is not None:
            c.execute(f"UPDATE essays SET {k}=?, updated_at=datetime('now') WHERE id=?", (v, eid))
    if body.brief is not None:              # e.g. Undo after a re-choose
        src = json.loads(c.execute("SELECT source FROM essays WHERE id=?", (eid,)).fetchone()[0] or "{}")
        src["brief"] = body.brief
        c.execute("UPDATE essays SET source=?, target=? WHERE id=?", (json.dumps(src), body.brief.get("count"), eid))
    c.commit()
    return {"ok": True}


@app.delete("/api/essays/{eid}")
def essay_delete(eid: int):
    _essay().delete(_lib().con(), eid)
    return essays_list()


class OrderIn(BaseModel):
    order: list[int]
    locked: dict[str, bool] = {}
    titles: dict[str, str] = {}


@app.post("/api/essays/{eid}/order")
def essay_order(eid: int, body: OrderIn):
    _essay_row(eid)
    _essay().set_order(_lib().con(), eid, body.order, locked={int(k): v for k, v in body.locked.items()},
                       titles={int(k): v for k, v in body.titles.items()})
    return {"ok": True}


class AddIn(BaseModel):
    photo_ids: list[int] = []
    source: dict | None = None


@app.post("/api/essays/{eid}/add")
def essay_add(eid: int, body: AddIn):
    c, es = _lib().con(), _essay()
    _essay_row(eid)
    ids = list(body.photo_ids)
    if body.source:
        try:
            ids += es.source_photo_ids(c, body.source)
        except ValueError as e:
            raise HTTPException(400, str(e))
    before = len(es.items(c, eid))
    es.add(c, eid, ids)
    src = json.loads(c.execute("SELECT source FROM essays WHERE id=?", (eid,)).fetchone()[0] or "{}")
    if src.get("type") == "photos":         # photos added by hand join the pool (and so the bench)
        src["ids"] = list(dict.fromkeys(list(src.get("ids", [])) + ids))
        c.execute("UPDATE essays SET source=? WHERE id=?", (json.dumps(src), eid))
        c.commit()
    return {"added": len(es.items(c, eid)) - before}


@app.get("/api/essays/{eid}/transitions")
def essay_transitions(eid: int, order: str):
    ids = [int(x) for x in order.split(",") if x]
    return _essay().transitions_for(_lib().con(), ids)


class SuggestIn(BaseModel):
    style: str = "flow"
    arc: str = "day_to_night"


@app.post("/api/essays/{eid}/suggest")
def essay_suggest(eid: int, body: SuggestIn):
    """A suggested order (not applied: the page applies it, so Undo works)."""
    _essay_row(eid)
    if body.style not in ("flow", "time", "mood"):
        raise HTTPException(400, "unknown style")
    return _essay().sequence(_lib().con(), eid, body.style, body.arc)


@app.get("/api/essays/{eid}/pairs")
def essay_pairs(eid: int, limit: int = 30):
    _essay_row(eid)
    return _essay().pairs(_lib().con(), eid, limit=limit)


class PairMarkIn(BaseModel):
    a: int
    b: int
    verdict: str | None = None
    rhyme: str | None = None


@app.post("/api/pairs/mark")
def pair_mark(body: PairMarkIn):
    try:
        _essay().mark_pair(_lib().con(), body.a, body.b, body.verdict, body.rhyme)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _with_names(c, rows):
    ids = [r["id"] for r in rows]
    names = {r[0]: PurePath(r[1]).stem for r in c.execute(
        f"""SELECT p.id, f.rel_path FROM photos p JOIN files f ON f.id = p.primary_file_id
            WHERE p.id IN ({','.join('?' * len(ids)) or 'NULL'})""", ids)}
    return [{**r, "name": names.get(r["id"], "")} for r in rows]


@app.get("/api/essays/{eid}/bench")
def essay_bench(eid: int, limit: int = 60):
    """Photos from the essay's pool that aren't in it, best first, with why not."""
    _essay_row(eid)
    c = _lib().con()
    rows = _essay().bench(c, eid)
    return {"total": len(rows), "results": _with_names(c, rows[:limit])}


@app.get("/api/essays/{eid}/replace/{photo_id}")
def essay_replace(eid: int, photo_id: int):
    """Alternatives from the bench for one photo's spot."""
    _essay_row(eid)
    c = _lib().con()
    return _with_names(c, _essay().replacements(c, eid, photo_id))


class RechooseIn(BaseModel):
    brief: dict


@app.post("/api/essays/{eid}/rechoose")
def essay_rechoose(eid: int, body: RechooseIn):
    """Choose again with a changed brief; locked photos stay where they are."""
    _essay_row(eid)
    try:
        note = _essay().rechoose(_lib().con(), eid, body.brief)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"note": note}


@app.get("/api/essays/{eid}/leaveout")
def essay_leaveout(eid: int, target: int):
    c, es = _lib().con(), _essay()
    _essay_row(eid)
    return es.leave_out(c, [i["photo_id"] for i in es.items(c, eid)], target)


@app.get("/api/essays/{eid}/fill")
def essay_fill(eid: int, position: int):
    _essay_row(eid)
    return _essay().fill_gap(_lib().con(), eid, position)


class VersionIn(BaseModel):
    name: str


@app.post("/api/essays/{eid}/versions")
def essay_version_save(eid: int, body: VersionIn):
    _essay_row(eid)
    return {"id": _essay().save_version(_lib().con(), eid, body.name.strip() or "Version")}


@app.post("/api/essays/{eid}/versions/{vid}/restore")
def essay_version_restore(eid: int, vid: int):
    try:
        _essay().restore_version(_lib().con(), eid, vid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.delete("/api/essays/{eid}/versions/{vid}")
def essay_version_delete(eid: int, vid: int):
    c = _lib().con()
    c.execute("DELETE FROM essay_versions WHERE id=? AND essay_id=?", (vid, eid))
    c.commit()
    return {"ok": True}


class EssayExportIn(BaseModel):
    size: str = "web"
    pdf: bool = True


@app.post("/api/essays/{eid}/export")
def essay_export(eid: int, body: EssayExportIn):
    """Numbered JPGs in order (background job; waits for offline drives) + optional contact sheet."""
    from .library import essay_out
    _essay_row(eid)
    job_id = uuid.uuid4().hex[:8]
    job = jobs[job_id] = {"kind": "essay-export", "label": f"Exporting “{_essay_row(eid)['name']}”", "total": 0, "done": 0, "current": "", "waiting": [], "outputs": [],
                          "folder": None, "pdf": None, "error": None, "finished": False, "cancel": False}

    def work():
        try:
            c = _lib().con()
            folder = essay_out.run_export(c, eid, body.size, job=job, cancelled=lambda: job["cancel"])
            if folder and body.pdf and not job["cancel"]:
                job["current"] = "contact sheet"
                job["pdf"] = str(essay_out.contact_sheet(c, eid, dst=folder / "00_contact-sheet.pdf"))
        except Exception as e:  # report to the page instead of dying silently
            job["error"] = str(e)
        job["current"] = ""
        job["finished"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


class BookIn(BaseModel):
    size: str = "a4-portrait"
    layout: str = "border"
    cover: bool = True
    captions: bool = True
    page_numbers: bool = True
    index: bool = True
    bleed: bool = False
    cover_photo: int | None = None


@app.post("/api/essays/{eid}/book")
def essay_book(eid: int, body: BookIn):
    """A print-ready book / zine PDF (background job; waits for offline drives)."""
    from .library import essay_book as eb
    _essay_row(eid)
    if body.size not in eb.SIZES:
        raise HTTPException(400, "unknown page size")
    job_id = uuid.uuid4().hex[:8]
    job = jobs[job_id] = {"kind": "book", "label": f"Book PDF of “{_essay_row(eid)['name']}”", "total": 0, "done": 0, "current": "", "waiting": [], "outputs": [], "folder": None,
                          "pdf": None, "error": None, "finished": False, "cancel": False}

    def work():
        try:
            eb.make_book(_lib().con(), eid, size=body.size, layout=body.layout, cover=body.cover,
                         captions=body.captions, page_numbers=body.page_numbers, index=body.index,
                         bleed=body.bleed, cover_photo=body.cover_photo, job=job, cancelled=lambda: job["cancel"])
        except Exception as e:
            job["error"] = str(e)
        job["current"] = ""
        job["finished"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    jobs[job_id]["cancel"] = True
    return {"ok": True}


@app.get("/api/essays/{eid}/plan")
def essay_plan(eid: int):
    """What an export would use for each photo, and which drives it needs."""
    from .library import essay_out
    _essay_row(eid)
    return essay_out.plan(_lib().con(), eid)


@app.post("/api/essays/{eid}/pdf")
def essay_pdf(eid: int):
    """The contact sheet on its own (works with the drives unplugged: it uses thumbnails)."""
    from .library import essay_out
    _essay_row(eid)
    path = essay_out.contact_sheet(_lib().con(), eid)
    _reveal(path, open_file=True)
    return {"path": str(path)}


def _reveal(path, open_file=False):
    """Open (or show in Finder) something the app wrote under ~/Pictures/Film Look Studio Essays."""
    import subprocess
    from .library import essay_out
    p = Path(path).resolve()
    if essay_out.OUT_DIR.resolve() not in p.parents:
        raise HTTPException(400, "can only show the app's own essay exports")
    subprocess.run(["open", str(p)] if open_file else ["open", "-R", str(p)], check=False)


class RevealIn(BaseModel):
    path: str


@app.post("/api/essays/reveal")
def essay_reveal(body: RevealIn):
    _reveal(body.path)
    return {"ok": True}


@app.post("/api/essays/open")
def essay_open(body: RevealIn):
    """Open a PDF the app made (e.g. the book) in Preview."""
    _reveal(body.path, open_file=True)
    return {"ok": True}


@app.get("/api/essays/thumb/{photo_id}")
def essay_thumb(photo_id: int):
    """The essay's view of a photo (graded JPG if any, else RAW preview with crop)."""
    es = _essay()
    r = _lib().con().execute("SELECT thumb FROM essay_features WHERE photo_id=?", (photo_id,)).fetchone()
    if r and r[0] and (es.ESSAY_THUMBS / r[0]).exists():
        return FileResponse(es.ESSAY_THUMBS / r[0], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
    return library_thumb(photo_id)


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
