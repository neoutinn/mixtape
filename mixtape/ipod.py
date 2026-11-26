"""Find Rockbox iPods that are plugged in, mount them, and eject them.

A background Watcher keeps the list of iPods up to date. On Linux it listens to
udev, so plugging in or unplugging is noticed at once rather than on the next poll.
It mounts new iPods through udisks, retrying quickly if the iPod isn't ready yet,
and never re-mounts one that was just ejected (by us or by a file manager) until
it has been unplugged.
"""

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("mixtape.ipod")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LINUX = sys.platform.startswith("linux")
MOUNTABLE = ("vfat", "exfat", "hfsplus")
POLL_SECONDS = 2.0        # fallback re-scan, in case an event is missed
EJECTED_NOTICE_SECONDS = 60
BUSY_MESSAGE = ("Something is still using the iPod. Close any windows showing its "
                "files and press Eject again.")


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


def _proc_mounts():
    """[(device, mount point, fstype)] from /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            return [(p[0], p[1].replace("\\040", " "), p[2])
                    for p in (line.split() for line in f) if len(p) > 2]
    except OSError:
        return []


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
    for _dev, mnt, fstype in _proc_mounts():
        if fstype in MOUNTABLE + ("fuseblk",):
            yield mnt


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


def find_ipods() -> list[dict]:
    """Mounted Rockbox iPods (doesn't mount anything)."""
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


# ---------------------------------------------------------------- Linux helpers

def _lsblk() -> list[dict]:
    # Flat list (-l) + PKNAME links each partition to its disk. (Without NAME
    # in the columns lsblk gives no tree, so don't rely on "children".)
    out = subprocess.run(
        ["lsblk", "-J", "-l", "-o", "NAME,PKNAME,PATH,TYPE,FSTYPE,MOUNTPOINTS,MODEL,VENDOR,HOTPLUG"],
        capture_output=True, text=True, timeout=5).stdout
    return json.loads(out).get("blockdevices", [])


def _mountpoints(dev: dict) -> list[str]:
    return [m for m in dev.get("mountpoints") or [] if m]


def _ipod_disks(devices):
    """[(disk, partitions)] for every plugged-in iPod."""
    for disk in devices:
        ident = f"{disk.get('vendor') or ''} {disk.get('model') or ''}".lower()
        if disk.get("type") == "disk" and disk.get("hotplug") and ("ipod" in ident or "apple" in ident):
            yield disk, [d for d in devices if d.get("pkname") == disk.get("name")]


def _udisks(*args, timeout=30) -> tuple[bool, str]:
    try:
        r = subprocess.run(["udisksctl", *args, "--no-user-interaction"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "the iPod didn't answer in time"
    msg = (r.stdout.strip() if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())
    return r.returncode == 0, msg


def _unmount(dev: str) -> tuple[bool, str]:
    """Unmount, retrying for a few seconds if something has a file open."""
    msg = ""
    for attempt in range(8):
        ok, msg = _udisks("unmount", "-b", dev, timeout=60)
        if ok or "NotMounted" in msg or "not mounted" in msg.lower():
            return True, msg
        if "busy" not in msg.lower():
            break
        time.sleep(0.5)
    return False, msg


# ---------------------------------------------------------------- other systems

def _eject_other(path: str) -> tuple[bool, str]:
    """Eject on Windows/macOS, or on Linux without udisks."""
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
    return False, "Please eject the iPod from your file manager."


# ---------------------------------------------------------------- the watcher

class Watcher:
    def __init__(self):
        self.lock = threading.RLock()          # held while scanning or ejecting
        self.changed = threading.Condition()
        self.wake = threading.Event()
        self.gen = 0                           # goes up whenever something visible changes
        self.ipods: list[dict] = []
        self.status = {"connecting": False, "ejected": False, "error": ""}
        self._key = None
        self._ejected: set[str] = set()        # iPod disks ejected but still plugged in
        self._ejected_at = 0.0
        self._mounted: set[str] = set()        # partitions we've seen mounted
        self._fails: dict[str, list] = {}      # partition -> [tries, next try, message]
        self._lsblk_failed = False
        self._can_mount = LINUX and all(shutil.which(c) for c in ("udisksctl", "lsblk"))

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="ipod-watcher").start()
        if LINUX and shutil.which("udevadm"):
            threading.Thread(target=self._udev, daemon=True, name="ipod-udev").start()

    def poke(self):
        """Re-scan now."""
        self.wake.set()

    def touch(self):
        """Tell waiting pages something changed (e.g. a different iPod was picked)."""
        with self.changed:
            self.gen += 1
            self.changed.notify_all()

    def wait(self, gen: int, timeout: float):
        """Block until something changes (or timeout), for the page's long-poll."""
        with self.changed:
            self.changed.wait_for(lambda: self.gen != gen, timeout)

    # -- background threads

    def _loop(self):
        while True:
            try:
                delay = self._scan()
            except Exception:
                log.exception("iPod scan failed")
                delay = POLL_SECONDS
            self.wake.wait(delay)
            self.wake.clear()

    def _udev(self):
        """Wake the scanner on every block-device event (plug, unplug, partition probed)."""
        while True:
            try:
                p = subprocess.Popen(["udevadm", "monitor", "--udev", "--subsystem-match=block"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    if line.startswith("UDEV"):
                        self.wake.set()
                p.wait()
            except Exception as e:
                log.info("udevadm monitor failed: %s", e)
            time.sleep(5)  # polling still works meanwhile

    # -- scanning

    def _scan(self) -> float:
        with self.lock:
            delay, connecting, error = POLL_SECONDS, False, ""
            if self._can_mount:
                delay, connecting, error = self._mount_pass()
            found = find_ipods()
            now = time.time()
            if now - self._ejected_at > EJECTED_NOTICE_SECONDS and not self._ejected:
                self._ejected_at = 0.0
            ejected = bool(self._ejected) or self._ejected_at > 0
            self._publish(found, {"connecting": connecting and not found,
                                  "ejected": ejected and not connecting,
                                  "error": error if not found else ""})
            if self._ejected_at:
                delay = min(delay, max(0.5, self._ejected_at + EJECTED_NOTICE_SECONDS + 0.1 - now))
            return delay

    def _publish(self, found, status):
        key = (tuple((i["path"], i["name"], i["writable"], i["rockbox"]) for i in found),
               tuple(sorted(status.items())))
        with self.changed:
            self.ipods, self.status = found, status
            if key != self._key:
                self._key = key
                self.gen += 1
                self.changed.notify_all()

    def _mount_pass(self):
        """Mount any plugged-in iPod that isn't mounted. Returns (next delay, connecting, error)."""
        try:
            devices = _lsblk()
            self._lsblk_failed = False
        except Exception as e:
            if not self._lsblk_failed:
                log.info("lsblk failed: %s", e)
            self._lsblk_failed = True
            return POLL_SECONDS, False, ""
        now = time.time()
        delay, connecting, error = POLL_SECONDS, False, ""
        disks, parts_seen = set(), set()
        for disk, parts in _ipod_disks(devices):
            name = disk["name"]
            disks.add(name)
            if name in self._ejected:
                continue
            usable = [p for p in parts if p.get("fstype") in MOUNTABLE and p.get("path")]
            if not usable:
                connecting = True  # udev hasn't finished reading the partitions yet
                continue
            for part in usable:
                dev = part["path"]
                parts_seen.add(dev)
                if _mountpoints(part):
                    self._mounted.add(dev)
                    self._fails.pop(dev, None)
                    continue
                if dev in self._mounted:
                    # It was mounted, now it isn't, and the iPod is still plugged in:
                    # it was ejected somewhere else. Don't fight that by re-mounting.
                    log.info("%s was unmounted outside Mixtape; leaving it alone until it's unplugged", dev)
                    self._ejected.add(name)
                    self._ejected_at = now
                    break
                fail = self._fails.get(dev)
                if fail and now < fail[1]:
                    connecting = True
                    delay = min(delay, fail[1] - now)
                    if fail[0] >= 3:
                        error = fail[2]
                    continue
                if not self.status.get("connecting"):
                    # Mounting can take a few seconds while the iPod's disk spins up;
                    # let the page say "opening it" meanwhile.
                    self._publish(self.ipods, dict(self.status, connecting=True, ejected=False))
                ok, msg = _udisks("mount", "-b", dev)
                if ok or "AlreadyMounted" in msg:
                    log.info("Mounted %s: %s", dev, msg)
                    self._ejected_at = 0.0  # plugged back in: the old "ejected" note is stale
                    self._mounted.add(dev)
                    self._fails.pop(dev, None)
                    continue
                tries = (fail[0] if fail else 0) + 1
                wait = min(0.5 * 2 ** (tries - 1), 8.0)  # 0.5, 1, 2, 4, 8, 8, ...
                self._fails[dev] = [tries, now + wait, msg]
                log.info("Mount %s failed (try %d, again in %.1fs): %s", dev, tries, wait, msg)
                connecting = True
                delay = min(delay, wait)
                if tries >= 3:
                    error = msg
        # Forget everything about iPods that have been unplugged.
        if self._ejected - disks:
            self._ejected &= disks
        self._mounted &= parts_seen
        for dev in [d for d in self._fails if d not in parts_seen]:
            del self._fails[dev]
        return delay, connecting, error

    # -- ejecting

    def eject(self, path: str) -> tuple[bool, str]:
        with self.lock:
            try:
                ok, msg = self._eject_linux(path) if self._can_mount else _eject_other(path)
            except Exception as e:
                log.exception("eject failed")
                ok, msg = False, str(e)
            if ok:
                self._ejected_at = time.time()
        self.poke()
        return ok, msg

    def _eject_linux(self, path: str) -> tuple[bool, str]:
        os.sync()
        mnt = os.path.realpath(path).rstrip("/") or "/"
        dev = next((d for d, m, _ in _proc_mounts() if m == mnt), "")
        devices = _lsblk()
        part = next((d for d in devices if d.get("path") == dev), None)
        if not part:
            return _eject_other(path)
        disk = part.get("pkname") or part["name"]
        # Mark it first, so the scanner can't mount it again halfway through.
        self._ejected.add(disk)
        for p in devices:
            if (p.get("pkname") == disk or p.get("name") == disk) and _mountpoints(p):
                ok, msg = _unmount(p["path"])
                if not ok:
                    self._ejected.discard(disk)
                    log.warning("Unmount %s failed: %s", p["path"], msg)
                    return False, BUSY_MESSAGE if "busy" in msg.lower() else (msg or BUSY_MESSAGE)
                log.info("Unmounted %s", p["path"])
        ok, msg = _udisks("power-off", "-b", f"/dev/{disk}")
        # Even if power-off isn't supported, it's unmounted and safe to unplug.
        log.info("Power off /dev/%s: %s", disk, "ok" if ok else msg)
        return True, "ejected"
