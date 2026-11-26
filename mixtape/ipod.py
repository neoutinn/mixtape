"""Find Rockbox iPods that are plugged in, and eject them."""

import glob
import os
import shutil
import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _is_rockbox(path: str) -> bool:
    try:
        return os.path.isdir(os.path.join(path, ".rockbox"))
    except OSError:
        return False


def _windows_drives():
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(0x0001)  # don't pop up "insert a disk" dialogs
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            yield f"{chr(65 + i)}:\\"


def _windows_label(root: str) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0
    )
    return buf.value if ok else ""


def _candidates():
    if sys.platform == "win32":
        yield from _windows_drives()
        return
    if sys.platform == "darwin":
        yield from glob.glob("/Volumes/*")
        return
    user = os.environ.get("USER", "")
    for pattern in (f"/run/media/{user}/*", f"/media/{user}/*", "/media/*", "/mnt/*", "/mnt"):
        yield from glob.glob(pattern)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2 and parts[2] in ("vfat", "exfat", "hfsplus", "fuseblk"):
                    yield parts[1].replace("\\040", " ")
    except OSError:
        pass


def describe(path: str) -> dict:
    usage = shutil.disk_usage(path)
    if sys.platform == "win32":
        name = _windows_label(path) or f"iPod ({path[:2]})"
    else:
        name = os.path.basename(path.rstrip("/")) or "iPod"
    return {
        "path": path,
        "name": name,
        "free": usage.free,
        "total": usage.total,
        "rockbox": _is_rockbox(path),
    }


def find_ipods() -> list[dict]:
    seen, found = set(), []
    for p in _candidates():
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        if _is_rockbox(p):
            try:
                found.append(describe(p))
            except OSError:
                pass
    return found


def eject(path: str) -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        if hasattr(os, "sync"):
            os.sync()
        if sys.platform == "win32":
            letter = path[:2]
            ps = (
                "$s=New-Object -ComObject Shell.Application;"
                f"$s.Namespace(17).ParseName('{letter}').InvokeVerb('Eject')"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
            return True, "ejected"
        if sys.platform == "darwin":
            r = subprocess.run(["diskutil", "eject", path], capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stderr or r.stdout).strip()
        # Linux: find the block device behind the mount point
        dev = ""
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[1].replace("\\040", " ") == path.rstrip("/"):
                    dev = parts[0]
        if not dev or not shutil.which("udisksctl"):
            return False, "Please eject the iPod from your file manager."
        r = subprocess.run(["udisksctl", "unmount", "-b", dev], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, r.stderr.strip() or "Please eject the iPod from your file manager."
        subprocess.run(["udisksctl", "power-off", "-b", dev], capture_output=True, timeout=30)
        return True, "ejected"
    except Exception as e:
        return False, str(e)
