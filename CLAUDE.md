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
- `filmlook/server.py` + `static/index.html` – local browser app (127.0.0.1 only).

## Checks

```bash
.venv/bin/python -m pytest -q
```
