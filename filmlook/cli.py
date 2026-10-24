"""Command line: `filmlook <command>`. Run `filmlook -h` for help."""
import argparse
import json
import sys
from pathlib import Path

from . import edits, engine, lut, raw, recipes, reference

WORK_DIR = Path(".filmlook")


def _targets(paths):
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            out.extend(raw.list_images(p))
        elif p.exists():
            out.append(p)
        else:
            sys.exit(f"not found: {p}")
    if not out:
        sys.exit("no photos found")
    return out


def _recipe(args):
    """Build the recipe from --look / --recipe plus --strength and --set overrides."""
    if getattr(args, "recipe", None):
        recipe = recipes.resolve(json.loads(Path(args.recipe).read_text()))
    else:
        recipe = recipes.load_look(getattr(args, "look", None))
    recipe = recipes.apply_sets(recipe, getattr(args, "set", None))
    if getattr(args, "strength", None) is not None:
        recipe["strength"] = args.strength
    return recipe


def cmd_looks(args):
    for lk in recipes.list_looks():
        tag = " (yours)" if lk["user"] else ""
        print(f"{lk['id']:<22} {lk['name']}{tag}\n{'':<22} {lk['description']}")


def cmd_show(args):
    print(json.dumps(_recipe(args), indent=2))


def cmd_sheet(args):
    ids = args.looks.split(",") if args.looks else ["neutral"] + [lk["id"] for lk in recipes.list_looks()]
    looks = [(recipes.load_look(i)["name"], recipes.load_look(i)) for i in ids]
    out_dir = Path(args.out or WORK_DIR / "sheets")
    for src in _targets(args.paths):
        dst = engine.contact_sheet(src, looks, out_dir / f"{src.stem}_sheet.jpg")
        print(dst)
    print("tiles: " + ", ".join(f"{chr(65 + i)}={i_}" for i, i_ in enumerate(ids)))


