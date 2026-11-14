# Film Look Studio

Local app that applies film looks to RAW (DNG) photos, previews them, and exports
full-resolution JPGs. Owner: Andrew (street photographer, Leica Q3 43, macOS).

- When Andrew asks for grading help (suggest a look, tweak in plain English, match a
  reference or a photographer, export, make a LUT), use the `grade` skill.
- Mac app: `mac-app/make_app.sh` builds /Applications/Film Look Studio.app + a Desktop alias: a native Swift app
  (`mac-app/FilmLookStudio.swift`, swiftc; project folder in Info.plist FLSProjectDir) with its own window (WKWebView on
  http://127.0.0.1:8765, size remembered), Dock icon and menu bar (Edit, View zoom/reload, Help › Open in Browser / Show
  Log). It runs `mac-app/launch.sh start` (server in the background with `--auto-stop`, log in ~/Library/Logs/Film Look
  Studio.log; readiness checked over HTTP — lsof triggers macOS permission prompts). ⌘Q / closing the window posts
  `/api/quit`; if a job runs it finishes in the background. The page hides its Quit button there (`NATIVE`, via the
  `fls` message handler). JS alert/confirm/prompt, file choosers, downloads (→ ~/Downloads) and other sites / new
  windows (→ default browser) are handled by the Swift delegate. `--auto-stop` stops after 10 min with no page open
  (pages hit `/api/ping` every minute), never while a job runs. Rebuilding changes the ad-hoc signature, so macOS
  asks for Documents / Removable Volumes again: change launch.sh or the page instead when possible.
- Colours: every colour in index.html comes from the :root tokens (dark) and their `prefers-color-scheme: light`
  twins, so the page follows the Mac's Appearance; canvases read them with `cssVar()`. Overlays on photos stay dark.
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
  Grade picks; Library Browse = a Finder-style tree (`lbTreeDraw`: year folders open / close, state in
  localStorage fls.treeOpen; the hidden `#lbWhere` select still holds the value). The viewer's Open in Grade opens just that
  photo (a one-photo collection via `/api/library/grade`), not its folder. Cull frames lay out in the grid that shows them
  largest (`cuLayout`, e.g. 2×2 for four landscape frames). Groups of more than 4 frames go page by page (`CU_PAGE`, ‹ › in the footer), and
  P picks and stays in every group (moves to the next frame) so several can be picked; Shift+P (or Shift-click ✓ Pick) picks and goes to the next group (`cuPick(next)`). Each card
  has ✓ Pick / ✕ buttons; cards are as wide as their photo (`cuFit`) and rejects are only lightly faded. Review picks
  (Cull top bar and the Cull finished page, `cuPicksMode`) shows all the cull's picks as one virtual group (`CU.picks`, returned by
  `cuGroup`) in the same cards, 4–12 per page (`cuPage`); X rejects, Esc goes back. B toggles a black-and-white preview in Cull. Library search box sits wide above the grid (⌘F); the grid hides
  file names unless Show file names is ticked (fls.names) and marks are small round icons (✓ ✕ G E). Grade: RGB histogram with
  clipping warnings at the top of the side panel (`drawHisto`), sliders grouped Light / Colour / Effects / Detail (`SLIDER_GROUPS`).
  Cull summary lists `cull.second_look` frames. Cull Compare (C / Compare button): two frames of the
  group at 100% from `/api/cull/full`, panned together by a shared centre fraction (`CMP`); P/X/0–5 mark the chosen side.
