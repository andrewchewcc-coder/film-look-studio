import re
from pathlib import Path

from filmlook import server


def test_every_address_the_page_calls_exists():
    """The page must never call an /api/ address the server doesn't have (it shows "Not Found")."""
    pats = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$")
            for r in server.app.routes if getattr(r, "path", "").startswith("/api/")]
    html = (Path(server.__file__).parent / "static" / "index.html").read_text()
    used = {re.sub(r"\$\{[^}]+\}", "X", m.group(1)).rstrip("/")
            for m in re.finditer(r"""[`"'](/api/[^`"'?]*)""", html)}
    missing = sorted(u for u in used if not any(p.match(u) or p.match(u + "/X") for p in pats))
    assert not missing, missing
