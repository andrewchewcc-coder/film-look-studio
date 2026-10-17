"""Publishing photo essays to your WordPress site (e.g. andrewchewcc.com).

The app talks to WordPress itself through its REST API, signing in with an Application
Password (WordPress → Users → Profile → Application Passwords). The site address and
username are kept in the library folder; the application password only in the macOS Keychain.

Nothing on the website is ever edited or deleted: the app only adds media and new *draft*
posts, which you review and publish in WordPress yourself.
"""
import base64
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .library import LIBRARY_DIR

SETTINGS = LIBRARY_DIR / "publish.json"
KEYCHAIN_SERVICE = "Film Look Studio – WordPress"
TIMEOUT = 30


# --- settings and the Keychain ----------------------------------------------------------------

def normalise_site(text):
    """'andrewchewcc.com' / 'http://andrewchewcc.com/wp-admin' -> 'https://andrewchewcc.com'."""
    t = (text or "").strip()
    if not t:
        return ""
    if "://" not in t:
        t = "https://" + t
    u = urllib.parse.urlparse(t)
    path = u.path.split("/wp-admin")[0].split("/wp-json")[0].rstrip("/")
    return f"https://{u.netloc}{path}" if u.netloc else ""


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def _account(s):
    return f"{s.get('user', '')}@{urllib.parse.urlparse(s.get('site', '')).netloc}"


def get_password(s=None):
    s = s or load_settings()
    if not s.get("site") or not s.get("user"):
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(s), "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def save_settings(site, user, password=None):
    """Save the site and user; the password (if given) goes into the Keychain only."""
    s = {"site": normalise_site(site), "user": (user or "").strip()}
    if not s["site"] or not s["user"]:
        raise ValueError("Enter your website address and WordPress username.")
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2))
    if password:
        pw = password.strip()
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", _account(s),
                            "-l", KEYCHAIN_SERVICE, "-w", pw], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Couldn't save the password in the Keychain: " + (r.stderr.strip() or "unknown error"))
    return s


def forget():
    """Disconnect: remove the saved site and the Keychain password."""
    s = load_settings()
    if s.get("site"):
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(s)],
                       capture_output=True)
    try:
        SETTINGS.unlink()
    except OSError:
        pass


# --- talking to WordPress ---------------------------------------------------------------------

class WPError(Exception):
    pass


def _request(method, path, s=None, password=None, data=None, headers=None, query=None, raw=None):
    s = s or load_settings()
    password = password or get_password(s)
    if not s.get("site"):
        raise WPError("Not connected: enter your website in Publish settings.")
    if not password:
        raise WPError("No application password saved yet: add it in Publish settings.")
    url = f"{s['site']}/wp-json/{path.lstrip('/')}"
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    token = base64.b64encode(f"{s['user']}:{password.replace(' ', '')}".encode()).decode()
    h = {"Authorization": f"Basic {token}", "Accept": "application/json", "User-Agent": "FilmLookStudio/1.0"}
    h.update(headers or {})
    body = raw
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read().decode() or "null"
            return json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
            msg = detail.get("message") or str(detail)
            code = detail.get("code", "")
        except Exception:
            msg, code = e.reason, ""
        if e.code in (401, 403) and code in ("incorrect_password", "invalid_username", "rest_not_logged_in", ""):
            raise WPError("WordPress didn't accept the username or application password. Check both "
                          "(the username is your WordPress login name, not your email, unless you log in with email).")
        raise WPError(f"WordPress said: {msg} ({e.code})")
    except urllib.error.URLError as e:
        raise WPError(f"Couldn't reach {s['site']}: {e.reason}. Check the address and your internet connection.")


def test_connection(s=None, password=None):
    """Who you are on the site, what you may do, and the post categories (for choosing a gallery)."""
    me = _request("GET", "wp/v2/users/me", s, password, query={"context": "edit"})
    caps = me.get("capabilities") or {}
    cats = _request("GET", "wp/v2/categories", s, password, query={"per_page": 100, "_fields": "id,name,count"})
    return {"ok": True, "name": me.get("name") or me.get("slug"), "can_upload": bool(caps.get("upload_files")),
            "can_post": bool(caps.get("edit_posts")), "can_publish": bool(caps.get("publish_posts")),
            "categories": [{"id": c["id"], "name": c["name"], "count": c.get("count", 0)} for c in cats]}