- `filmlook/library/` – searchable archive. Database + thumbnails live in
  `~/Library/Application Support/Film Look Studio/` (internal disk; never write next to photos).
  `db.py` schema (photos ← files, sidecars, embeddings; later features attach to `photos.id`),
  `drives.py` (drives by volume UUID; offline = not mounted), `scan.py` (incremental, stoppable),
  `pairing.py` (RAW + copies + exported JPGs = one photo; a camera JPG from RAW+JPG mode — firmware in
  Software, modify time = capture time, no editor history, not in graded/ — joins as role 'camera' and doesn't
  make the photo graded; the app's exports get Software=Film Look Studio), `sidecars.py` (ON1/XMP, read only),
  `thumbs.py` (embedded RAW previews), `vision.py` (SigLIP 2 on MPS, categories),
  `index.py`, `search.py` (`understand` splits a search: years/months filter, colour words use `colours.py`
  shares (B&W = graded versions via `colours.graded_bw`), shoot-folder words boost that shoot, the rest goes
  to SigLIP; near-identical frames of a shoot are grouped at the end with "+N alike"; `like`/`unlike` ids = the Library's More like this / Not this steering,
  z-scored photo likeness added to / taken from the words' scores), `service.py` (background scan job for the app),
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
  (own tiny PDF writer, one JPEG per page). `printing`: screen / printer (cover + inside pages files) / duplex /
  zine-a5 / zine-a4 (`reading_order` adds the blank inside cover; `imposition` = booklet order, portrait pages
  two per A4 / A3 sheet); pages render to a temp folder first; `job['howto']` = the printing steps shown when done. Library also shows a new-shoot notice (`scan.new_shoots`).
- `filmlook/library/craft.py` – photobook craft: 13 guidelines from classic street photobooks (Frank, Cartier-Bresson,
  Klein, Levitt, Koudelka, Moriyama, Parr, Leiter, Webb, Davidson) as `score` (styles classic / quiet /
  juxtaposition / flow weigh them) and `check` (the Essays → Arrange → Check list; `failures` per rule for Fix).
  Busy is relative to the essay; motifs = SigLIP prompts z-scored over the library (theme motifs in >40% ignored);
  heroes (≈1 in 8 strongest) get a spread alone in the book PDF (`heroes=True`); `make_book(preview=True)` = small pages from thumbnails for the Book panel's Preview. `essay.sequence` optimises
  `craft.score` for every style but time; `essay.craft_check` / `craft_fix` back /api/essays/{id}/check and /fix.
- `filmlook/publish.py` – WordPress publishing (REST API + Application Password in the macOS Keychain; site/user
  in LIBRARY_DIR/publish.json). Essays → Publish tab: post details saved in essays.source.publish, local
  preview, Upload as draft = web JPGs (GPS stripped) to the Media Library + a DRAFT post copying the
  site's essay layout: Gallery style `carousel` (default; Spectra Image Gallery carousel, `SPECTRA_GALLERY`, from "Fire Dragon
  Festival 2026") or `slideshow` (Spectra container 89% + Visual Portfolio fade slider, `VP_SETTINGS`, from "Bedok Jetty"). Only ever adds
  media and drafts; never edits or deletes site content.
- `filmlook/appsettings.py` – ⚙ Settings: defaults (look, export size/quality), storage (preview cache
  capped, default 4 GB, trimmed on start and as it grows; views; old DB copies), backups of your work to
  <drive>/Film Look Studio Backups (newest 10). Header: activity indicator (`/api/activity`), ? shortcuts.
- Essays panel groups: Arrange (pairs, bench, fill the gap) · Brief · Output (export, book PDF, contact
  sheet, publish). Essays are made from the Library only. Grade: undo/redo, copy/paste a grade, double-click
  a slider to reset, list filter, Next not graded, before/after split; when a camera JPG sits beside the RAW
  (`server._camera_jpg`, `pairing.is_camera_jpg`), Hold: camera JPG (J) and Split camera JPG / grade
  (`/api/render?source=camera`: the JPG as shot, with the photo's crop).
  Grade side panel sections fold by their headings (and Tone curve / Colour mixer / Masks boxes; `foldSetup`, state
  in localStorage fls.gradeFold; badges show masks / mixer in use); the status line sits at the top of the aside so
  it shows in crop mode too. Colour mixer (HSL: Hue / Saturation / Luminance × 8 bands) = adjust `hsl` → recipe `color.user_hsl`, applied after the
  look's own `color.hsl` with sharper tent bands (`pipeline._tent_weights`; looks keep their soft bands). Colour looks only.
  Detail sliders (Noise reduction / Colour noise / Sharpen) = adjust → recipe `detail` → `detail.py` on the neutral image
  (before the look, so grain isn't touched), sized in full-res pixels via `detail_scale`; strips / tiles borrow
  `detail.margin` rows of context (`pad`) so exports have no seams. Invisible at fit size: judge at 100%.
  Crop & straighten → Auto-straighten: `straighten.estimate` (Canny + fine Hough on near-vertical lines, robust fit
  lean = tilt + k·offset so keystone from looking up isn't read as tilt) via `/api/auto_straighten`; below
  `straighten.SURE` confidence it only reports. Checked against Andrew's hand-set angles: within 0.5° on 11 of 17.
  Masks (subject / people / sky) = adjust `masks` [{kind, invert, exposure, contrast, warmth, saturation}] → recipe
  `masks` → `pipeline.local` at the end of `pipeline.render` (after the look strength). `masks.py`: found once per photo
  on a 1536 px neutral preview (Vision foreground instances; people = instances overlapping VNDetectHumanRectangles +
  person instance + person segmentation; sky = blue / pale smooth regions touching the top edge), guided-filter edges,
  cached as PNGs in LIBRARY_DIR/masks. `masks.alphas` maps them onto any box of the final frame (crop/straighten
  applied), so `engine._masks_at` feeds previews, zoom tiles and export strips alike. `/api/mask_info`, `/api/mask_overlay`.
- `filmlook/claude_ai.py` – Claude in the app (phase 2): API key only in the Keychain (⚙ Settings → Claude, checked free via
  models.list), Anthropic SDK, model claude-opus-5-5 with `fallbacks: "default"`, structured JSON answers (`ask_json`),
  photos sent only as ≤1024 px re-encoded JPEGs (no EXIF/GPS), cost per call logged by month in LIBRARY_DIR/claude_usage.json.
  `library/essay_ai.py`: Essays → Arrange → AI editor — Claude reviews the essay like a photobook editor (overall,
  opening, ending, keep/maybe/cut + note + caption per photo, moves, 3 titles, intro draft), saved in
  essays.source.ai_review; apply moves / remove / captions → titles / title + intro → the website post.
  `grade_ai.py`: Grade → Describe the grade — Claude sees the current render + settings + your looks and returns a
  complete grade (look, sliders, colour mixer, masks; curve kept), clamped to the slider ranges (`/api/grade_ai`); the page
  applies it as one undo step.
  Alt text: Publish → Write alt text with Claude (`essay_ai.write_alt_text`, one request for the essay, effort low) →
  publish.alt {photo_id: text}, editable in the Publish panel; Upload as draft sets each photo's WordPress alt_text from it.
- PyTorch model calls must go through `vision` (one at a time: concurrent MPS use segfaults).
- The app's open folder / collection is global server state: test on another port (e.g. 8766).
- Library CLI: `./studio library add|scan|status|search|folders`.

## Checks

```bash
.venv/bin/python -m pytest -q
FLS_E2E=1 .venv/bin/python -m pytest tests/test_e2e.py -q   # before every upload: clicks through every tab
```

`tests/test_e2e.py` runs the app on a throwaway library (FILMLOOK_LIBRARY in a temp folder, generated JPGs) in
Playwright's headless Chromium (`.playwright-browsers/`, not in git; `PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers
.venv/bin/python -m playwright install --only-shell chromium` to reinstall) and fails on any /api/ 404 / 5xx or page
error. Extend it when adding a tab, panel or button that calls the server.

- Cull Compare: Shift-click two frames (`CU.chosen`) then Compare; ‹ › on each side (`cmpStep`); photos are scaled by `cmpScale` so small previews can always be panned; `cuBW` button / B key toggles black & white. Cards no longer show PICK/REJECT tags (the Pick/✕ buttons show state); `cuFit` sizes columns to `max-content` once all photos are measured, and `cuLayout` uses the median aspect.
- Grade layout: photo in the centre, photo list as a filmstrip under it (`#film` inside `#center`: filter / Next not graded / Tick all in `.filmHead`; wheel scrolls sideways; `\` or ▾ Photos hides it, fls.filmHide; `thumbInView` keeps the current one centred), looks as a two-column grid in the Look tab (`#looks`; previews keep the photo's own shape, portrait too, always two columns).
- Grade side panel = a tool strip (`#gTabs`: Look · Adjust · Tone · Masks · Crop · Export; `gradeTab`, remembered in fls.gradeTab) over panes
  (`.gpane`), with the histogram pinned on top (`#gHead`) and Undo / Redo / Copy / Paste pinned below (`#gActions`). Badges via `tabBadge`.
- Drawn masks (Masks tab): Dodge / Burn / Brush (`kind: "brush"`: a painted grey picture of the cropped frame, ~1024 px, uploaded by the page to
  `POST /api/brush` (`masks.save_brush`, LIBRARY_DIR/masks/brush/<id>.png); the grade only keeps the 16-hex `id`, so every stroke is a new id and
  Undo steps stroke by stroke), plus Linear and Radial gradients (a few fractions of the final frame; handles in `#shapeSvg`; formulas in
  `masks._shape_alpha` and the page's `shapeAlpha`, keep them identical). They use the same four sliders as the automatic masks
  (`recipes._mask_entry` validates; `masks.alphas` renders previews, tiles and export strips). Painting UI: `PT` / `PB`, `paintSync`, `#paintCv`
  over `#big`; [ ] size, X erase, Esc done. Paste grade / copy to ticked drop brush masks (they only fit their own photo).
- One notice area: every message goes to `#notice` (top centre; `notifyFrom` / `notify` / `noteAlert`, `status()` is a thin wrapper), kept in the 🔔 history
  (`NOTE.hist`). The old message elements (`NOTE_SINKS`: lbNote, esNote, cuStartMsg, brMsg, aeMsg, setBackupMsg, esPdfMsg, aiGradeNote, pbSaved, status) are
  sinks: `$(id)` installs a textContent / innerHTML setter on them that notifies instead, so old code works unchanged (HTML with buttons works too; buttons
  stay until dismissed). Messages ending in … stay until replaced or cleared (""); others fade after ~5 s + 45 ms per character (errors 12 s). `alert()` is gone:
  use `noteAlert`. New messages: call `status(text, isError)`; don't add new note elements.
- Library shows day headers (`lbDayHead`: "Sun, 1 Nov 2026 · Shoot name") in the plain, unsearched list only. Grade has a B&W preview toggle (W key / `#zBW`, `gradeBW`; CSS filter on the viewer, never saved).
  Grade's photo list (`.thumb`) and Cull's strip (`.cuTile`) share one look: 2 px border, green pick / red reject (`/api/state` `marks` ← `edits.marked_names`), accent outline for the current photo, small dark corner chips.
- Drag out (Mac app only, `DRAG` in the page + `DragWebView` / `FileDragSource` in `FilmLookStudio.swift`): the page posts `{drag: [{url, name}]}` once a press on a Library cell / Grade thumb moves 10 px; the app starts a file-promise
  drag and downloads each JPG from `/api/library/dragfile/{id}` or `/api/dragfile?name=` when dropped. `library/dragout.py` makes a COPY (exported JPG, else the graded photo, 4000 px, cached a day in LIBRARY_DIR/drag): never offer an original, Finder moves on same-disk drops.
- Keys are the same across tabs: P / X / U / 1–5 mark in Library, Cull and Grade (Grade: `gradeMark` → `POST /api/mark_file`; 0 stays Fit there), ← → previous / next (Grade: `gradeStep`, follows the strip filter),
  V = black & white preview (Library viewer `lbBW`, Cull `cuBW`, Grade `gradeBW`), Z zoom, ? shortcuts. ⌘K = go anywhere (`goOpen`: tabs, commands, Library views, shoots from `#lbWhere`, essays, culls).
- Essays: right-hand panel `#esInsp` (title, count, look `#esInLook` → PATCH look, versions, arrange style, brief via `esBriefPanel` in `#esInBrief`); the bottom panel keeps Arrange + Output only.
- Drawn masks follow crop changes: brush masks carry `geom` (the crop they were painted under; `recipes._mask_entry` keeps it) and `masks._brush_remapped` traces each pixel
  through `geometry.frame_to_source` / `source_to_frame`; `/api/brush/{id}.png?name=&g=` shows it on the current crop. Gradients are moved on crop save (`refitMasks` → `/api/masks/refit` → `masks.refit`).
- Cull frames per page (`cuPage` / `cuSlots`, never more than two rows: `CU_ROWS`, Review picks too): a group that fits at a readable size (long side ≥ 360 px,
  short ≥ 230) shows whole; bigger ones page with at least 4 a page, the most frames that still fill the screen within 10% of the best (full 4 × 2 / 5 × 2 grids).
  Every page full but the last, which keeps the full pages' grid (`CU.slots`, fixed column / row tracks in `cuFit`) so frames keep their size and place. Never a scrolling row.
- Crop → Transform: Vertical / Horizontal perspective (−100…100) = geometry `perspective` {v, h}, applied after the 90° turns and before the fine
  rotation (`geometry._persp`, `persp_inverse` / `persp_forward`, PERSP_K = 0.004; the page's `inImage` / `perspAB` copy the formula — keep them identical).
  `apply`, `valid` / `max_crop`, the mask point mapping and the crop screen's `crop=turn` render all include it; areas outside the warped photo render black.
- "Edited since export" (stale): `search._STALE` = exported (p.graded) and the newest grade / crop `updated_at` is later than the newest exported JPG's mtime.
  `put_grade` / `put` only move `updated_at` when the grade / crop really changes. Shown as the amber G↻ badge + Graded filter "Edited since export" in the
  Library, "re-export" chip + filter in Grade (`/api/state` `stale`, `edits.stale_names`), counted as to-do by Grade picks, and warned about in Essays → Output →
  JPG folder (`essay_out.plan` item `stale`, button opens them in Grade).
