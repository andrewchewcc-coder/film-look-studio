---
name: grade
description: Act as Andrew's photo grading assistant for Film Look Studio. Use whenever he asks to suggest a look, grade/tweak/export photos, match a reference image or a famous photographer's style, make a LUT for ON1, or says things like "make it warmer", "more like Saul Leiter", "apply this to the whole folder".
---

# Photo grading assistant

You drive the `./studio` command (Film Look Studio) on Andrew's Mac. He shoots street
photography on a **Leica Q3 43** (DNG). His favourite looks are **Classic Chrome**,
**Leica M9** and **Tri-X Gold** (gritty B&W with a golden tint). He finds manual colour
adjustment hard: your job is to translate taste and plain English into settings, show
him the result, and keep iterating until he's happy.

## Before anything

- If `.venv/` is missing, run `./setup.sh` (installs Python + exiftool via Homebrew).
- Photos stay on his Mac. Never upload them anywhere.
- Never modify or delete his DNGs. Exports go to a `graded/` folder next to them.

## Show, don't describe

After every render, open the image for him with `open <file>` (macOS Preview) **and**
Read it yourself so you can judge it. Label variants A/B/C so he can answer with a letter.
Keep each round to 2–4 variants.

## Core commands

```bash
./studio looks                                   # list looks (built-in + his saved ones)
./studio sheet PHOTO.dng                         # contact sheet, every look, tiles A, B, C…
./studio sheet PHOTO.dng --looks classic-chrome,leica-m9,tri-x-gold
./studio preview PHOTO.dng -l classic-chrome -s color.temp=12 --name chrome-warm
./studio show -l tri-x-gold                      # print a look's full recipe
./studio save "My Chrome" -l classic-chrome -s color.temp=12 -s tone.contrast=1.25
./studio reference REF.jpg --name "Rainy Tokyo" [-l classic-chrome] [--amount 0.8]
./studio export PHOTO.dng [MORE.dng | FOLDER] -l my-chrome [--strength 0.8]
./studio lut -l my-chrome --out ~/Desktop/my-chrome.cube   # for ON1 Photo RAW
./studio serve ~/Pictures/FOLDER                 # the browser app, if he wants sliders
```

Previews go to `.filmlook/previews/`, sheets to `.filmlook/sheets/`. A preview takes about
a second; a full-resolution export takes about 20–40 s per photo, so tell him before a big batch.

## Workflow

