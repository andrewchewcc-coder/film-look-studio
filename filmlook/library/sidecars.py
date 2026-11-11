"""Read ratings, flags and keywords from ON1 (.on1) and XMP sidecars. Never writes them."""
import json
import re
from pathlib import Path

SIDECAR_EXTENSIONS = {".on1", ".xmp"}


def read(path):
    """Return {target, rating, flag, keywords} or None if unreadable."""
    path = Path(path)
    try:
        with open(path, "rb") as f:          # read-only by construction
            data = f.read()
    except OSError:
        return None
    if path.suffix.lower() == ".on1":
        return _read_on1(path, data)
    return _read_xmp(path, data)


def _read_on1(path, data):
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    for photo in (doc.get("photos") or {}).values():
        meta = photo.get("metadata") or {}
        kw = meta.get("Keywords") or []
        return {"target": photo.get("name") or path.stem,
                "rating": _int(meta.get("Rating")),
                "flag": _int(meta.get("UserFlag")),
                "keywords": kw if isinstance(kw, list) else [kw]}
    return None


def _read_xmp(path, data):
    text = data.decode("utf-8", "replace")
    m = re.search(r'xmp:Rating\s*=\s*"(-?\d+)"|<xmp:Rating>(-?\d+)<', text)
    subj = re.search(r"<dc:subject>(.*?)</dc:subject>", text, re.S)
    keywords = re.findall(r"<rdf:li[^>]*>([^<]+)</rdf:li>", subj.group(1)) if subj else []
    ext = re.search(r'SidecarForExtension\s*=\s*"([^"]+)"', text)
    target = f"{path.stem}.{ext.group(1)}" if ext else path.stem
    return {"target": target,
            "rating": _int(next((g for g in m.groups() if g), None)) if m else None,
            "flag": None,
            "keywords": [k.strip() for k in keywords]}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
