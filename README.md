# Film Look Studio

Point it at a folder of DNG files, see each photo in your favourite film looks side by
side, tweak, and export full-resolution JPG copies. Runs entirely on your Mac and is free.
Plain-English tweaks ("warmer, deeper reds, like Saul Leiter") come from Claude Code,
using the `grade` skill in this repo.

**Built-in looks:** Classic Chrome · Leica M9 · Tri-X Gold (gritty B&W, golden tint) ·
Saul Leiter style · Fan Ho style · Alex Webb style · Daido Moriyama style ·
Martin Parr style · Joel Meyerowitz style. Every look is
a small JSON recipe you can tweak and save as your own.

## Setup (once)

1. Install [Homebrew](https://brew.sh) if you don't have it.
2. In Terminal:
   ```bash
   git clone https://github.com/<you>/film-look-studio.git
   cd film-look-studio
   ./setup.sh
   ```

## Use it

### Library (search and browse all your photos)
Add your photo folders with **Folders…**, then **Scan for new photos**. Everything is set in the sidebar:
search in plain words, **Browse** all shoots / a year / one shoot, dates, marks (picked / rejected /
not marked), graded or not, stars, camera, and whether to show graded versions or originals. One shoot
is shown in shooting order (like Finder), a search best match first, otherwise newest first.
Click to select (⌘ / Shift for more), double-click or Space for the big viewer (← →, **G** graded ↔
original, **S** side by side), P / X / U and 0–5 to mark. **Show in Finder** opens Finder with the
selected photos' originals and ON1 sidecars selected (one shoot at a time) so you can delete them
there yourself — the app never deletes. Afterwards **Scan for new photos** and the app offers to
remove them from the library. Drives can be unplugged: thumbnails stay on the iMac.
When a new shoot (or new photos in one) turns up on a plugged-in drive, a banner offers **Scan now**, then
**Cull this shoot** once it's ready.

### Second look (rediscover photos you never graded)
In the Library sidebar, **Show → Worth a second look** lists photos you haven't picked, rejected or graded
that deserve another look, best first with reasons, for one shoot, a year or everything. **Show → On this
day** shows photos from today's date in past years. Press **N** (or Not now) to stop a suggestion. After a
cull, the summary also lists frames worth a second look and rejects worth another look.

### Cull (from hundreds of frames to your picks)
In the **Cull** tab choose a shoot, dates, or "new since last cull". Frames of the same scene are grouped;
in each group the app suggests a pick (from subject focus, face and eyes, gesture, edges, separation,
exposure and your taste — using Apple's on-device Vision) and says why. **P** pick, **X** reject, **U**
unmark, **1–5** stars, **← →** frames, **↑ ↓ / Enter** groups, **Z** zoom to 100% where you point (drag to
look around). The strip at the bottom jumps to any group. **Send picks to Grade** opens your picks in order.
After each cull the suggestions re-learn from what you picked, and the agreement rate shows how often
it matched you. Picks, rejects and stars are the same everywhere: change them in Cull, in the Library
(hover a photo: ✓ ✕ ↺ ★, or P / X / U / 1–5) or with Keep in Second look. Rejects are only a flag.

### Essays (sequence a photo essay, zine or exhibition)
Start from the **Library**: select photos (or set up a view, e.g. 2026 + Graded) and click **Make an essay**.
Give a brief: title, number of photos, optional theme words (they steer the choice), colour / B&W / mix,
and at most N per shoot. The app chooses the best photos with variety (your picks, stars, grades and taste;
no near-copies) and lays them out in a suggested order; hover a photo to see why it was chosen. The rest
wait on the **Bench** (with why not): Add them, or Swap. **⇄** on a photo suggests replacements from the
bench; the **Brief** tab re-chooses (locked photos stay). **Add to essay…** in the Library adds a selection.
In the Essays tab: strip (with the reason for each transition), contact sheet (titles) or two-page spreads;
drag, lock, the order styles (Visual flow, Time & place, Day → night, Calm → intense), pairs, Fill the gap,
undo (⌘Z) and versions. **Contact sheet PDF** and **Export…** save to Pictures › Film Look Studio Essays
as 01_title.jpg…: exported photos keep their grade, photos graded in the app use that grade, others get
the essay's look; all with their crop. Drives can be unplugged while you arrange; export waits for them.
**Book PDF** (bottom panel) makes a print-ready zine or book: A4 or 8 × 10 in, portrait or landscape; one photo
per page with a white border or full-bleed; optional cover, captions, page numbers, index page and 3 mm bleed
for online printers. Pages follow the Spreads view (page 1 alone on the right, then facing pairs).

### Browser app (sliders)
```bash
./studio serve ~/Pictures/2026-10-street-walk
```
- Click a thumbnail, then click a look in the strip underneath.
- Zoom from Fit to 400% (slider, pinch, + / −, Z or double-click for 100%) and drag to look around;
  the zoomed view shows your look, crop and grain at full detail.
- Sliders: strength, exposure, warmth, tint, contrast, saturation, toning (the gold in
  Tri-X Gold), grain, vignette. Hold **B** to see the "before" image.
- **Save as my look** keeps your tweak for next time.
- **Crop & straighten** (or press **C**): ratios (3:2, 4:5, 1:1, 16:9, 65:24, free, custom), overlays,
  rotation slider, a level-line tool, 90° turns and flips. **Enter** done, **Esc** cancel, **R** reset.
  Crops are stored in the library, never in your files, and every preview, thumbnail and export uses
  them. **Copy ratio to ticked** gives other photos the same ratio. Works on RAF, DNG and JPG.
- **This photo** or **Ticked (n)** exports full-resolution JPGs to `graded/` next to the DNGs,
  keeping camera, lens and date metadata. A `.recipe.json` next to each JPG records the
  exact settings.
- **Match a reference photo**: upload any image whose colour or tone you love.
- **Download LUT**: a `.cube` file for ON1 Photo RAW (grain and vignette aren't part of a LUT).
Adjust has Highlights, Shadows, Whites and Blacks sliders and a **tone curve** (click to add a point, drag to
move, double-click to remove; presets such as Gentle S or Faded film). Hold **B** (or Hold: before) for the original.

### With Claude Code (AI help, plain English)
Open this folder in the **Code** tab of the Claude desktop app (choose the local folder),
then just talk:

> Suggest a look for ~/Pictures/walk/L1000123.DNG
> B, but warmer and with deeper reds. Save it as "Chinatown Chrome".
> Make a look like Fan Ho and show me it on the three night shots.
> Use ~/Downloads/ref.jpg as a reference on top of Classic Chrome.
> Export the whole folder with Chinatown Chrome.

### Command line
```bash
./studio looks
./studio sheet PHOTO.DNG                       # all looks side by side
./studio preview PHOTO.DNG -l leica-m9 -s color.temp=10
./studio export ~/Pictures/walk -l tri-x-gold  # whole folder
./studio lut -l classic-chrome --out ~/Desktop/classic-chrome.cube
```

## Notes

- Looks are approximations built from the character of each film or camera, not copies of
  Fujifilm's or Leica's proprietary processing.
- Leica DNGs carry lens-distortion corrections that most free RAW decoders ignore; this app
  applies them, so the geometry matches what you see in ON1/Lightroom.
- Your DNGs are never modified.
