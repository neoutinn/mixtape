import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser

from . import __version__, updater
from .paths import data_dir, is_frozen
from .server import PORT, idle_watchdog, make_server


def setup_logging():
    handlers = [logging.FileHandler(data_dir() / "mixtape.log", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")


def already_running(port) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.load(r).get("app") == "mixtape"
    except Exception:
        return False


def open_browser(url):
    if is_frozen() and sys.platform.startswith("linux"):
        # PyInstaller changes LD_LIBRARY_PATH; give the browser the original one back.
        orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    webbrowser.open(url)


def main():
    setup_logging()
    log = logging.getLogger("mixtape")
    log.info("Mixtape %s starting", __version__)
    try:
        httpd = make_server(PORT)
    except OSError:
        if already_running(PORT):
            open_browser(f"http://127.0.0.1:{PORT}/")
            return
        httpd = make_server(0)  # port busy with something else: pick any free one
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    log.info("Serving on %s", url)
    threading.Thread(target=updater.update_in_background, daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, open_browser, args=(url,)).start()
    else:
        print(url, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
