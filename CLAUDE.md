# Film Look Studio

Local app that applies film looks to RAW (DNG) photos, previews them, and exports
full-resolution JPGs. Owner: Andrew (street photographer, Leica Q3 43, macOS).

- When Andrew asks for grading help (suggest a look, tweak in plain English, match a
  reference or a photographer, export, make a LUT), use the `grade` skill.
- Run the app with `./studio <command>` (see `./studio -h`). First-time setup: `./setup.sh`.
- Never modify or delete source RAW files; exports go to `graded/` next to them.

## Code map

- `filmlook/raw.py` – RAW decode (rawpy/LibRaw) to linear sRGB, auto exposure metering.
- `filmlook/dng.py` – DNG lens corrections LibRaw skips (WarpRectilinear, FixVignetteRadial, DefaultCrop).
- `filmlook/pipeline.py` – the grade: base render → per-pixel look → grain/vignette.
- `filmlook/recipes.py` – JSON looks in `looks/` (built-in) and `looks/user/` (saved), `extends`, overrides.
- `filmlook/engine.py` – cached previews, contact sheets, strip-based full-res export with EXIF copy.
- `filmlook/lut.py` – bake a look to a 3D LUT (.cube export; also speeds up exports).
- `filmlook/reference.py` – make a look from a reference image (OKLab statistics transfer).
- `filmlook/geometry.py` – crop & straighten (flips/90° -> fine angle -> crop as fractions; slot for
  perspective). `filmlook/edits.py` stores edits in the library DB (`edits`, keyed drive + path),
  never in the photo. Preview, thumbnails, export and the CLI all apply them.
- JPGs open in Grade too: `raw.decode` handles them and `pipeline.base(display=True)` keeps a
  neutral JPG unchanged (no auto exposure / contrast).
- `filmlook/server.py` + `static/index.html` – local browser app (127.0.0.1 only): Library and Grade tabs.
- `filmlook/library/` – searchable archive. Database + thumbnails live in
  `~/Library/Application Support/Film Look Studio/` (internal disk; never write next to photos).
  `db.py` schema (photos ← files, sidecars, embeddings; later features attach to `photos.id`),
  `drives.py` (drives by volume UUID; offline = not mounted), `scan.py` (incremental, stoppable),
  `pairing.py` (RAW + copies + exported JPGs = one photo), `sidecars.py` (ON1/XMP, read only),
  `thumbs.py` (embedded RAW previews), `vision.py` (SigLIP 2 on MPS, categories),
  `index.py`, `search.py`, `service.py` (background scan job for the app),
  `quality.py` (focus/motion/exposure from embedded previews, burst groups),
  `secondlook.py` (second-look finder: cross-fitted taste model + zero-shot appeal; keep/dismiss in `picks`).
- PyTorch model calls must go through `vision` (one at a time: concurrent MPS use segfaults).
- Library CLI: `./studio library add|scan|status|search|folders`.

## Checks

```bash
.venv/bin/python -m pytest -q
```
