"""The photo library: a searchable archive of your photo folders across drives.

Everything the library writes (database, thumbnails) lives on the Mac's internal disk
under LIBRARY_DIR. Photo folders on external drives are only ever read.
"""
import os
from pathlib import Path

LIBRARY_DIR = Path(os.environ.get(
    "FILMLOOK_LIBRARY", Path.home() / "Library" / "Application Support" / "Film Look Studio"))
