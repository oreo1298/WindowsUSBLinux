"""Locate or download the UEFI:NTFS boot image (by Pete Batard, from the Rufus project).

UEFI firmware can only read FAT.  To boot an NTFS/exFAT drive, Rufus adds a
tiny FAT partition containing UEFI:NTFS, which loads an NTFS/exFAT driver and
chainloads the real boot loader.  Installs can bundle the image (see the
Makefile's UEFI_NTFS option); otherwise it is downloaded once, pinned to a
Rufus release and verified by hash.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from typing import Callable

RUFUS_VERSION = "v4.15"
URL = f"https://raw.githubusercontent.com/pbatard/rufus/{RUFUS_VERSION}/res/uefi/uefi-ntfs.img"
SHA256 = "72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e"
SIZE = 1048576

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "rufux", "uefi-ntfs.img")


def candidates() -> list[str]:
    out = []
    env = os.environ.get("RUFUX_UEFI_NTFS")
    if env:
        out.append(env)
    out += [
        "/usr/share/rufux/uefi-ntfs.img",
        "/usr/local/share/rufux/uefi-ntfs.img",
        os.path.join(PACKAGE_DIR, "data", "uefi-ntfs.img"),
        cache_path(),
    ]
    return out


def is_valid(path: str, check_hash: bool = False) -> bool:
    try:
        with open(path, "rb") as f:
            data = f.read(8 * 1024 * 1024 + 1)
    except OSError:
        return False
    if not data or len(data) > 8 * 1024 * 1024 or data[510:512] != b"\x55\xaa":
        return False
    if b"FAT" not in data[0x36:0x5A]:
        return False
    if check_hash and hashlib.sha256(data).hexdigest() != SHA256:
        return False
    return True


def find() -> str | None:
    for path in candidates():
        if os.path.isfile(path) and is_valid(path):
            return path
    return None


def download(progress: Callable[[int, int], None] | None = None, timeout: float = 60) -> str:
    """Download the pinned image into the user cache; returns its path."""
    dest = cache_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "Rufux"})
    h = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(prefix=".uefi-ntfs-", dir=os.path.dirname(dest))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, os.fdopen(fd, "wb") as out:
            total = int(resp.headers.get("Content-Length") or SIZE)
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > 8 * 1024 * 1024:
                    raise OSError("Unexpectedly large download")
                if progress:
                    progress(done, total)
        if h.hexdigest() != SHA256:
            raise OSError("The downloaded UEFI:NTFS image failed its checksum verification")
        os.replace(tmp, dest)
        tmp = ""
        return dest
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
