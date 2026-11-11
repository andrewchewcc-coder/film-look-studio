"""Your marks on a photo — pick / reject and 1-5 stars — in one place.

Cull, Second look and the Library all read and write these through `set_mark`, so a change in
one shows everywhere (Cull is a focused way of making the same decisions). Every change is
logged in `cull_log` (the taste learning reads it). Rejects are only a flag: nothing is deleted.
"""


def get(con, photo_id):
    r = con.execute("SELECT flag, rating FROM marks WHERE photo_id=?", (photo_id,)).fetchone()
    return {"flag": r["flag"] if r else None, "rating": r["rating"] if r else None}


def set_mark(con, photo_id, flag=..., rating=..., session_id=None, group_idx=None, suggested=None):
    """Change a photo's flag ("pick" / "reject" / None) and/or rating (0/None clears).
    Pass only what changes (the other stays as is)."""
    con.execute("INSERT OR IGNORE INTO marks (photo_id) VALUES (?)", (photo_id,))
    if flag is not ...:
        if flag not in ("pick", "reject", None):
            raise ValueError(f"unknown flag {flag}")
        con.execute("UPDATE marks SET flag=?, updated_at=datetime('now') WHERE photo_id=?", (flag, photo_id))
        if flag != "pick":            # Second look's "Kept" is the same as picked
            con.execute("DELETE FROM picks WHERE photo_id=? AND decision='keep'", (photo_id,))
        if flag is not None:          # a decision supersedes "don't suggest again"
            con.execute("DELETE FROM picks WHERE photo_id=? AND decision='dismiss'", (photo_id,))
        con.execute("""INSERT INTO cull_log (session_id, group_idx, photo_id, action, value, suggested)
                       VALUES (?,?,?,?,NULL,?)""", (session_id, group_idx, photo_id, flag or "unmark", suggested))
    if rating is not ...:
        value = int(rating) if rating else None
        if value is not None and not 1 <= value <= 5:
            raise ValueError("stars must be 1-5")
        con.execute("UPDATE marks SET rating=?, updated_at=datetime('now') WHERE photo_id=?", (value, photo_id))
        con.execute("""INSERT INTO cull_log (session_id, group_idx, photo_id, action, value, suggested)
                       VALUES (?,?,?,'rate',?,?)""", (session_id, group_idx, photo_id, value, suggested))
    con.commit()
    return get(con, photo_id)
