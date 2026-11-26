"""Read track lists from Spotify links (no login needed) or Exportify CSV files."""

import csv
import io
import json
import re
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
LINK_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(?:embed/)?|spotify:)"
    r"(playlist|album|track)[/:]([A-Za-z0-9]{22})"
)
# Spotify only shares this many songs through the public embed page.
EMBED_LIMIT = 100


class SpotifyError(Exception):
    """An error with a message that is safe to show to a non-technical person."""


def _get(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def _split_artists(subtitle: str) -> list[str]:
    # The embed separates artists with ",<non-breaking space>", which keeps
    # names like "Tyler, The Creator" in one piece.
    parts = [p.strip() for p in (subtitle or "").split(", ")]
    return [p for p in parts if p] or ["Unknown Artist"]


def _best_image(entity: dict) -> str:
    imgs = (entity.get("visualIdentity") or {}).get("image") or []
    if imgs:
        return max(imgs, key=lambda i: i.get("maxWidth") or 0).get("url", "")
    srcs = (entity.get("coverArt") or {}).get("sources") or []
    return srcs[0].get("url", "") if srcs else ""


def parse_link(text: str) -> tuple[str, str] | None:
    m = LINK_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def fetch_link(text: str) -> dict:
    text = (text or "").strip()
    found = parse_link(text)
    if not found and re.search(r"spotify\.(link|app\.link)/", text):
        # Short share links redirect to the real one.
        try:
            final, body = _get(text if text.startswith("http") else "https://" + text)
            found = parse_link(final) or parse_link(body)
        except Exception:
            found = None
    if not found:
        raise SpotifyError(
            "That doesn't look like a Spotify link. In Spotify, tap ••• on the "
            "playlist, then Share → Copy link, and paste it here."
        )
    kind, sid = found
    try:
        _, html = _get(f"https://open.spotify.com/embed/{kind}/{sid}")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception:
        raise SpotifyError(
            "Couldn't open that playlist. Check that you're online and that the "
            "playlist isn't private. (In Spotify: ••• → Make public.)"
        )

    name = entity.get("name") or entity.get("title") or "My Playlist"
    image = _best_image(entity)
    tracks = []
    if kind == "track":
        artists = [a.get("name") for a in entity.get("artists") or [] if a.get("name")]
        tracks.append({
            "id": entity.get("uri") or f"spotify:track:{sid}",
            "title": name,
            "artists": artists or ["Unknown Artist"],
            "album": "",
            "duration": round((entity.get("duration") or 0) / 1000),
        })
    else:
        album_artist = entity.get("subtitle") if kind == "album" else ""
        for i, t in enumerate(entity.get("trackList") or []):
            if t.get("entityType", "track") != "track" or not t.get("title"):
                continue
            tracks.append({
                "id": t.get("uri") or "",
                "title": t["title"],
                "artists": _split_artists(t.get("subtitle") or album_artist),
                "album": name if kind == "album" else "",
                "number": i + 1 if kind == "album" else None,
                "year": (entity.get("releaseDate") or {}).get("isoString", "")[:4] if kind == "album" else "",
                "duration": round((t.get("duration") or 0) / 1000),
            })
    if not tracks:
        raise SpotifyError("That playlist looks empty.")
    return {
        "kind": kind,
        "name": name,
        "image": image,
        # Album covers from Spotify are great; for playlists we look them up per song.
        "album_cover": image if kind == "album" else "",
        "source": f"https://open.spotify.com/{kind}/{sid}",
        "tracks": tracks,
        "maybe_truncated": kind == "playlist" and len(tracks) >= EMBED_LIMIT,
    }


def parse_csv(text: str, filename: str = "") -> dict:
    """Parse an Exportify (exportify.app) CSV export."""
    text = (text or "").lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise SpotifyError("That file is empty or isn't a playlist export.")

    def col(row, *names):
        low = {(k or "").strip().lower(): v for k, v in row.items()}
        for n in names:
            if low.get(n):
                return low[n].strip()
        return ""

    tracks = []
    for row in rows:
        title = col(row, "track name", "title", "name", "song")
        if not title:
            continue
        raw_artists = col(row, "artist name(s)", "artist name", "artists", "artist")
        sep = ";" if ";" in raw_artists else ","
        artists = [a.strip() for a in raw_artists.split(sep) if a.strip()] or ["Unknown Artist"]
        ms = col(row, "duration (ms)", "track duration (ms)", "duration_ms")
        tracks.append({
            "id": col(row, "track uri", "uri", "spotify uri"),
            "title": title,
            "artists": artists,
            "album": col(row, "album name", "album"),
            "year": col(row, "release date", "album release date")[:4],
            "duration": round(int(ms) / 1000) if ms.isdigit() else 0,
        })
    if not tracks:
        raise SpotifyError(
            "Couldn't find any songs in that file. Make sure it's the .csv file "
            "from exportify.app."
        )
    name = re.sub(r"\.csv$", "", filename or "My Playlist", flags=re.I).replace("_", " ").strip()
    return {
        "kind": "csv",
        "name": name or "My Playlist",
        "image": "",
        "album_cover": "",
        "source": "",
        "tracks": tracks,
        "maybe_truncated": False,
    }
