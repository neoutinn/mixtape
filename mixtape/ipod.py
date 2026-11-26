"""Find Rockbox iPods that are plugged in, and eject them."""

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("mixtape.ipod")

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
        # e.g. mounted with plain "sudo mount": readable but we can't add songs
        "writable": os.access(path, os.W_OK),
    }


_mount_tried: dict[str, float] = {}


def _automount_linux():
    """Mount a plugged-in iPod that nothing has mounted yet (common on Arch
    without a desktop file manager). udisks lets the logged-in user do this
    without a password, and it mounts it writable for them."""
    if not sys.platform.startswith("linux") or not shutil.which("udisksctl"):
        return
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "PATH,FSTYPE,MOUNTPOINTS,MODEL,VENDOR,HOTPLUG"],
                             capture_output=True, text=True, timeout=5).stdout
        disks = json.loads(out).get("blockdevices", [])
    except Exception:
        return
    for disk in disks:
        ident = f"{disk.get('vendor') or ''} {disk.get('model') or ''}".lower()
        if not disk.get("hotplug") or not ("ipod" in ident or "apple" in ident):
            continue
        for part in disk.get("children") or []:
            mounted = [m for m in part.get("mountpoints") or [] if m]
            dev = part.get("path")
            if part.get("fstype") not in ("vfat", "exfat", "hfsplus") or mounted or not dev:
                continue
            if time.time() - _mount_tried.get(dev, 0) < 30:
                continue  # don't hammer a device that failed to mount
            _mount_tried[dev] = time.time()
            r = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                               capture_output=True, text=True, timeout=30)
            log.info("Auto-mount %s: %s", dev, (r.stdout or r.stderr).strip())


def find_ipods() -> list[dict]:
    _automount_linux()
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
