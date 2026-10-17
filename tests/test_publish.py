import base64
import io
import json
import urllib.error

import pytest

from filmlook import publish


def test_site_address_is_cleaned_up():
    assert publish.normalise_site("andrewchewcc.com") == "https://andrewchewcc.com"
    assert publish.normalise_site(" http://andrewchewcc.com/wp-admin/ ") == "https://andrewchewcc.com"
    assert publish.normalise_site("https://example.com/blog/wp-json/") == "https://example.com/blog"
    assert publish.normalise_site("") == ""


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_signs_in_with_the_application_password(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["url"], seen["auth"] = req.full_url, req.headers["Authorization"]
        if "users/me" in req.full_url:
            return FakeResp(json.dumps({"name": "Andrew", "capabilities": {"upload_files": True, "edit_posts": True,
                                                                            "publish_posts": True}}).encode())
        return FakeResp(json.dumps([{"id": 7, "name": "Street works", "count": 13}]).encode())
    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
    s = {"site": "https://example.com", "user": "andrew"}
    r = publish.test_connection(s, password="abcd EFGH 1234")
    assert r["name"] == "Andrew" and r["can_upload"] and r["categories"][0]["name"] == "Street works"
    assert seen["auth"] == "Basic " + base64.b64encode(b"andrew:abcdEFGH1234").decode()   # spaces removed
    assert seen["url"].startswith("https://example.com/wp-json/wp/v2/categories")


def test_wrong_password_gives_a_plain_message(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(json.dumps({"code": "incorrect_password", "message": "x"}).encode()))
    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(publish.WPError, match="didn't accept"):
        publish.test_connection({"site": "https://example.com", "user": "a"}, password="nope")


def test_post_content_matches_existing_essays():
    import re
    media = [{"id": 11, "url": "https://x/a.jpg", "thumb": "https://x/a-1024.jpg", "title": "Lamp"},
             {"id": 12, "url": "https://x/b.jpg", "thumb": None, "title": "Door"}]
    html = publish.post_content("First para.\n\nSecond <para>.", media, "The end.", captions=False)
    assert html.count("<!-- wp:paragraph -->") == 3 and "Second &lt;para&gt;." in html
    vp = json.loads(re.search(r"<!-- wp:visual-portfolio/block (\{.*?\}) /-->", html).group(1))
    assert vp["layout"] == "slider" and vp["slider_effect"] == "fade" and vp["slider_thumbnails"] is True
    assert vp["images_titles_source"] == "none"
    assert [i["id"] for i in vp["images"]] == [11, 12] and vp["images"][1]["imgThumbnailUrl"] == "https://x/b.jpg"
    c = json.loads(re.search(r"<!-- wp:uagb/container (\{.*?\}) -->", html).group(1))
    assert c["widthDesktop"] == 80 and f"uagb-block-{c['block_id']}" in html
    assert json.loads(re.search(r"<!-- wp:visual-portfolio/block (\{.*?\}) /-->",
                                publish.post_content("", media, captions=True)).group(1))["images_titles_source"] == "title"


def test_upload_creates_a_draft_and_never_edits(monkeypatch, tmp_path):
    from filmlook.library import db, essay_out
    con = db.connect(tmp_path / "lib.db")
    src = {"publish": {"title": "Lisbon", "category": 7, "intro": "Hello.", "cover": 2, "captions": True,
                       "size": 1600, "strip_gps": True}}
    con.execute("INSERT INTO essays (id, name, look, source) VALUES (1, 'Lisbon', 'leica-m9', ?)", (json.dumps(src),))
    con.commit()
    items = [{"pos": 1, "photo_id": 1, "title": "Tram", "stem": "L1", "file": "01_Tram.jpg", "src": "x", "online": True, "drive": "SSD"},
             {"pos": 2, "photo_id": 2, "title": "", "stem": "L2", "file": "02_L2.jpg", "src": "y", "online": True, "drive": "SSD"}]
    monkeypatch.setattr(essay_out, "plan", lambda con, eid: items)
    monkeypatch.setattr(publish, "load_settings", lambda: {"site": "https://example.com", "user": "a"})
    monkeypatch.setattr(publish, "get_password", lambda s=None: "pw")
    monkeypatch.setattr(publish, "prepare_image", lambda con, it, r, edge, strip, dst: (dst.write_bytes(b"jpg"), dst)[1])
    calls = []
    def fake_request(method, path, s=None, password=None, data=None, headers=None, query=None, raw=None):
        calls.append((method, path, data))
        if path == "wp/v2/users/me":
            return {"name": "A", "capabilities": {"upload_files": True, "edit_posts": True}}
        if path == "wp/v2/categories":
            return []
        if path == "wp/v2/media":
            n = len([c for c in calls if c[1] == "wp/v2/media"])
            return {"id": 100 + n, "source_url": f"https://example.com/{n}.jpg", "media_details": {"sizes": {}}}
        if path.startswith("wp/v2/media/"):
            return {}
        if path == "wp/v2/posts":
            return {"id": 555}
        raise AssertionError(path)
    monkeypatch.setattr(publish, "_request", fake_request)
    r = publish.upload_essay(con, 1)
    post = [c for c in calls if c[1] == "wp/v2/posts"][0][2]
    assert post["status"] == "draft" and post["categories"] == [7] and post["featured_media"] == 102   # cover = photo 2
    assert '"id": 101' in post["content"] and '"title": "Tram"' in post["content"] and "<p>Hello.</p>" in post["content"]
    assert all(m in ("GET", "POST") for m, _, _ in calls)                              # never PUT/PATCH/DELETE
    assert not any(p.startswith("wp/v2/posts/") or p.startswith("wp/v2/pages") for _, p, _ in calls)  # no existing post touched
    assert r["post_id"] == 555 and "post=555" in r["edit"]
    saved = json.loads(con.execute("SELECT source FROM essays WHERE id=1").fetchone()[0])["publish"]
    assert saved["post_id"] == 555
