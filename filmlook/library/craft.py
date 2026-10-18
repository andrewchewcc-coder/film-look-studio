"""Photobook craft: sequencing guidelines drawn from classic street photobooks, as scores the
arranger optimises and as a checklist for the Essays tab.

Guidelines (and the books they come from):
 1 open with a world: a strong, readable wide/medium photo, not the busiest (Frank, Parr)
 2 close with resonance: strong but quieter, or echoing the opener (Frank, Koudelka)
 3 chapters for long essays, each with its own opener (Frank, Cartier-Bresson)
 4 waves of density: no more than 2 busy frames in a row (Klein vs Leiter)
 5 shot-size rhythm: no 3 of the same size in a row
 6 hero images get a spread to themselves (Frank, Webb) — used by the book PDF
 7 rhyme or contrast, never repeat: no near-identical photos
 8 motifs as a refrain: a recurring subject spaced out, never side by side (Frank, Koudelka)
 9 gentle colour flow: no big colour jumps, except at chapter breaks (Leiter, Webb)
10 an arc in light or time (Parr) — the Day → night / Calm → intense styles
11 one look throughout: all colour or all B&W (Koudelka, Parr, Davidson)
12 edit tight: one frame per moment, cut the weakest
13 a deliberate rupture is allowed (Moriyama) — the Juxtaposition style

Styles weigh them differently: Classic (Frank / Cartier-Bresson), Quiet flow (Leiter / Webb),
Juxtaposition (Klein); Visual flow and the arcs use the basics.
"""
import numpy as np

from . import vision

MOTIFS = {"umbrella": "umbrellas", "flag": "flags", "bicycle": "bicycles", "scooter or motorbike": "scooters",
          "dog": "dogs", "cat": "cats", "bird": "birds", "child": "children", "reflection in glass": "reflections",
          "long shadows": "shadows", "staircase": "stairs", "tram or train": "trams and trains", "bus": "buses",
          "taxi or car": "cars", "lanterns": "lanterns", "smoke or incense": "smoke", "food stall": "food stalls",
          "person on a phone": "phones", "hat": "hats", "street sign or lettering": "signs and lettering",
          "window": "windows", "crowd": "crowds", "lone figure": "lone figures", "elderly person": "older people"}
MOTIF_Z = 3.2              # this far above the library typical = the photo shows it
COLOUR_JUMP = 0.12         # OKLab distance between neighbours' average colours that counts as a jump
STYLES = {
    #               rhyme contrast monotony busy  opener closer colour motif_adj motif_spread breathe chapters
    "classic":      (1.0, 0.2, -0.25, -0.45, 0.45, 0.40, -1.0, -0.6, -0.5, 0.2, True),
    "quiet":        (1.3, 0.0, -0.15, -0.60, 0.35, 0.45, -2.5, -0.4, -0.2, 0.5, False),
    "juxtaposition": (0.4, 1.2, -0.30, -0.10, 0.40, 0.30, 0.0, -0.3, -0.2, 0.0, False),
    "flow":         (1.0, 0.0, -0.25, -0.30, 0.35, 0.30, -0.5, -0.4, -0.2, 0.0, False),
}
STYLE_NAMES = {"classic": "Classic", "quiet": "Quiet flow", "juxtaposition": "Juxtaposition", "flow": "Visual flow"}
_motif_cache = {}


def motifs(con, ids):
    """{photo_id: {motif label}}: recurring street subjects, found with SigLIP against the library."""
    from .search import _matrix, query_vector, CLOSEUP_PENALTY
    lib_ids, mat = _matrix(con)
    if not len(lib_ids):
        return {p: set() for p in ids}
    if _motif_cache.get("n") != len(lib_ids):
        hits = {}
        for prompt, label in MOTIFS.items():
            s = mat @ query_vector(prompt)
            sc = np.maximum(s[:, 0], s[:, 1:].max(axis=1) - CLOSEUP_PENALTY)
            z = (sc - np.median(sc)) / max(float(sc.std()), 1e-6)
            hits[label] = {int(i) for i in lib_ids[z >= MOTIF_Z]}
        _motif_cache.update(n=len(lib_ids), hits=hits)
    return {p: {lab for lab, s in _motif_cache["hits"].items() if p in s} for p in ids}


def _mean_lab(fv):
    pal = np.asarray(fv["colour"]["palette"] or [[0.5, 0, 0]], float)
    w = np.asarray(fv["colour"]["weights"] or [1.0], float)
    return (pal * w[:, None]).sum(0) / max(w.sum(), 1e-9)


