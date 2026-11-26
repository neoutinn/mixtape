"""Tiny local web server: serves the UI and a JSON API on 127.0.0.1 only."""

import json
import logging
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, ipod, spotify, updater
from .paths import ui_dir
from .sync import Library, Syncer

log = logging.getLogger("mixtape.server")
PORT = 47321
IDLE_EXIT_SECONDS = 180


class App:
    def __init__(self):
        self.syncer = Syncer()
        self.selected = None  # path of the chosen iPod
        self.manual = None    # a folder the user typed in themselves
        self.previews = {}    # id -> playlist (looked up, not yet synced)
        self.last_seen = time.time()
        self.lock = threading.Lock()
        self._ipods = []
        self._ipods_at = 0.0

    def ipods(self):
        if time.time() - self._ipods_at > 1.5:
            found = ipod.find_ipods()
            if self.manual and os.path.isdir(self.manual) and not any(
                    os.path.realpath(i["path"]) == os.path.realpath(self.manual) for i in found):
                found.append(ipod.describe(self.manual))
            self._ipods, self._ipods_at = found, time.time()
        return self._ipods

    def current(self):
        ipods = self.ipods()
        paths = [i["path"] for i in ipods]
        if self.selected not in paths:
            self.selected = paths[0] if paths else None
        return next((i for i in ipods if i["path"] == self.selected), None)

    def state(self):
        cur = self.current()
        return {
            "version": __version__,
            "ipods": self.ipods(),
            "ipod": cur,
            "playlists": Library(cur["path"]).summary() if cur else [],
            "job": self.syncer.snapshot(),
        }


APP = App()


def _store_preview(pl: dict) -> dict:
    pid = str(int(time.time() * 1000))
    APP.previews = {pid: pl}  # only keep the latest one
    return {
        "id": pid, "name": pl["name"], "image": pl["image"], "kind": pl["kind"],
        "count": len(pl["tracks"]), "maybe_truncated": pl["maybe_truncated"],
        "tracks": [{"title": t["title"], "artist": ", ".join(t["artists"])} for t in pl["tracks"]],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Mixtape"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _ok_host(self) -> bool:
        # Blocks other websites from talking to us via DNS tricks.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "forbidden"})
        APP.last_seen = time.time()
        path = self.path.split("?")[0]
        if path == "/api/ping":
            return self._send(200, {"app": "mixtape", "version": __version__})
        if path == "/api/state":
            try:
                return self._send(200, APP.state())
            except Exception as e:
                log.exception("state failed")
                return self._send(500, {"error": str(e)})
        if path == "/":
            path = "/index.html"
        file = (ui_dir() / path.lstrip("/")).resolve()
        if ui_dir().resolve() not in file.parents or not file.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        self._send(200, file.read_bytes(), ctype)

    def do_POST(self):
        # The custom header can't be sent by other websites without our permission.
        if not self._ok_host() or self.headers.get("X-Mixtape") != "1":
            return self._send(403, {"error": "forbidden"})
        APP.last_seen = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            result = self.route(self.path, body)
            self._send(200, result if result is not None else {"ok": True})
        except spotify.SpotifyError as e:
            self._send(400, {"error": str(e)})
        except UserError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            log.exception("POST %s failed", self.path)
            self._send(500, {"error": f"Something went wrong: {e}"})

    def route(self, path, body):
        if path == "/api/lookup":
            return _store_preview(spotify.fetch_link(body.get("url", "")))
        if path == "/api/csv":
            return _store_preview(spotify.parse_csv(body.get("text", ""), body.get("filename", "")))
        if path == "/api/select":
            p = body.get("path", "").strip()
            if p and not os.path.isdir(p):
                raise UserError("That folder doesn't exist.")
            APP.manual = p or None
            APP._ipods_at = 0
            APP.selected = p or None
            return None
        if path == "/api/sync":
            cur = APP.current()
            if not cur:
                raise UserError("Plug in your iPod first.")
            pl = APP.previews.get(body.get("id"))
            if not pl:
                raise UserError("Please paste the playlist link again.")
            name = (body.get("name") or pl["name"]).strip() or pl["name"]
            try:
                APP.syncer.start(cur["path"], pl, name)
            except RuntimeError as e:
                raise UserError(str(e))
            return None
        if path == "/api/update":
            cur = APP.current()
            if not cur:
                raise UserError("Plug in your iPod first.")
            name = body.get("name", "")
            entry = Library(cur["path"]).playlists.get(name)
            if not entry or not entry.get("source"):
                raise UserError("This playlist can't be updated automatically. Add it again instead.")
            pl = spotify.fetch_link(entry["source"])
            try:
                APP.syncer.start(cur["path"], pl, name)
            except RuntimeError as e:
                raise UserError(str(e))
            return None
        if path == "/api/cancel":
            APP.syncer.stop()
            return None
        if path == "/api/dismiss":
            if not APP.syncer.busy():
                APP.syncer.status = {"state": "idle"}
            return None
        if path == "/api/delete":
            cur = APP.current()
            if not cur:
                raise UserError("Plug in your iPod first.")
            if APP.syncer.busy():
                raise UserError("Wait for the current sync to finish first.")
            Library(cur["path"]).delete(body.get("name", ""), bool(body.get("songs", True)))
            return None
        if path == "/api/eject":
            cur = APP.current()
            if not cur:
                return {"ok": True, "message": "No iPod plugged in."}
            if APP.syncer.busy():
                raise UserError("Wait for the current sync to finish first.")
            ok, msg = ipod.eject(cur["path"])
            APP._ipods_at = 0
            if APP.manual == cur["path"]:
                APP.manual = None
            return {"ok": ok, "message": msg}
        if path == "/api/quit":
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return None
        raise UserError("Unknown action")


class UserError(Exception):
    pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # so a second copy of the app notices the first one
    daemon_threads = True


def make_server(port=PORT) -> Server:
    return Server(("127.0.0.1", port), Handler)


def idle_watchdog():
    """Quit by itself once the browser tab has been closed for a while."""
    while True:
        time.sleep(20)
        if not APP.syncer.busy() and time.time() - APP.last_seen > IDLE_EXIT_SECONDS:
            log.info("No browser tab open, quitting.")
            os._exit(0)
