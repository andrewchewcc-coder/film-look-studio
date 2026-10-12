"""SQLite schema for the library.

A *photo* is one picture, however many files it has: the RAW original, "copy" duplicates,
exported JPGs and the same file on another drive are all *files* linked to one photo.
Later features (scores, picks, crops, tags, sequences) hang off `photos.id`.

Paths are stored relative to their drive, and drives are identified by volume UUID, so
a renamed or unplugged drive keeps its photos.
"""
import sqlite3

from . import LIBRARY_DIR

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS drives (
    id        INTEGER PRIMARY KEY,
    uuid      TEXT NOT NULL UNIQUE,
    name      TEXT NOT NULL,
    mount     TEXT,                 -- last known mount point, e.g. /Volumes/Extreme SSD
    last_seen TEXT
);

-- Folders you chose to include; each is scanned recursively.
CREATE TABLE IF NOT EXISTS roots (
    id       INTEGER PRIMARY KEY,
    drive_id INTEGER NOT NULL REFERENCES drives(id),
    rel_path TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (drive_id, rel_path)
);

-- Every folder that holds photos (one per shoot, plus graded/ subfolders).
CREATE TABLE IF NOT EXISTS folders (
    id        INTEGER PRIMARY KEY,
    drive_id  INTEGER NOT NULL REFERENCES drives(id),
    rel_path  TEXT NOT NULL,
    category  TEXT,                 -- your override: street / travel / portrait / misc
    UNIQUE (drive_id, rel_path)
);

CREATE TABLE IF NOT EXISTS photos (
    id              INTEGER PRIMARY KEY,
    primary_file_id INTEGER NOT NULL UNIQUE,   -- the RAW, or the JPG when there is no RAW
    folder_id       INTEGER NOT NULL REFERENCES folders(id),
    capture_time    TEXT,                      -- local time, ISO 8601
    camera          TEXT,
    lens            TEXT,
    focal_length    REAL,
    aperture        REAL,
    shutter         REAL,                      -- seconds
    iso             INTEGER,
    gps_lat         REAL,
    gps_lon         REAL,
    width           INTEGER,
    height          INTEGER,
    rating          INTEGER,                   -- from ON1 / XMP sidecars, read only
    flag            INTEGER,
    graded          INTEGER NOT NULL DEFAULT 0, -- has an exported JPG
    category        TEXT,                      -- from the image model
    category_score  REAL,
    thumb           TEXT                       -- file name under LIBRARY_DIR/thumbs
);
CREATE INDEX IF NOT EXISTS photos_time ON photos(capture_time);
CREATE INDEX IF NOT EXISTS photos_folder ON photos(folder_id);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    drive_id     INTEGER NOT NULL REFERENCES drives(id),
    folder_id    INTEGER NOT NULL REFERENCES folders(id),
    rel_path     TEXT NOT NULL,
    kind         TEXT NOT NULL,         -- raw / jpg / onphoto
    role         TEXT,                  -- original / copy / export / duplicate
    photo_id     INTEGER REFERENCES photos(id),
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    quick_hash   TEXT,                  -- size + sampled blocks; full hash only on collision
    full_hash    TEXT,
    capture_time TEXT,
    exif         TEXT,                  -- JSON of the fields we read
    missing      INTEGER NOT NULL DEFAULT 0,  -- gone from an online drive
    scanned_at   TEXT,
    UNIQUE (drive_id, rel_path)
);
CREATE INDEX IF NOT EXISTS files_photo ON files(photo_id);
CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS files_qhash ON files(quick_hash);

-- ON1 (.on1) and XMP sidecars. Read only: we never write these files.
CREATE TABLE IF NOT EXISTS sidecars (
    id       INTEGER PRIMARY KEY,
    drive_id INTEGER NOT NULL REFERENCES drives(id),
    folder_id INTEGER NOT NULL REFERENCES folders(id),
    rel_path TEXT NOT NULL,
    kind     TEXT NOT NULL,             -- on1 / xmp
    target   TEXT,                      -- file name it describes
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    rating   INTEGER,
    flag     INTEGER,
    keywords TEXT,                      -- JSON list
    UNIQUE (drive_id, rel_path)
);

-- Second-look finder: technical check from the embedded preview, and burst groups.
CREATE TABLE IF NOT EXISTS quality (
    photo_id    INTEGER PRIMARY KEY REFERENCES photos(id),
    source      TEXT NOT NULL,          -- primary file's quick hash it was measured from
    sharpness   REAL,                   -- 0 blurred .. ~1 crisp, in the sharpest area
    motion      REAL,                   -- direction of remaining detail (motion blur)
    mean_lum    REAL,
    dark_frac   REAL,
    bright_frac REAL,
    verdict     TEXT,                   -- ok / blurry / motion / too_dark / too_bright
    burst_id    INTEGER,                -- photos sharing a burst share this id
    measured_at TEXT
);
CREATE INDEX IF NOT EXISTS quality_burst ON quality(burst_id);

-- Your keep / dismiss decisions (later steps, e.g. culling, learn from these).
CREATE TABLE IF NOT EXISTS picks (
    photo_id   INTEGER PRIMARY KEY REFERENCES photos(id),
    decision   TEXT NOT NULL,           -- keep / dismiss
    source     TEXT,                    -- where it was decided, e.g. second-look
    decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Crop / straighten edits (non-destructive), keyed by the source file so they work for any
-- folder opened in Grade, in the library or not. `geometry` is filmlook.geometry's JSON.
CREATE TABLE IF NOT EXISTS edits (
    id         INTEGER PRIMARY KEY,
    drive_id   INTEGER NOT NULL REFERENCES drives(id),
    rel_path   TEXT NOT NULL,
    geometry   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (drive_id, rel_path)
);

CREATE TABLE IF NOT EXISTS embeddings (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id),
    model    TEXT NOT NULL,
    thumb    TEXT NOT NULL,             -- thumbnail it was computed from
    vector   BLOB NOT NULL              -- float16, L2-normalised
);
"""


def connect(path=None):
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path or LIBRARY_DIR / "library.db", timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")    # safe to stop at any moment
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con
