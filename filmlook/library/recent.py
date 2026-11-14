"""Recent work: photos you graded, cropped or exported lately (the Library's Recently graded view and shelf).

Activity per photo = the newest of its grade / crop record (`grades.updated_at`, `edits.updated_at`, stored in UTC)
and its exported JPGs' modify times. Exports made outside the app count too once a scan has seen them.
"""
import time

from . import search

_SQL = """
SELECT photo_id, MAX(t) AS at, GROUP_CONCAT(DISTINCT k) AS kinds FROM (
  SELECT f.photo_id, CAST(strftime('%s', g.updated_at) AS REAL) AS t, 'graded' AS k
    FROM grades g JOIN files f ON f.drive_id = g.drive_id AND f.rel_path = g.rel_path
   WHERE CAST(strftime('%s', g.updated_at) AS REAL) >= :since
  UNION ALL
  SELECT f.photo_id, CAST(strftime('%s', e.updated_at) AS REAL), 'cropped'
    FROM edits e JOIN files f ON f.drive_id = e.drive_id AND f.rel_path = e.rel_path
   WHERE CAST(strftime('%s', e.updated_at) AS REAL) >= :since
  UNION ALL
  SELECT photo_id, mtime, 'exported' FROM files
   WHERE role = 'export' AND kind = 'jpg' AND missing = 0 AND mtime >= :since
) GROUP BY photo_id ORDER BY at DESC"""


def recent(con, days=7, offset=0, limit=200, now=None):
    """{"total", "results"}: Library photos (as `search.photo`) plus `worked_at` (unix seconds) and
    `worked` (what was done: exported / graded / cropped), newest work first."""
    since = (now or time.time()) - days * 86400
    rows = con.execute(_SQL, {"since": since}).fetchall()
    out = []
    for pid, at, kinds in rows[offset:offset + limit]:
        ph = search.photo(con, pid)
        if ph:
            k = set((kinds or "").split(","))
            ph["worked_at"] = at
            ph["worked"] = [w for w in ("exported", "graded", "cropped") if w in k]
            out.append(ph)
    return {"total": len(rows), "results": out}
