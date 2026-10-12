"""Library operations for the browser app: one background scan job, per-thread DB connections."""
import threading
import traceback

from . import db, index, scan, vision

_local = threading.local()
_job_lock = threading.Lock()
job = {"running": False, "phase": "idle", "done": 0, "todo": 0, "current": "", "stats": None,
       "stopped": False, "error": None}
_stop = threading.Event()


def con():
    """A connection for the calling thread (SQLite connections aren't shared across threads)."""
    c = getattr(_local, "con", None)
    if c is None:
        c = _local.con = db.connect()
    return c


def start_scan(root_ids=None):
    with _job_lock:
        if job["running"]:
            return job
        _stop.clear()
        job.update(running=True, phase="starting", done=0, todo=0, current="", stats=None, stopped=False, error=None)
    threading.Thread(target=_run, args=(root_ids,), daemon=True).start()
    return job


def pause():
    _stop.set()
    return job


def _progress(s):
    job.update(phase=s.get("phase", job["phase"]), done=s.get("done", job["done"]),
               todo=s.get("todo", job["todo"]), current=s.get("current", ""))


def _run(root_ids):
    c = db.connect()   # the scan thread's own connection
    try:
        stats = scan.scan(c, root_ids=root_ids, progress=_progress, should_stop=_stop.is_set)
        if not stats["stopped"]:
            job.update(done=0, todo=0)
            stats.update(index.build(c, progress=_progress, should_stop=_stop.is_set))
        stopped = stats["stopped"] or _stop.is_set()
        job.update(stats=stats, stopped=stopped, phase="paused" if stopped else "done",
                   error=stats.get("error"))
    except Exception as e:
        traceback.print_exc()
        job.update(phase="error", error=str(e))
    finally:
        job["running"] = False
        c.close()


def warm_model():
    """Load the image-text model in the background so the first search is quick."""
    def go():
        try:
            if con().execute("SELECT 1 FROM embeddings LIMIT 1").fetchone():
                from . import search
                vision.load()
                search.query_vector("warm up")   # first GPU run is the slow one
                search._matrix(con())            # load the photo index into memory
                from . import secondlook
                secondlook._scores(con())        # second-look scores ready before the first visit
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()
