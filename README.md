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

### Library (search all your photos)
```bash
./studio serve
```
Open the **Library** tab, add your photo folders (e.g. `/Volumes/Extreme SSD/2025 Photography`) and
click **Scan**. Then search in plain words ("rain at night", "lone figure in red"), filter by year,
dates, camera, category and graded / not graded, and click a photo to grade it. RAW files and
their exported JPGs count as one photo. Everything stays on your Mac; the database and thumbnails
live in `~/Library/Application Support/Film Look Studio/`, and your photo folders are only read.
Unplugged drives stay searchable from their thumbnails. The first scan downloads the local
search model once (~1.5 GB).

### Browser app (sliders)
```bash
./studio serve ~/Pictures/2026-10-street-walk
```
- Click a thumbnail, then click a look in the strip underneath.
- Sliders: strength, exposure, warmth, tint, contrast, saturation, toning (the gold in
  Tri-X Gold), grain, vignette. Hold **B** to see the "before" image.
- **Save as my look** keeps your tweak for next time.
- **This photo** or **Ticked (n)** exports full-resolution JPGs to `graded/` next to the DNGs,
  keeping camera, lens and date metadata. A `.recipe.json` next to each JPG records the
  exact settings.
- **Match a reference photo**: upload any image whose colour or tone you love.
- **Download LUT**: a `.cube` file for ON1 Photo RAW (grain and vignette aren't part of a LUT).

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
