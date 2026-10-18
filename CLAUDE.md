# Film Look Studio

Local app that applies film looks to RAW (DNG) photos, previews them, and exports
full-resolution JPGs. Owner: Andrew (street photographer, Leica Q3 43, macOS).

- When Andrew asks for grading help (suggest a look, tweak in plain English, match a
  reference or a photographer, export, make a LUT), use the `grade` skill.
- Run the app with `./studio <command>` (see `./studio -h`). First-time setup: `./setup.sh`.
- Never modify or delete source RAW files; exports go to `graded/` next to them.

## Code map

- `filmlook/raw.py` – RAW decode (rawpy/LibRaw) to linear sRGB, auto exposure metering. Grade's auto exposure
  (`engine._auto_gain`, METER_VERSION 4) matches the camera's embedded preview (median), else the meter.
  `pipeline.base` brightens with g·x/(1+(g−1)x): mid-tones lifted, the sensor's white stays white (a plain
  gain + shoulder blew out skies). Cached previews re-meter once when METER_VERSION changes.
- `filmlook/dng.py` – DNG lens corrections LibRaw skips (WarpRectilinear, FixVignetteRadial, DefaultCrop).
- `filmlook/pipeline.py` – the grade: base render → per-pixel look → grain/vignette. Tone: S-curve,
  shadows/highlights, black/white, then `tone.curve` (look) and `tone.user_curve` (Grade tab's curve;
  monotone cubic, `curve_table`). `engine.render_threaded` splits renders over cores (identical output).
  Grade zoom: full-size decode warmed in the background on open (`/api/warm_full`, `engine.warm_full`);
  `raw.decode(quick=True)` uses 1-pass X-Trans for Fuji zoom (exports keep 3-pass).
- `filmlook/recipes.py` – JSON looks in `looks/` (built-in) and `looks/user/` (saved), `extends`, overrides.
- `filmlook/engine.py` – cached previews, contact sheets, strip-based full-res export with EXIF copy.
- `filmlook/lut.py` – bake a look to a 3D LUT (.cube export; also speeds up exports).
- `filmlook/reference.py` – make a look from a reference image (OKLab statistics transfer).
- `filmlook/geometry.py` – crop & straighten (flips/90° -> fine angle -> crop as fractions; slot for
  perspective). `filmlook/edits.py` stores edits in the library DB (`edits`, keyed drive + path),
  never in the photo. Preview, thumbnails, export and the CLI all apply them. Grade-tab grades (look +
  adjust incl. curve) are remembered per file in `grades` (`edits.get_grade/put_grade`); batch export
  (`own`) and essay exports use them; the Library shows Edited (graded in app, not exported).
- JPGs open in Grade too: `raw.decode` handles them and `pipeline.base(display=True)` keeps a
  neutral JPG unchanged (no auto exposure / contrast).
- `filmlook/server.py` + `static/index.html` – local browser app (127.0.0.1 only): Library (search + browse in one, sidebar filters; Show: Worth a
  second look / On this day via `/api/library/second` → `secondlook.suggestions(undecided=True)`), Cull,
  Grade and Essays tabs, in workflow order (the old Second look tab was removed; its views live in the Library). Library → Cull this shoot /
  Grade picks; Cull summary lists `cull.second_look` frames.
- `filmlook/library/` – searchable archive. Database + thumbnails live in
  `~/Library/Application Support/Film Look Studio/` (internal disk; never write next to photos).
  `db.py` schema (photos ← files, sidecars, embeddings; later features attach to `photos.id`),
  `drives.py` (drives by volume UUID; offline = not mounted), `scan.py` (incremental, stoppable),
  `pairing.py` (RAW + copies + exported JPGs = one photo), `sidecars.py` (ON1/XMP, read only),
  `thumbs.py` (embedded RAW previews), `vision.py` (SigLIP 2 on MPS, categories),
  `index.py`, `search.py`, `service.py` (background scan job for the app),
  `quality.py` (focus/motion/exposure from embedded previews, burst groups),
  `secondlook.py` (second-look finder: cross-fitted taste model + zero-shot appeal; keep/dismiss in `picks`).
  `scene.py` (Apple Vision via pyobjc: people/faces/eyes/pose/subject -> culling features),
  `cull.py` (groups, within-group ranking + reasons, sessions, learning weights from picks),
  `marks.py` (THE place for pick / reject / stars: Cull, Second look "Keep" and the Library all
  use it, so they always agree; every change is logged in `cull_log`).
  `browse.py` (Library's shoot list / graded thumbs / Finder helpers: "Show in Finder" selects originals +
  copies + sidecars via osascript so YOU delete in Finder — the app never deletes; after a rescan,
  photos whose original is gone can be forgotten). `pairing.py` accepts whole-hour time-zone
  shifts when names match (-IG exports); `scan.merge_into_owner` keeps marks/essay places on merge.
  `essay.py` (photo essays: display image = newest graded export else library thumb; per-photo
  shape/colour/light/gesture/pace features; shot size `shot_scale` (close/medium/full/wide + sure: Vision
  person height, face size, feet/knees from pose; else SigLIP prompts z-scored over the library; SCALE_VERSION
  re-labels cached features; unsure sizes are never claimed in reasons); pairs (rhymes + contrasts, ranked per type, good/bad
  marks); sequence styles flow / time / mood with locked positions; fill-the-gap; leave-outs;
  versions; `choose` / `create_from_brief` / `bench` / `replacements` / `rechoose`: best-N from a pool to a
  brief with variety, B&W via `is_mono`, theme words steer; Library → Make an essay / Add to essay…). Essays tab: strip / contact sheet / spreads (page 1 alone on the right), drag, lock,
  undo, versions. `essay_out.py`: PDF contact sheet (thumbnails, works offline) and numbered export
  (01_title.jpg: graded JPG resized, else RAW + essay look + crop via `engine.render_full`) into a
  new folder under ~/Pictures/Film Look Studio Essays; waits while a drive is offline. `essay_book.py`:
  print-ready book PDF (A4 / 8x10, portrait / landscape; border or full-bleed; cover, captions, page
  numbers, index, 3 mm bleed with TrimBox) following the spreads, 300 dpi, streamed page by page
  (own tiny PDF writer, one JPEG per page). Library also shows a new-shoot notice (`scan.new_shoots`).
- `filmlook/publish.py` – WordPress publishing (REST API + Application Password in the macOS Keychain; site/user
  in LIBRARY_DIR/publish.json). Essays → Publish tab: post details saved in essays.source.publish, local
  preview, Upload as draft = web JPGs (GPS stripped) to the Media Library + a DRAFT post copying the
  site's essay layout (Spectra container + Visual Portfolio fade slider, `VP_SETTINGS`). Only ever adds
  media and drafts; never edits or deletes site content.
- `filmlook/appsettings.py` – ⚙ Settings: defaults (look, export size/quality), storage (preview cache
  capped, default 4 GB, trimmed on start and as it grows; views; old DB copies), backups of your work to
  <drive>/Film Look Studio Backups (newest 10). Header: activity indicator (`/api/activity`), ? shortcuts.
- Essays panel groups: Arrange (pairs, bench, fill the gap) · Brief · Output (export, book PDF, contact
  sheet, publish). Essays are made from the Library only. Grade: undo/redo, copy/paste a grade, double-click
  a slider to reset, list filter, Next not graded, before/after split.
- PyTorch model calls must go through `vision` (one at a time: concurrent MPS use segfaults).
- The app's open folder / collection is global server state: test on another port (e.g. 8766).
- Library CLI: `./studio library add|scan|status|search|folders`.

## Checks

```bash
.venv/bin/python -m pytest -q
```