def cmd_preview(args):
    recipe = _recipe(args)
    out_dir = Path(args.out or WORK_DIR / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    label = recipes.slug(args.name or recipe.get("name", "look"))
    for src in _targets(args.paths):
        dst = out_dir / f"{src.stem}_{label}.jpg"
        dst.write_bytes(engine.jpeg_bytes(engine.render_preview(src, recipe, edge=args.edge, geom=edits.get(src))))
        print(dst)


def cmd_export(args):
    recipe = _recipe(args)
    files = _targets(args.paths)
    for i, src in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {src.name} ...", end="", flush=True)
        dst = engine.export(src, recipe, out_dir=args.out, quality=args.quality,
                            look_name=args.name or recipe.get("name"), geom=edits.get(src))
        print(f" {dst}")


def cmd_save(args):
    """Save a look made of an existing look plus overrides as your own look."""
    over = {"name": args.name, "description": args.description or ""}
    if args.look:
        over["extends"] = args.look
    over = recipes.apply_sets(over, args.set)
    if args.strength is not None:
        over["strength"] = args.strength
    recipes.resolve(over)  # validate before saving
    print(recipes.save_user_look(args.name, over))


def cmd_reference(args):
    look_id, path = reference.make_look(args.image, args.name, base_look=args.look, amount=args.amount)
    print(f"saved look '{look_id}' -> {path}")


def cmd_lut(args):
    recipe = _recipe(args)
    match_src = None
    if recipe.get("match") and args.photo:
        from . import pipeline
        lin, gain = engine.preview_linear(args.photo)
        match_src = pipeline.match_source_stats(lin, recipe, gain)
    out = args.out or f"{recipes.slug(recipe.get('name', 'look'))}.cube"
    print(lut.write_cube(recipe, out, size=args.size, match_src=match_src))


def cmd_library(args):
    from .library import LIBRARY_DIR, db, scan
    con = db.connect()
    if args.action == "add":
        for p in args.paths:
            scan.add_root(con, p)
            print(f"added {Path(p).expanduser()}")
    elif args.action == "scan":
        last = [0]

        def progress(s):
            if s.get("phase") == "reading" and s.get("todo"):
                pct = 100 * s.get("done", 0) // max(s["todo"], 1)
                if pct != last[0]:
                    last[0] = pct
                    print(f"\r  {pct:3d}%  {s.get('current', '')[-60:]:<60}", end="", flush=True)
            elif s.get("phase") in ("listing", "duplicates"):
                print(f"\n  {s['phase']} {s.get('current', '')}", flush=True)
        def progress2(s):
            if s.get("todo"):
                print(f"\r  {s['phase']}: {s['done']}/{s['todo']}   ", end="", flush=True)
        stats = scan.scan(con, progress=progress)
        print()
        if not stats["stopped"]:
            from .library import index
            try:
                stats.update(index.build(con, progress=progress2))
            except KeyboardInterrupt:
                stats["stopped"] = True
            print()
        if stats["stopped"]:
            print(f"stopped safely ({stats.get('error', 'interrupted')}); run scan again to continue")
        print(json.dumps({k: v for k, v in stats.items() if k != "stopped"}, indent=2))
    elif args.action == "search":
        from .library import search, thumbs
        from PIL import Image, ImageDraw
        text = " ".join(args.paths)
        cats = None if args.all else search.DEFAULT_CATEGORIES
        res = search.search(con, text, limit=args.n, categories=cats)
        for i, r in enumerate(res["results"], 1):
            print(f"{i:3d}. {r['score'] or '':<7} {r['category']:<9} {r['folder'][:30]:<30} {r['name']}")
        tiles = [r for r in res["results"] if r["thumb"]]
        if tiles:
            cols, edge = 6, 300
            sheet = Image.new("RGB", (cols * edge, -(-len(tiles) // cols) * edge), (24, 24, 24))
            for i, r in enumerate(tiles):
                im = Image.open(thumbs.path(r["thumb"])); im.thumbnail((edge - 8, edge - 8))
                x, y = (i % cols) * edge, (i // cols) * edge
                sheet.paste(im, (x + (edge - im.width) // 2, y + (edge - im.height) // 2))
                ImageDraw.Draw(sheet).text((x + 6, y + 4), str(i + 1), fill=(255, 220, 0))
            out = WORK_DIR / "search" / f"{recipes.slug(text) or 'all'}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            sheet.save(out, quality=85)
            print(out)
        return
    elif args.action == "folders":
        from .library import search
        for f in search.folder_categories(con):
            counts = " ".join(f"{k}:{v}" for k, v in sorted(f["counts"].items(), key=lambda kv: -kv[1]))
            print(f"{f['folder'][:38]:<38} {f['override'] or f['suggested'] or '?':<9}"
                  f"{'(set)' if f['override'] else '     '} {counts}")
        return
    if args.action in ("status", "scan", "add"):
        print(f"library: {LIBRARY_DIR}")
        for r in scan.list_roots(con):
            print(f"  {'online ' if r['online'] else 'OFFLINE'}  {r['drive']}: {r['rel_path']}")
        print(json.dumps(scan.summary(con), indent=2))


def cmd_serve(args):
    from . import server
    server.run(args.folder, port=args.port, open_browser=not args.no_browser, auto_stop=args.auto_stop)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="filmlook", description="Film looks for your RAW photos.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def look_opts(p):
        p.add_argument("--look", "-l", help="look id (see `filmlook looks`); default neutral")
        p.add_argument("--recipe", help="path to a recipe JSON instead of --look")
        p.add_argument("--set", "-s", action="append", metavar="KEY=VALUE",
                       help="override a setting, e.g. -s color.temp=15 -s grain.amount=0.3")
        p.add_argument("--strength", type=float, help="0..1 blend with the neutral image")

    p = sub.add_parser("looks", help="list available looks"); p.set_defaults(fn=cmd_looks)

    p = sub.add_parser("show", help="print the full resolved recipe")
    look_opts(p); p.set_defaults(fn=cmd_show)

    p = sub.add_parser("sheet", help="contact sheet: each photo in several looks side by side")
    p.add_argument("paths", nargs="+"); p.add_argument("--looks", help="comma-separated look ids")
    p.add_argument("--out"); p.set_defaults(fn=cmd_sheet)

    p = sub.add_parser("preview", help="render a quick preview JPEG")
    p.add_argument("paths", nargs="+"); look_opts(p)
    p.add_argument("--name", help="label used in the output filename")
    p.add_argument("--edge", type=int, default=1600); p.add_argument("--out")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("export", help="full-resolution JPEG export (files or whole folders)")
    p.add_argument("paths", nargs="+"); look_opts(p)
    p.add_argument("--name", help="label used in the output filename")
    p.add_argument("--out", help="output folder (default: <photo folder>/graded)")
    p.add_argument("--quality", type=int, default=95); p.set_defaults(fn=cmd_export)

    p = sub.add_parser("save", help="save a look + overrides as your own look")
    p.add_argument("name"); p.add_argument("--look", "-l"); p.add_argument("--description")
    p.add_argument("--set", "-s", action="append", metavar="KEY=VALUE")
    p.add_argument("--strength", type=float); p.set_defaults(fn=cmd_save)

    p = sub.add_parser("reference", help="create a look that matches a reference photo")
    p.add_argument("image"); p.add_argument("--name", required=True)
    p.add_argument("--look", "-l", help="start from this look, then match")
    p.add_argument("--amount", type=float, default=0.8); p.set_defaults(fn=cmd_reference)

    p = sub.add_parser("lut", help="export a look as a .cube LUT (for ON1, Lightroom, Capture One)")
    look_opts(p); p.add_argument("--out"); p.add_argument("--size", type=int, default=33)
    p.add_argument("--photo", help="photo to base a reference match on (match looks only)")
    p.set_defaults(fn=cmd_lut)

    p = sub.add_parser("library", help="searchable archive: add folders, scan, status")
    p.add_argument("action", choices=["add", "scan", "status", "search", "folders"])
    p.add_argument("paths", nargs="*", help="folders to add (for `add`), or search words (for `search`)")
    p.add_argument("-n", type=int, default=24, help="results to show (search)")
    p.add_argument("--all", action="store_true", help="search every category, not just street + travel")
    p.set_defaults(fn=cmd_library)

    p = sub.add_parser("serve", help="open the browser app")
    p.add_argument("folder", nargs="?"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--auto-stop", action="store_true", help="stop once no app page has been open for 10 minutes")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
