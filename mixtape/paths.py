"""Where Mixtape keeps its own files (logs, the auto-updated yt-dlp)."""

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        d = base / "Mixtape"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "Mixtape"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        d = base / "mixtape"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ui_dir() -> Path:
    here = Path(__file__).resolve().parent / "ui"
    if here.is_dir():
        return here
    # PyInstaller one-file bundles unpack data next to sys._MEIPASS
    return Path(getattr(sys, "_MEIPASS", ".")) / "mixtape" / "ui"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