def context(con, ids, f, T, zst):
    """Everything the rules need, precomputed once per essay."""
    from . import essay
    n = len(ids)
    vec = essay.vectors(con, ids)
    # Busy = dense for THIS essay (street photos are mostly busy in absolute terms): the top ~40% by
    # edge detail plus people, and never a frame that is calm in absolute terms.
    dens = np.array([f[p]["pace"]["busy"] / 0.035 + 0.15 * min(f[p]["pace"]["people"], 8) for p in ids])
    cut = float(np.percentile(dens, 60)) if n else 0.0
    busy = [bool(d > cut and d > 0.8) for d in dens]
    scale = [f[p]["pace"]["scale"] if f[p]["pace"].get("scale_sure", True) else None for p in ids]
    lab = np.stack([_mean_lab(f[p]) for p in ids]) if n else np.zeros((0, 3))
    cd = np.linalg.norm(lab[:, None] - lab[None], axis=2) if n else np.zeros((0, 0))
    sim = np.zeros((n, n))
    con_m = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ids[i], ids[j]
            if a in vec and b in vec:
                sim[i, j] = sim[j, i] = float(vec[a] @ vec[b])
            c, _ = essay._contrast(f[a], f[b])
            con_m[i, j] = con_m[j, i] = c or 0.0
    mono = [essay.is_mono(f[p].get("thumb")) for p in ids]
    mot = motifs(con, ids)
    common = {m for m in MOTIFS.values() if sum(m in mot.get(p, ()) for p in ids) > max(2, 0.4 * n)}
    mot = {p: ms - common for p, ms in mot.items()}            # in most photos = the theme, not a refrain
    return {"ids": ids, "idx": {p: k for k, p in enumerate(ids)}, "T": T, "z": [zst[p] for p in ids],
            "busy": busy, "scale": scale, "light": [essay._pace_class(f[p])[2] for p in ids], "cd": cd,
            "sim": sim, "contrast": con_m, "mono": mono, "motifs": [mot.get(p, set()) for p in ids]}


def chapter_starts(n, chapters=None):
    """Positions where chapters begin: 1 chapter up to 20 photos, 2 up to 32, else 3."""
    k = chapters or (1 if n <= 20 else 2 if n <= 32 else 3)
    return sorted({round(i * n / k) for i in range(k)})


def _opener_value(ctx, i):
    v = ctx["z"][i]
    if ctx["scale"][i] in ("wide", "medium", "full"):
        v += 0.5
    if ctx["scale"][i] == "close":
        v -= 0.5
    if ctx["busy"][i]:
        v -= 0.7
    return v


def _closer_value(ctx, i, first):
    v = 0.8 * ctx["z"][i] + (0.0 if ctx["busy"][i] else 0.5)
    if first is not None and first != i and 0.6 <= ctx["sim"][first, i] < 0.9:
        v += 0.6                                                # echoes the opener: a bookend
    return v


def score(order, ctx, style="flow"):
    """How well an order follows the guidelines, weighted for a style (higher is better)."""
    w = STYLES.get(style, STYLES["flow"])
    w_r, w_c, w_mono, w_busy, w_open, w_close, w_col, w_madj, w_mspread, w_breathe, use_ch = w
    pos = [ctx["idx"][p] for p in order]
    n = len(pos)
    if not n:
        return 0.0
    T, cd, cm = ctx["T"], ctx["cd"], ctx["contrast"]
    breaks = set(chapter_starts(n)) if use_ch else {0}
    s = 0.0
    run = 0
    for k in range(n):
        i = pos[k]
        run = run + 1 if ctx["busy"][i] else 0
        if run >= 3:
            s += w_busy                                         # waves of density
        if k + 1 < n:
            j = pos[k + 1]
            s += w_r * max(T[i, j], -2.0)
            spread = k % 2 == 1                                 # pages (2,3), (4,5)… face each other
            s += w_c * cm[i, j] * (1.5 if spread else 1.0)
            if k + 1 not in breaks:
                s += w_col * max(0.0, cd[i, j] - COLOUR_JUMP) * 5
            if ctx["motifs"][i] & ctx["motifs"][j]:
                s += w_madj
            if w_breathe and ctx["busy"][i] and ctx["z"][i] > 0.7 and not ctx["busy"][j]:
                s += w_breathe                                  # a dense strong frame, then room to breathe
        if k + 2 < n:
            a, b, c = i, pos[k + 1], pos[k + 2]
            if ctx["scale"][a] and ctx["scale"][a] == ctx["scale"][b] == ctx["scale"][c]:
                s += w_mono
            if ctx["light"][a] == ctx["light"][b] == ctx["light"][c]:
                s += w_mono * 0.5
    s += w_open * _opener_value(ctx, pos[0]) + w_close * _closer_value(ctx, pos[-1], pos[0])
    o, c = pos[0], pos[-1]                                      # the checklist's opener / closer tests
    if ctx["busy"][o] or ctx["scale"][o] == "close" or ctx["z"][o] < 0:
        s -= 1.5
    if (ctx["busy"][c] and ctx["z"][c] < 0.7) or ctx["z"][c] < -0.3:
        s -= 1.0
    if use_ch:
        for b in breaks - {0}:
            s += 0.3 * _opener_value(ctx, pos[b])
    if w_mspread:                                               # motifs evenly spaced through the essay
        seen = {}
        for k, i in enumerate(pos):
            for m in ctx["motifs"][i]:
                seen.setdefault(m, []).append(k)
        for ks in seen.values():
            if len(ks) >= 3:
                gaps = np.diff(ks)
                s += w_mspread * float(gaps.std() / max(gaps.mean(), 1e-6))
    return s


