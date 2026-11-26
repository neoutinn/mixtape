"""Turn one Spotify track into a tagged, Rockbox-friendly MP3.

Steps: look up album info + cover on iTunes -> find the best matching audio on
YouTube -> download -> convert to MP3 -> embed tags and a 500x500 baseline JPEG
cover (Rockbox can't show progressive JPEGs).
"""

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError

from . import updater

log = logging.getLogger("mixtape.songs")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
# Words that usually mean "not the studio version" unless the Spotify title has them too.
UNWANTED = ["live", "remix", "cover", "karaoke", "instrumental", "sped up", "slowed",
            "reverb", "8d", "nightcore", "acoustic", "reaction", "lyrics video", "1 hour",
            "extended", "mashup", "piano version", "tutorial"]


class Cancelled(Exception):
    pass


class SongNotFound(Exception):
    pass


# ---------------------------------------------------------------- helpers

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"&", " and ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song'"""
    t = re.sub(r"\s*[\(\[](feat|ft|with)\.?\s[^\)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|version|edit|mono|stereo|deluxe|from ).*$", "", t, flags=re.I)
    return t.strip() or title


def safe_name(name: str, maxlen: int = 80) -> str:
    """Make a name safe for the iPod's FAT32 filesystem."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if name.upper() in WIN_RESERVED:
        name = "_" + name
    return name[:maxlen].rstrip(". ") or "Unknown"


def song_path(track: dict) -> str:
    """Relative path on the iPod, e.g. Music/Daft Punk/Digital Love.mp3"""
    artist = safe_name(track["artists"][0], 60)
    return f"Music/{artist}/{safe_name(track['title'], 90)}.mp3"


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg is missing")
        return exe


def run_ffmpeg(*args):
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-300:])


def http_get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mixtape/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- iTunes metadata

_itunes_lock = threading.Lock()
_itunes_last = 0.0


def _itunes(term: str) -> list[dict]:
    """iTunes allows roughly 20 searches a minute, so space them out."""
    global _itunes_last
    q = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 10})
    for attempt in range(3):
        with _itunes_lock:
            wait = _itunes_last + 3.2 - time.time()
            if wait > 0:
                time.sleep(wait)
            _itunes_last = time.time()
        try:
            return json.loads(http_get(f"https://itunes.apple.com/search?{q}")).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                time.sleep(20)
                continue
            raise
    return []


def find_metadata(track: dict) -> dict:
    artist, title = track["artists"][0], clean_title(track["title"])
    try:
        results = _itunes(f"{artist} {title}")
    except Exception as e:
        log.info("iTunes lookup failed for %s: %s", title, e)
        return {}
    best, best_score = None, 0.0
    for r in results:
        score = similar(artist, r.get("artistName", "")) * 2 + similar(title, r.get("trackName", "")) * 2
        if track.get("album"):
            score += similar(track["album"], r.get("collectionName", ""))
        if track.get("duration") and r.get("trackTimeMillis"):
            score -= min(abs(track["duration"] - r["trackTimeMillis"] / 1000) / 30, 1)
        if score > best_score:
            best, best_score = r, score
    if not best or best_score < 3:
        return {}
    art = best.get("artworkUrl100", "")
    return {
        "album": best.get("collectionName", ""),
        "album_artist": best.get("collectionArtistName") or best.get("artistName", ""),
        "year": (best.get("releaseDate") or "")[:4],
        "genre": best.get("primaryGenreName", ""),
        "number": best.get("trackNumber"),
        "cover_url": art.replace("100x100bb", "600x600bb") if art else "",
    }


_cover_cache: dict[str, bytes] = {}
_cover_lock = threading.Lock()


def get_cover(url: str, workdir: str) -> bytes | None:
    """Download a cover and re-encode it as a 500x500 baseline JPEG."""
    if not url:
        return None
    with _cover_lock:
        if url in _cover_cache:
            return _cover_cache[url]
    try:
        src = os.path.join(workdir, "cover_src")
        out = os.path.join(workdir, "cover.jpg")
        with open(src, "wb") as f:
            f.write(http_get(url))
        run_ffmpeg("-i", src, "-vf", "scale=500:500", "-pix_fmt", "yuvj420p", "-q:v", "3",
                   "-frames:v", "1", out)
        with open(out, "rb") as f:
            data = f.read()
    except Exception as e:
        log.info("Cover failed (%s): %s", url, e)
        data = None
    with _cover_lock:
        if len(_cover_cache) > 200:
            _cover_cache.clear()
        _cover_cache[url] = data
    return data


# ---------------------------------------------------------------- YouTube

class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): log.debug(msg)
    def error(self, msg): log.info(msg)


def _score_video(entry: dict, track: dict) -> float:
    title = norm(entry.get("title", ""))
    channel = norm(entry.get("channel") or entry.get("uploader") or "")
    want = norm(track["title"])
    artist = norm(track["artists"][0])
    score = 0.0
    dur, want_dur = entry.get("duration"), track.get("duration")
    if dur and want_dur:
        diff = abs(dur - want_dur)
        if diff > max(20, want_dur * 0.15):
            score -= 100
        else:
            score -= diff
    for word in UNWANTED:
        if re.search(rf"\b{word}\b", title) and not re.search(rf"\b{word}\b", want):
            score -= 40
    if norm(clean_title(track["title"])) in title:
        score += 15
    if artist and (artist in title or artist in channel):
        score += 10
    if channel.endswith(" topic"):  # auto-generated "Artist - Topic" channels = studio audio
        score += 20
    if "official audio" in title:
        score += 8
    return score


def find_video(track: dict) -> str:
    yt_dlp = updater.ytdlp()
    query = f"{track['artists'][0]} - {clean_title(track['title'])}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "logger": _QuietLogger(), "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch8:{query}", download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e and e.get("id")]
    if not entries:
        raise SongNotFound("no results")
    best = max(entries, key=lambda e: _score_video(e, track))
    if _score_video(best, track) <= -100:
        raise SongNotFound("no good match")
    return f"https://www.youtube.com/watch?v={best['id']}"


def download_audio(url: str, workdir: str, cancel: threading.Event) -> str:
    yt_dlp = updater.ytdlp()

    def hook(d):
        if cancel.is_set():
            raise Cancelled()

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "format": "bestaudio/best", "outtmpl": os.path.join(workdir, "audio.%(ext)s"),
        "logger": _QuietLogger(), "progress_hooks": [hook], "socket_timeout": 20, "retries": 3,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        reqs = info.get("requested_downloads") or []
        path = reqs[0].get("filepath") if reqs else ydl.prepare_filename(info)
    if not path or not os.path.exists(path):
        raise RuntimeError("download failed")
    return path


# ---------------------------------------------------------------- tagging

def write_tags(mp3: str, track: dict, meta: dict, cover: bytes | None):
    try:
        tags = ID3(mp3)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=track["title"]))
    tags.add(TPE1(encoding=3, text=", ".join(track["artists"])))
    tags.add(TPE2(encoding=3, text=meta.get("album_artist") or track["artists"][0]))
    album = track.get("album") or meta.get("album")
    if album:
        tags.add(TALB(encoding=3, text=album))
    number = track.get("number") or meta.get("number")
    if number:
        tags.add(TRCK(encoding=3, text=str(number)))
    year = track.get("year") or meta.get("year")
    if year:
        tags.add(TDRC(encoding=3, text=str(year)))
    if meta.get("genre"):
        tags.add(TCON(encoding=3, text=meta["genre"]))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(mp3, v2_version=3)  # ID3v2.3 is the most widely supported


# ---------------------------------------------------------------- the whole thing

def make_song(track: dict, ipod_root: str, cover_url: str, cancel: threading.Event) -> str:
    """Puts the song on the iPod (if not there already). Returns its relative path."""
    rel = song_path(track)
    dest = os.path.join(ipod_root, *rel.split("/"))
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return rel
    with tempfile.TemporaryDirectory(prefix="mixtape-") as work:
        meta = find_metadata(track)
        if cancel.is_set():
            raise Cancelled()
        url = find_video(track)
        src = download_audio(url, work, cancel)
        mp3 = os.path.join(work, "song.mp3")
        run_ffmpeg("-i", src, "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame", "-q:a", "0", mp3)
        cover = get_cover(cover_url or meta.get("cover_url", ""), work)
        write_tags(mp3, track, meta, cover)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        shutil.copyfile(mp3, part)
        os.replace(part, dest)
    return rel
