"""Local browser app. Binds to 127.0.0.1 only; photos never leave your Mac."""
import json
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path, PurePath

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import engine, lut, pipeline, raw, recipes, reference

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Film Look Studio")
state = {"folder": None}
jobs = {}
_thumbs = {}


def _photos():
    folder = state["folder"]
    try:
        return raw.list_raws(folder) if folder else []
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
    return {"folder": str(state["folder"]) if state["folder"] else "",
            "photos": [p.name for p in _photos()], "looks": recipes.list_looks()}


class FolderIn(BaseModel):
    folder: str


@app.post("/api/folder")
def set_folder(body: FolderIn):
    folder = Path(body.folder.strip().strip("'\"")).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, f"not a folder: {folder}")
    state["folder"] = folder.resolve()
    return get_state()


@app.get("/api/thumb")
def thumb(name: str):
    p = _photo(name)
    if p not in _thumbs:
        _thumbs[p] = engine.jpeg_bytes(engine.render_preview(p, recipes.load_look(None), edge=360), 80)
    return Response(_thumbs[p], media_type="image/jpeg")


@app.get("/api/render")
def render(name: str, look: str = "neutral", adjust: str = "{}", edge: int = 1400):
    return _jpeg(engine.render_preview(_photo(name), _recipe(look, adjust), edge=min(edge, engine.PREVIEW_EDGE)))


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
                job["outputs"].append(str(engine.export(p, recipe, quality=body.quality)))
                job["done"] += 1
        except Exception as e:  # report to the UI instead of dying silently
            job["error"] = str(e)
        job["current"] = ""

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
        lin, gain = engine.preview_linear(_photo(name))
        match_src = pipeline.match_source_stats(lin, recipe, gain)
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
        scan.add_root(_lib().con(), body.path.strip().strip("'\""))
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
                   offset: int = 0, limit: int = 200):
    from .library import search
    return search.search(_lib().con(), q, limit=min(limit, 500), offset=offset,
                         categories=[c for c in cats.split(",") if c] or None, year=year or None,
                         date_from=date_from or None, date_to=date_to or None, camera=camera or None,
                         graded=None if graded == "" else graded == "1", folder_id=folder_id or None)


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
    if not raw.is_raw(src):
        raise HTTPException(415, "This photo has no RAW file, so it can't be graded here.")
    set_folder(FolderIn(folder=str(src.parent)))
    return {**get_state(), "select": src.name}


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
