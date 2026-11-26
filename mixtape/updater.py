"""Keep yt-dlp fresh.

YouTube changes often and old yt-dlp versions stop working. The packaged app
downloads the latest official yt-dlp (a pure-Python zip) once a day and uses it
in place of the bundled copy, so the app keeps working without a reinstall.
"""

import logging
import os
import sys
import threading
import time
import urllib.request
import zipfile

from .paths import data_dir, is_frozen

log = logging.getLogger("mixtape.updater")
URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
ZIP = data_dir() / "yt-dlp.zip"
_ready = threading.Event()
_ytdlp = None
_import_lock = threading.Lock()


def _version_in_zip(path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            src = z.read("yt_dlp/version.py").decode()
        for line in src.splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


def update_in_background():
    try:
        if not is_frozen():
            return  # when running from source, use whatever pip installed
        if ZIP.exists() and time.time() - ZIP.stat().st_mtime < 24 * 3600:
            return
        tmp = ZIP.with_suffix(".download")
        req = urllib.request.Request(URL, headers={"User-Agent": "Mixtape"})
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            f.write(r.read())
        if _version_in_zip(tmp):
            os.replace(tmp, ZIP)
            log.info("Downloaded yt-dlp %s", _version_in_zip(ZIP))
        else:
            tmp.unlink(missing_ok=True)
    except Exception as e:
        log.warning("yt-dlp update check failed: %s", e)
    finally:
        _ready.set()


def ytdlp():
    """Import yt-dlp, preferring the freshly downloaded copy if it is newer."""
    global _ytdlp
    with _import_lock:
        if _ytdlp:
            return _ytdlp
        _ready.wait(timeout=30)
        if is_frozen() and ZIP.exists():
            try:
                import yt_dlp.version as bundled_version
                bundled = bundled_version.__version__
                for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                    del sys.modules[name]
                if _version_in_zip(ZIP) > bundled:
                    sys.path.insert(0, str(ZIP))
                    import yt_dlp
                    _ytdlp = yt_dlp
                    log.info("Using updated yt-dlp %s", yt_dlp.version.__version__)
                    return _ytdlp
            except Exception as e:
                log.warning("Updated yt-dlp failed to load, using bundled one: %s", e)
                if str(ZIP) in sys.path:
                    sys.path.remove(str(ZIP))
                for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                    del sys.modules[name]
        import yt_dlp
        _ytdlp = yt_dlp
        return _ytdlp


def version() -> str:
    try:
        return ytdlp().version.__version__
    except Exception:
        return "?"
