"""Local browser app. Binds to 127.0.0.1 only; photos never leave your Mac."""
import json
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path

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
    return raw.list_raws(folder) if folder else []


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


def run(folder=None, port=8765, open_browser=True):
    import uvicorn
    if folder:
        set_folder(FolderIn(folder=folder))
    url = f"http://127.0.0.1:{port}"
    print(f"Film Look Studio running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