def failures(order, ctx, rule):
    """How badly one guideline is broken (0 = fine): what a Fix button aims to bring to zero."""
    pos = [ctx["idx"][p] for p in order]
    n = len(pos)
    bad = 0
    if rule == "Density":
        run = 0
        for i in pos:
            run = run + 1 if ctx["busy"][i] else 0
            bad += run >= 3
    elif rule == "Shot size":
        bad = sum(1 for k in range(n - 2) if ctx["scale"][pos[k]] and
                  ctx["scale"][pos[k]] == ctx["scale"][pos[k + 1]] == ctx["scale"][pos[k + 2]])
    elif rule == "Motifs":
        bad = sum(1 for k in range(n - 1) if ctx["motifs"][pos[k]] & ctx["motifs"][pos[k + 1]])
    elif rule == "Colour flow":
        breaks = set(chapter_starts(n)) if n > 20 else {0}
        bad = sum(1 for k in range(n - 1) if k + 1 not in breaks and ctx["cd"][pos[k], pos[k + 1]] > COLOUR_JUMP * 1.6)
    return bad


def check(order, ctx, f):
    """The guidelines as a checklist: [{rule, ok, text, positions, fix}] (positions are 1-based).
    fix: "move" (re-arrange just those photos), "opener" / "closer", "remove" (photo id), or None."""
    pos = [ctx["idx"][p] for p in order]
    n = len(pos)
    out = []
    if not n:
        return out

    def add(rule, ok, text, positions=(), fix=None, photo=None):
        out.append({"rule": rule, "ok": ok, "text": text, "positions": [p + 1 for p in positions],
                    "fix": fix, "photo": photo})

    o, c = pos[0], pos[-1]
    bad = [why for cond, why in ((ctx["busy"][o], "busy"), (ctx["scale"][o] == "close", "a close-up"),
                                 (ctx["z"][o] < 0, "not one of the stronger photos")) if cond]
    add("Opener", not bad, "The opener sets the world: strong and readable." if not bad else
        f"The opener is {', '.join(bad)}. A strong, readable wide or medium photo sets the world.",
        [0], None if not bad else "opener")
    bad = [why for cond, why in ((ctx["busy"][c] and ctx["z"][c] < 0.7, "a busy frame"),
                                 (ctx["z"][c] < -0.3, "one of the weaker photos")) if cond]
    echo = 0.6 <= ctx["sim"][o, c] < 0.9
    add("Closer", not bad, ("The closer echoes the opener — a bookend." if echo else
                            "The closer is strong and lets the essay settle.") if not bad else
        f"The closer is {', '.join(bad)}. End on something strong but quieter, or echo the opener.",
        [n - 1], None if not bad else "closer")
    if n > 20:
        starts = chapter_starts(n)
        add("Chapters", True, f"{n} photos: think in {len(starts)} chapters — Classic starts them at "
            + ", ".join(f"#{s + 1}" for s in starts) + ".", starts)
    runs, k = [], 0
    while k < n:
        if ctx["busy"][pos[k]]:
            e = k
            while e + 1 < n and ctx["busy"][pos[e + 1]]:
                e += 1
            if e - k + 1 >= 3:
                runs.append((k, e))
            k = e + 1
        else:
            k += 1
    for a, b in runs:
        add("Density", False, f"{b - a + 1} busy frames in a row (#{a + 1}–#{b + 1}). Give the eye a quieter one.",
            list(range(a, b + 1)), "move")
    if not runs:
        add("Density", True, "Busy and quiet frames alternate in waves.")
    from .essay import SCALE_NAMES
    reps, k = [], 0
    while k < n - 2:                                            # runs of 3+ of the same size
        sc = ctx["scale"][pos[k]]
        e = k
        while sc and e + 1 < n and ctx["scale"][pos[e + 1]] == sc:
            e += 1
        if sc and e - k + 1 >= 3:
            reps.append((k, e, sc))
        k = e + 1
    for a, b, sc in reps[:3]:
        add("Shot size", False, f"{b - a + 1} {SCALE_NAMES[sc]} shots in a row (#{a + 1}–#{b + 1}).",
            list(range(a, b + 1)), "move")
    if not reps:
        add("Shot size", True, "Shot sizes change in a rhythm.")
    dups = [(a, b) for a in range(n) for b in range(a + 1, n) if ctx["sim"][pos[a], pos[b]] >= 0.90]
    for a, b in dups[:4]:
        weaker = a if ctx["z"][pos[a]] < ctx["z"][pos[b]] else b
        add("No repeats", False, f"#{a + 1} and #{b + 1} are nearly the same picture — keep one.",
            [a, b], "remove", order[weaker])
    if not dups:
        add("No repeats", True, "Every photo is its own moment.")
    adj = [(k, sorted(ctx["motifs"][pos[k]] & ctx["motifs"][pos[k + 1]])) for k in range(n - 1)
           if ctx["motifs"][pos[k]] & ctx["motifs"][pos[k + 1]]]
    for k, ms in adj[:4]:
        add("Motifs", False, f"#{k + 1} and #{k + 2} both show {' and '.join(ms)} — space them out.",
            [k, k + 1], "move")
    seen = {}
    for k, i in enumerate(pos):
        for m in ctx["motifs"][i]:
            seen.setdefault(m, []).append(k)
    refrains = [(m, ks) for m, ks in seen.items() if len(ks) >= 3]
    for m, ks in refrains[:2]:
        if not any(set(ks) & {k, k + 1} for k, _ in adj):
            add("Motifs", True, f"A refrain: {m} at " + ", ".join(f"#{k + 1}" for k in ks) + ".", ks)
    if not adj and not refrains:
        add("Motifs", True, "No subject repeats side by side.")
    breaks = set(chapter_starts(n)) if n > 20 else {0}
    jumps = [k for k in range(n - 1) if k + 1 not in breaks and ctx["cd"][pos[k], pos[k + 1]] > COLOUR_JUMP * 1.6]
    for k in jumps[:3]:
        add("Colour flow", False, f"A big colour jump from #{k + 1} to #{k + 2}.", [k, k + 1], "move")
    if not jumps:
        add("Colour flow", True, "Colours step gently from photo to photo.")
    m = sum(ctx["mono"][i] for i in pos)
    if 0 < m < n:
        few, many = ("black & white", "colour") if m <= n - m else ("colour", "black & white")
        add("One look", False, f"{min(m, n - m)} {few} photo{'s' if min(m, n - m) > 1 else ''} among {max(m, n - m)} "
            f"{many}. Grade them to match, or make the whole essay one or the other.",
            [k for k, i in enumerate(pos) if ctx["mono"][i] == (few == "black & white")])
    else:
        add("One look", True, "One look throughout.")
    weak = sorted((k for k in range(n) if ctx["z"][pos[k]] < -1.0 and ctx.get("weak", [True] * len(ctx["z"]))[pos[k]]),
                  key=lambda k: ctx["z"][pos[k]])                # weak here AND in your library's bottom quarter
    for k in sorted(weak[:2]):
        add("Edit tight", False, f"#{k + 1} is one of the weakest photos — the essay may be stronger without it.",
            [k], "remove", order[k])
    if not weak:
        add("Edit tight", True, "Every photo earns its place.")
    return out


def heroes(ctx, order, share=0.12):
    """The strongest photos (about 1 in 8, at least one), in essay order: these get a spread alone."""
    n = len(order)
    k = max(1, round(n * share))
    top = sorted(range(n), key=lambda i: -ctx["z"][ctx["idx"][order[i]]])[:k]
    return [order[i] for i in sorted(top)]
