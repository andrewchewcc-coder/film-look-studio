"""Your own keywords on photos ("Sultan Mosque", "Kampong Glam", "rain"), for exact finding.

The picture search guesses; a tag is certain. Tags live only in the library database (never written
to photo files or drives). Matching ignores upper / lower case and extra spaces: "sultan  mosque" is
the same tag as "Sultan Mosque" and keeps the spelling you first used.
"""
import re

MAX_LEN = 60


def clean(tag):
    """'  sultan   mosque ' -> 'sultan mosque'; '' for nothing usable. Commas separate tags, so none inside."""
    t = re.sub(r"\s+", " ", str(tag or "").replace(",", " ")).strip().lstrip("#").strip()
    return t[:MAX_LEN]


def split(text):
    """'Sultan Mosque, rain' -> ['Sultan Mosque', 'rain'] (typed in one box)."""
    out = []
    for part in str(text or "").split(","):
        t = clean(part)
        if t and t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out


def _spelling(con, tag):
    """The spelling already used for this tag, so new photos join it rather than start a variant."""
    r = con.execute("SELECT tag FROM photo_tags WHERE tag = ? LIMIT 1", (tag,)).fetchone()
    return r[0] if r else tag


def add(con, photo_ids, tags):
    """Put these tags on these photos. Returns how many photo-tag pairs were new."""
    tags = [_spelling(con, t) for t in split(",".join(tags) if isinstance(tags, (list, tuple)) else tags)]
    before = con.total_changes
    for pid in {int(i) for i in photo_ids}:
        for t in tags:
            con.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag) VALUES (?, ?)", (pid, t))
    con.commit()
    return con.total_changes - before


def remove(con, photo_ids, tags):
    ids = [int(i) for i in photo_ids]
    n = 0
    for t in split(",".join(tags) if isinstance(tags, (list, tuple)) else tags):
        for pid in ids:
            n += con.execute("DELETE FROM photo_tags WHERE photo_id = ? AND tag = ?", (pid, t)).rowcount
    con.commit()
    return n


def for_photos(con, photo_ids):
    """{photo_id: [tags, A-Z]}."""
    ids = [int(i) for i in photo_ids]
    out = {}
    for k in range(0, len(ids), 900):
        chunk = ids[k:k + 900]
        for r in con.execute(f"SELECT photo_id, tag FROM photo_tags WHERE photo_id IN ({','.join('?' * len(chunk))}) "
                             "ORDER BY tag COLLATE NOCASE", chunk):
            out.setdefault(r[0], []).append(r[1])
    return out


def all_tags(con):
    """[{tag, count}] — every tag you've used, most used first."""
    return [{"tag": r[0], "count": r[1]} for r in con.execute(
        "SELECT MIN(tag), COUNT(*) FROM photo_tags GROUP BY tag ORDER BY COUNT(*) DESC, MIN(tag) COLLATE NOCASE")]


def rename(con, old, new):
    """Rename a tag everywhere (or merge it into another one that already exists)."""
    old, new = clean(old), clean(new)
    if not old or not new:
        raise ValueError("A tag needs a name.")
    if old.lower() != new.lower():
        new = _spelling(con, new)
    if old.lower() == new.lower():           # only the spelling changes
        con.execute("UPDATE photo_tags SET tag = ? WHERE tag = ?", (new, old))
    else:                                    # moves onto the new name (merging if it exists)
        con.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag, added_at) "
                    "SELECT photo_id, ?, added_at FROM photo_tags WHERE tag = ?", (new, old))
        con.execute("DELETE FROM photo_tags WHERE tag = ?", (old,))
    con.commit()
    return new


def delete(con, tag):
    """Take a tag off every photo (the photos themselves are untouched)."""
    n = con.execute("DELETE FROM photo_tags WHERE tag = ?", (clean(tag),)).rowcount
    con.commit()
    return n


def find_in(con, text):
    """The tags named in a search, longest first: ('sultan mosque at night') -> ['Sultan Mosque'], rest 'at night'."""
    low = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    found = []
    for t in sorted((r["tag"] for r in all_tags(con)), key=len, reverse=True):
        needle = " " + t.lower() + " "
        if needle in low:
            found.append(t)
            low = low.replace(needle, " ")
    return found, re.sub(r"\s+", " ", low).strip()