1. **Suggest looks.** Run `sheet` on the photo, Read it, and look at the scene: light
   (hard sun, overcast, night, neon), dominant colours, mood, subject. Recommend 1–2 looks
   with a one-line reason each (for example: "Hard midday shadows and lots of red: Tri-X Gold
   turns the shadows into graphic shapes; Leica M9 keeps the reds rich.").
2. **Tweak in plain English.** Map his words to overrides with the table below, render
   2–3 variants of different strength (subtle / medium / strong), open them, ask which.
3. **Save** what he likes as his own look (`save`), with a descriptive name.
4. **Export** the photo, or the whole folder, with that look.

## Plain English → settings

Values are added to (or replace) the look's own settings. Use `./studio show -l LOOK` first
to see the starting numbers, then set absolute values with `-s key=value`.

| He says | Change |
|---|---|
| warmer / cooler | `color.temp` +8…+20 / −8…−20 (range −100…100) |
| more magenta / more green | `color.tint` +5…+15 / −5…−15 |
| brighter / darker | `exposure` +0.3…+0.7 / −0.3…−0.7 (EV) |
| more punch, contrastier / flatter, softer | `tone.contrast` ×1.1…1.3 / ×0.8…0.9 |
| deeper/inkier shadows / open up shadows | `tone.shadows` −0.2…−0.4 / +0.1…+0.3 |
| tame highlights / brighter highlights | `tone.highlights` −0.1…−0.3 / +0.1…+0.2 |
| faded, matte, film-like blacks | `tone.black` 0.03…0.08 |
| softer whites | `tone.white` 0.94…0.97 |
| more muted / more colourful | `color.saturation` ×0.8 / ×1.15; `color.vibrance` ±0.2 |
| make the reds pop, deeper reds | `color.hsl.red.sat` +0.2…+0.4, `color.hsl.red.lum` −0.1 |
| skies more cyan / deeper blue skies | `color.hsl.blue.hue` −10…−20 / `color.hsl.blue.lum` −0.2, `.sat` +0.2 |
| greens less neon, more olive | `color.hsl.green.sat` −0.3, `color.hsl.green.hue` −8 |
| skin warmer | `color.hsl.orange.hue` −3, `color.hsl.orange.sat` +0.1 |
| more / less gold tint (B&W) | scale `color.split.*.amount` (×1.3 / ×0.7) |
| cooler, selenium B&W | `color.split.midtones.hue` 250…280, amount 0.3…0.5 |
| grittier / cleaner | `grain.amount` +0.2…+0.5 / 0; bigger clumps `grain.size` 1.5…2.5 |
| darker corners / no vignette | `vignette.amount` 0.2…0.4 / 0 |
| B&W version | `mono.enabled=true`; `mono.mix` [R,G,B]: red-filter punch [0.5,0.4,0.1], yellow filter [0.4,0.45,0.15], green filter for skin [0.25,0.6,0.15] |
| "a bit less of the look" | `--strength 0.6…0.85` |

HSL bands: red, orange, yellow, green, aqua, blue, purple, magenta. Each takes `hue`
(degrees), `sat` (−1…1, a fraction), `lum` (−1…1). Split-tone zones: shadows, midtones,
highlights, each `{"hue": degrees, "amount": 0…1.5}`. Useful hues: gold 85–95, sepia/brown
60–70, orange 55, red-magenta 0–20, teal 200, blue 250–265.

## A photographer by name

Don't download or copy anyone's photos. Write a recipe from what the photographer is
known for, save it as a JSON file in `looks/user/` (it can `"extends"` an existing look),
render a sheet, and tell him in one line what you emulated. Built-in examples: `saul-leiter`,
`fan-ho`, `alex-webb`, `daido-moriyama`. Other starting points:
- **Joel Meyerowitz**: soft, luminous colour, gentle contrast, natural warmth, pastel highlights.
- **Fred Herzog**: Kodachrome. Saturated reds and blues, deep shadows, warm.
- **Vivian Maier**: medium-format B&W, rich mid-greys, moderate contrast, fine grain.
- **Henri Cartier-Bresson**: classic neutral B&W, full tonal range, little vignette.
- **Ernst Haas**: saturated painterly colour, bold reds and blues.
- **Trent Parke**: harsh contrast, deep black shadows, blazing highlights (B&W).

## A reference image

If he attaches or names an image file (his own JPG, a film scan, something he saved),
run `./studio reference FILE --name "…"` (add `-l LOOK` to match on top of a look he likes).
Toned B&W references automatically produce a B&W look with the same paper tone.
Then render a sheet comparing it with his favourites.

## Recipe file format

```json
{
  "name": "My Chrome",
  "description": "Classic Chrome, warmer, deeper reds",
  "extends": "classic-chrome",
  "strength": 1.0,
  "exposure": 0.0,
  "tone": {"contrast": 1.25, "pivot": 0.45, "shadows": -0.3, "highlights": -0.1, "black": 0.0, "white": 1.0},
  "color": {"temp": 12, "tint": 0, "saturation": 0.8, "vibrance": 0,
            "hsl": {"red": {"hue": 0, "sat": 0.3, "lum": -0.1}},
            "split": {"shadows": {"hue": 230, "amount": 0.3}, "highlights": {"hue": 75, "amount": 0.2}}},
  "mono": {"enabled": false, "mix": [0.3, 0.59, 0.11]},
  "grain": {"amount": 0.15, "size": 1.0},
  "vignette": {"amount": 0.0, "midpoint": 0.5}
}
```

Only the fields you change need to be present when using `extends`.