# --- building and uploading a draft post --------------------------------------------------------

# Your essays' gallery: a Visual Portfolio slider (fade, autoplay, bullets, 10 thumbnails) inside a
# Spectra container at 80% width — copied from "Thaipusam 2026" so new essays look the same.
VP_SETTINGS = {
    "align": "full", "content_source": "images", "images_titles_source": "none",
    "images_descriptions_source": "none", "items_count": -1, "layout": "slider",
    "masonry_horizontal_order": True, "justified_row_height": 449, "slider_effect": "fade",
    "slider_autoplay_hover_pause": True, "slider_items_height_type": "auto",
    "slider_items_height_dynamic": 100, "slider_slides_per_view_type": "auto", "slider_loop": True,
    "slider_arrows": False, "slider_bullets": True, "slider_mousewheel": True,
    "slider_thumbnails": True, "slider_thumbnails_gap": 10, "slider_thumbnails_height_type": "auto",
    "slider_thumbnails_height_dynamic": 80, "slider_thumbnails_per_view_custom": 10, "items_gap": 6,
    "items_style": "default", "items_style_default__show_overlay": "always",
    "items_style_emerge__show_overlay": "always", "items_style_emerge__show_caption": "always",
    "items_click_action": "false", "items_click_action_popup_title_source": "item_title",
    "items_click_action_popup_description_source": "item_description",
}
CONTAINER = {"contentWidth": "default", "widthDesktop": 80, "minHeightDesktop": 347, "backgroundType": "color",
             "backgroundColor": "", "variationSelected": True, "isBlockRootParent": True,
             "widthSetByUser": True, "containerBorderStyle": "none"}


