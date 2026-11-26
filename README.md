# Mixtape

Put Spotify playlists on a Rockbox iPod, made for people who don't want to touch a terminal.

1. Open the Mixtape app. A page opens in the browser.
2. Plug in the iPod. Mixtape finds it automatically (it looks for the `.rockbox` folder).
3. Paste a Spotify playlist, album or song link and press **Put it on my iPod**.

Each song is matched on YouTube (using the Spotify song length to choose the right version), converted
to MP3, tagged with album, year, track number and genre from iTunes, and given a **500×500 baseline
JPEG cover**, which is the format Rockbox can show. A `.m3u8` playlist is written so it appears under
**Playlists** on the iPod.

The website (in `docs/`) is the friendly download page; the apps are built by GitHub Actions.

## What ends up on the iPod

```
/Music/<Artist>/<Title>.mp3      songs, shared between playlists (no duplicates)
/Playlists/<Name>.m3u8           Rockbox playlists
/.mixtape/library.json           what Mixtape added, so it can Update or Remove playlists
```

* **Update** re-reads the Spotify playlist and downloads only new songs.
* **Remove** deletes the playlist and (optionally) its songs, but only if no other playlist uses them.
* Spotify's public link only exposes the first **100** songs. For longer playlists, export a CSV
  from [exportify.app](https://exportify.app) and drop it in.

## Publishing (one-time setup)

1. Create an empty GitHub repo (e.g. `mixtape`) and push this folder to it.
2. **Settings → Pages → Build and deployment → Deploy from a branch → `main` / `/docs`.**
   The site will be at `https://<you>.github.io/mixtape/`. The download buttons work out the repo
   from that address automatically.
3. Build the apps by pushing a version tag:
   ```sh
   git tag v1.0.0
   git push origin v1.0.0
   ```
   The **Build apps** workflow builds Windows, Mac (Apple Silicon + Intel) and Linux versions and
   attaches them to a GitHub Release, which is where the website's download buttons point.

## Arch Linux

A native package lives in `packaging/arch`. It uses Arch's own `yt-dlp`, `ffmpeg` and `python-mutagen`,
adds a **Mixtape** launcher entry, and installs the `mixtape` command:

```sh
git clone https://github.com/neoutinn/mixtape
cd mixtape/packaging/arch
makepkg -si
```

On Linux, Mixtape mounts a plugged-in iPod itself through udisks (no password, mounted writable for
your user). If the iPod was mounted with a plain `sudo mount`, it's read-only for you, and Mixtape will
tell you so. Unmount it and let Mixtape handle it.

## Running from source (for development)

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m mixtape            # add --no-browser to just print the URL
```

Build a standalone app locally with `pip install pyinstaller pillow && python assets/make_icons.py && pyinstaller mixtape.spec`.

## How it's put together

| File | What it does |
| --- | --- |
| `mixtape/main.py` | Starts the local server on `127.0.0.1:47321` and opens the browser. Quits by itself a few minutes after the tab is closed. |
| `mixtape/server.py` | JSON API used by the page. It only accepts local requests with an `X-Mixtape` header, so other websites can't drive it. |
| `mixtape/ui/index.html` | The whole interface (no internet resources needed). |
| `mixtape/spotify.py` | Reads Spotify links via the public embed page (no login, no API key), plus Exportify CSVs. |
| `mixtape/songs.py` | iTunes metadata → YouTube match → download → MP3 → tags + cover. |
| `mixtape/sync.py` | Runs a sync (3 songs at a time), writes the `.m3u8`, and keeps `library.json`. |
| `mixtape/ipod.py` | Finds Rockbox iPods on Windows, Mac and Linux, and ejects them. |
| `mixtape/updater.py` | The packaged app downloads the newest yt-dlp once a day, because YouTube changes often. |

Logs are written to `%LOCALAPPDATA%\Mixtape\mixtape.log` (Windows), `~/Library/Application Support/Mixtape/`
(Mac), or `~/.local/share/mixtape/` (Linux).

## A note on use

This is for putting music you listen to onto your own device. Respect artists and the terms of the services involved.
