# PyInstaller build recipe. Build with:  pyinstaller mixtape.spec
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [("mixtape/ui", "mixtape/ui")] + collect_data_files("imageio_ffmpeg")
binaries = collect_dynamic_libs("imageio_ffmpeg")
hidden = collect_submodules("yt_dlp") + ["mutagen.id3", "mutagen.mp3"]
icon = {"win32": "assets/icon.ico", "darwin": "assets/icon.icns"}.get(sys.platform)

a = Analysis(["run_mixtape.py"], datas=datas, binaries=binaries, hiddenimports=hidden)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Mixtape", console=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name="Mixtape")
    app = BUNDLE(
        coll, name="Mixtape.app", icon=icon, bundle_identifier="io.github.mixtape",
        info_plist={
            # No Dock icon: Mixtape lives in the browser and quits by itself when the tab closes.
            "LSUIElement": True,
            "CFBundleShortVersionString": "1.0.0",
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Mixtape",
              console=False, icon=icon, upx=False)
