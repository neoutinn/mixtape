"""Sync a playlist onto the iPod and remember what's there.

Layout on the iPod:
    /Music/<Artist>/<Title>.mp3       songs (shared between playlists)
    /Playlists/<Name>.m3u8            Rockbox playlists (show up under Playlists)
    /.mixtape/library.json            what Mixtape has put on the iPod
"""

import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import songs

log = logging.getLogger("mixtape.sync")
WORKERS = 3
MIN_FREE = 60 * 1024 * 1024


class Library:
    def __init__(self, root: str):
        self.root = root
        self.file = os.path.join(root, ".mixtape", "library.json")
        self.lock = threading.Lock()
        self.data = {"version": 1, "playlists": {}}
        try:
            with open(self.file, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    @property
    def playlists(self) -> dict:
        return self.data["playlists"]

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file)

    def summary(self) -> list[dict]:
        out = []
        for name, p in self.playlists.items():
            out.append({
                "name": name, "count": len(p.get("songs", [])), "missing": p.get("missing", 0),
                "source": p.get("source", ""), "image": p.get("image", ""),
                "updated": p.get("updated", 0),
            })
        return sorted(out, key=lambda p: -p["updated"])

    def delete(self, name: str, delete_songs: bool):
        p = self.playlists.pop(name, None)
        if not p:
            return
        m3u = os.path.join(self.root, "Playlists", p.get("file", songs.safe_name(name) + ".m3u8"))
        if os.path.exists(m3u):
            os.remove(m3u)
        if delete_songs:
            still_used = {s for other in self.playlists.values() for s in other.get("songs", [])}
            for rel in p.get("songs", []):
                if rel in still_used:
                    continue
                path = os.path.join(self.root, *rel.split("/"))
                try:
                    os.remove(path)
                    folder = os.path.dirname(path)
                    if not os.listdir(folder):
                        os.rmdir(folder)
                except OSError:
                    pass
        self.save()


def write_m3u8(root: str, filename: str, entries: list[tuple[dict, str]]):
    folder = os.path.join(root, "Playlists")
    os.makedirs(folder, exist_ok=True)
    lines = ["#EXTM3U"]
    for track, rel in entries:
        lines.append(f"#EXTINF:{track.get('duration') or -1},{', '.join(track['artists'])} - {track['title']}")
        lines.append("/" + rel)  # Rockbox paths start at the root of the iPod
    path = os.path.join(folder, filename)
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)


class Syncer:
    """Runs one sync at a time in the background and reports progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.status = {"state": "idle"}

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.status))

    def start(self, root: str, playlist: dict, name: str):
        if self.busy():
            raise RuntimeError("Already syncing — wait for it to finish first.")
        self.cancel.clear()
        tracks = playlist["tracks"]
        self.status = {
            "state": "running", "name": name, "image": playlist.get("image", ""),
            "total": len(tracks), "done": 0, "added": 0, "already": 0, "failed": 0,
            "songs": [{"title": t["title"], "artist": ", ".join(t["artists"]), "state": "waiting"}
                      for t in tracks],
            "error": "", "started": time.time(),
        }
        self.thread = threading.Thread(target=self._run, args=(root, playlist, name), daemon=True)
        self.thread.start()

    def _set(self, i=None, **kw):
        with self.lock:
            if i is None:
                self.status.update(kw)
            else:
                self.status["songs"][i].update(kw)

    def _run(self, root: str, playlist: dict, name: str):
        tracks = playlist["tracks"]
        filename = songs.safe_name(name, 100) + ".m3u8"
        results: list[str | None] = [None] * len(tracks)
        try:
            lib = Library(root)
            existing = {}
            for i, t in enumerate(tracks):
                path = os.path.join(root, *songs.song_path(t).split("/"))
                existing[i] = os.path.exists(path)

            def work(i):
                if self.cancel.is_set():
                    raise songs.Cancelled()
                if not existing[i] and shutil.disk_usage(root).free < MIN_FREE:
                    raise OSError(28, "iPod is full")
                self._set(i, state="working")
                return songs.make_song(tracks[i], root, playlist.get("album_cover", ""), self.cancel)

            with ThreadPoolExecutor(WORKERS) as pool:
                futures = {pool.submit(work, i): i for i in range(len(tracks))}
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                        kind = "already" if existing[i] else "added"
                        self._set(i, state="done" if kind == "added" else "already")
                        with self.lock:
                            self.status[kind] += 1
                    except songs.Cancelled:
                        self._set(i, state="waiting")
                        continue
                    except songs.SongNotFound:
                        self._set(i, state="failed", why="Couldn't find this song on YouTube")
                        with self.lock:
                            self.status["failed"] += 1
                    except OSError as e:
                        if e.errno == 28 or "full" in str(e):
                            self.cancel.set()
                            self._set(error="The iPod is full! Remove a playlist to make room.")
                        elif not os.path.isdir(root):
                            self.cancel.set()
                            self._set(error="The iPod was unplugged. Plug it back in and press Update.")
                        self._set(i, state="failed", why=str(e))
                        with self.lock:
                            self.status["failed"] += 1
                    except Exception as e:
                        log.exception("Song failed: %s", tracks[i]["title"])
                        self._set(i, state="failed", why=str(e)[:200])
                        with self.lock:
                            self.status["failed"] += 1
                    with self.lock:
                        self.status["done"] += 1
                    # Keep the playlist file current so a partial sync is still usable.
                    write_m3u8(root, filename, [(tracks[j], r) for j, r in enumerate(results) if r])

            ok = [r for r in results if r]
            write_m3u8(root, filename, [(tracks[j], r) for j, r in enumerate(results) if r])
            old = lib.playlists.get(name, {})
            lib.playlists[name] = {
                "file": filename,
                "source": playlist.get("source", "") or old.get("source", ""),
                "image": playlist.get("image", "") or old.get("image", ""),
                "songs": ok,
                "missing": len(tracks) - len(ok),
                "updated": time.time(),
            }
            lib.save()
            self._set(state="cancelled" if self.cancel.is_set() and not self.status["error"] else "finished")
        except Exception as e:
            log.exception("Sync failed")
            msg = "The iPod was unplugged." if not os.path.isdir(root) else f"Something went wrong: {e}"
            self._set(state="finished", error=msg)

    def stop(self):
        self.cancel.set()