def _block_id(n=8):
    import secrets
    return secrets.token_hex(n // 2)


def _paragraphs(text):
    import html
    out = []
    for p in (text or "").split("\n\n"):
        p = p.strip()
        if p:
            body = html.escape(p).replace("\n", "<br>")
            out.append(f"<!-- wp:paragraph -->\n<p>{body}</p>\n<!-- /wp:paragraph -->")
    return "\n\n".join(out)


def post_content(intro, media, closing="", captions=False):
    """The post's blocks: write-up, the gallery (like your existing essays), closing text.
    media: [{"id", "url", "thumb", "title"}] in order."""
    import html
    vp = {"block_id": _block_id(6), **VP_SETTINGS}
    if captions:
        vp["images_titles_source"] = "title"           # show each photo's title (your caption)
    vp["images"] = [{"id": m["id"], "imgUrl": m["url"], "imgThumbnailUrl": m["thumb"] or m["url"], "title": m["title"]}
                    for m in media]
    cid = _block_id(8)
    container = {"block_id": cid, **CONTAINER}
    gallery = (f"<!-- wp:uagb/container {json.dumps(container)} -->\n"
               f'<div class="wp-block-uagb-container uagb-block-{cid} default uagb-is-root-container">'
               f"<!-- wp:visual-portfolio/block {json.dumps(vp)} /--></div>\n<!-- /wp:uagb/container -->")
    return "\n\n".join(x for x in (_paragraphs(intro), gallery, _paragraphs(closing)) if x)


def prepare_image(con, item, recipe, edge, strip_gps, dst):
    """A web-size JPEG of one essay photo: the largest graded JPG, else the RAW with its grade + crop."""
    from PIL import Image
    from .library import essay_book
    from . import engine
    im = essay_book.photo_image(con, item, recipe, edge)
    im.save(dst, "JPEG", quality=90, subsampling=0, icc_profile=engine._SRGB_ICC)
    engine.copy_metadata(item["src"], dst)
    if strip_gps:
        import shutil
        exiftool = shutil.which("exiftool")
        if exiftool:
            subprocess.run([exiftool, "-q", "-q", "-overwrite_original", "-gps:all=", "-xmp:geotag=", str(dst)],
                           check=False, capture_output=True)
    return dst


def upload_media(path, title, alt, s=None, password=None):
    """Upload one JPEG to the Media Library; returns {"id", "url", "thumb"}."""
    import urllib.parse as up
    data = Path(path).read_bytes()
    name = up.quote(Path(path).name)
    m = _request("POST", "wp/v2/media", s, password, raw=data,
                 headers={"Content-Type": "image/jpeg", "Content-Disposition": f'attachment; filename="{name}"'})
    _request("POST", f"wp/v2/media/{m['id']}", s, password, data={"title": title, "alt_text": alt})
    sizes = (m.get("media_details") or {}).get("sizes") or {}
    thumb = (sizes.get("large") or sizes.get("medium_large") or {}).get("source_url") or m["source_url"]
    return {"id": m["id"], "url": m["source_url"], "thumb": thumb}


def upload_essay(con, essay_id, job=None, cancelled=lambda: False, poll=3.0):
    """Prepare the photos, upload them, and create a DRAFT post. Never edits or deletes anything.
    Returns {"post_id", "edit", "preview"}."""
    import tempfile
    import time
    from datetime import datetime
    from . import recipes
    from .library import essay_out
    job = job if job is not None else {}
    s = load_settings()
    e = con.execute("SELECT name, look, source FROM essays WHERE id=?", (essay_id,)).fetchone()
    src = json.loads(e["source"] or "{}")
    pub = src.get("publish") or {}
    items = essay_out.plan(con, essay_id)
    if not items:
        raise WPError("This essay has no photos.")
    if not pub.get("category"):
        raise WPError("Choose a gallery (category) first.")
    job.update(total=len(items) * 2 + 1, done=0, current="", waiting=[], uploaded=[])
    while not all(i["online"] for i in items):
        job["waiting"] = sorted({i["drive"] or "a drive" for i in items if not i["online"]})
        if cancelled():
            return None
        time.sleep(poll)
        items = essay_out.plan(con, essay_id)
    job["waiting"] = []
    test_connection(s)                               # fail early, before preparing anything
    recipe = recipes.load_look(e["look"] or "neutral")
    size, strip = int(pub.get("size") or 2048), bool(pub.get("strip_gps", True))
    media, cover = [], pub.get("cover") or items[0]["photo_id"]
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for it in items:
            if cancelled():
                return None
            job["current"] = f"preparing {it['file']}"
            dst = Path(tmp) / it["file"]
            prepare_image(con, it, recipe, size, strip, dst)
            files.append((it, dst))
            job["done"] += 1
        for it, dst in files:
            if cancelled():
                job["note"] = f"Stopped after uploading {len(media)} photo(s) to your Media Library (no post was created)."
                return None
            job["current"] = f"uploading {it['file']}"
            title = (it["title"] or it["stem"]).strip()
            m = upload_media(dst, title, it["title"] or f"{pub.get('title') or e['name']}, photo {it['pos']}", s)
            media.append({**m, "title": title if pub.get("captions") else it["stem"], "photo_id": it["photo_id"]})
            job["uploaded"].append(m["id"])
            job["done"] += 1
    job["current"] = "creating the draft post"
    featured = next((m["id"] for m in media if m["photo_id"] == cover), media[0]["id"])
    post = _request("POST", "wp/v2/posts", s, data={
        "title": pub.get("title") or e["name"], "status": "draft", "categories": [int(pub["category"])],
        "featured_media": featured, "format": "gallery",
        "content": post_content(pub.get("intro", ""), media, pub.get("closing", ""), bool(pub.get("captions")))})
    job["done"] += 1
    edit = f"{s['site']}/wp-admin/post.php?post={post['id']}&action=edit"
    preview = f"{s['site']}/?p={post['id']}&preview=true"
    pub.update(post_id=post["id"], link=edit, uploaded=datetime.now().isoformat(timespec="minutes"))
    src["publish"] = pub
    con.execute("UPDATE essays SET source=? WHERE id=?", (json.dumps(src), essay_id))
    con.commit()
    job["current"] = ""
    return {"post_id": post["id"], "edit": edit, "preview": preview}
