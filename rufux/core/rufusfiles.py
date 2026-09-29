"""Small files from the Rufus project (by Pete Batard) that Rufux writes to drives.

- ``uefi-ntfs.img``: UEFI:NTFS, which lets UEFI firmware boot NTFS/exFAT drives.
- ``setup_x64.exe`` / ``setup_arm64.exe``: the signed Windows 11 setup wrapper that
  Rufus (4.6 and later) puts on Windows 11 24H2+ drives, so that in-place upgrades
  also work on PCs that don't meet the hardware requirements.

Installs can bundle them (see the Makefile); otherwise they are downloaded once into
the user's cache.  Every copy is pinned to one Rufus release and checked by SHA-256.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from typing import Callable

RUFUS_VERSION = "v4.15"
_BASE_URL = f"https://raw.githubusercontent.com/pbatard/rufus/{RUFUS_VERSION}/res/"
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE_DIRS = ("/usr/share/rufux", "/usr/local/share/rufux")


@dataclass(frozen=True)
class RufusFile:
    name: str       # file name in share/rufux and in the cache
    repo_path: str  # path below res/ in the Rufus repository
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return _BASE_URL + self.repo_path


UEFI_NTFS = RufusFile("uefi-ntfs.img", "uefi/uefi-ntfs.img",
                      "72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e", 1048576)

# Keyed by the architecture of the image's own setup.exe.
SETUP_WRAPPERS = {
    "x64": RufusFile("setup_x64.exe", "setup/setup_x64.exe",
                     "11df838dc69378187e1e1aaf32d34384157642d07096c6e49c1d0e7375634544", 150888),
    "arm64": RufusFile("setup_arm64.exe", "setup/setup_arm64.exe",
                       "14bd07f559513890a0f6565df3927392b4fe6b8e6fc3f5e832e9d69c8b7bb7eb", 150376),
}

MAX_SIZE = 8 * 1024 * 1024


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "rufux")


def candidates(f: RufusFile) -> list[str]:
    dirs = list(SHARE_DIRS) + [os.path.join(PACKAGE_DIR, "data"), cache_dir()]
    return [os.path.join(d, f.name) for d in dirs]


def sha256_of(path: str) -> str | None:
    try:
        with open(path, "rb") as fp:
            data = fp.read(MAX_SIZE + 1)
    except OSError:
        return None
    if len(data) > MAX_SIZE:
        return None
    return hashlib.sha256(data).hexdigest()


def find(f: RufusFile) -> str | None:
    """Return the first installed or cached copy that matches the pinned checksum."""
    for path in candidates(f):
        if os.path.isfile(path) and sha256_of(path) == f.sha256:
            return path
    return None


def identify_setup_wrapper(data: bytes) -> str | None:
    """Architecture of a known setup wrapper, or None if `data` is not one."""
    digest = hashlib.sha256(data).hexdigest()
    for arch, f in SETUP_WRAPPERS.items():
        if digest == f.sha256:
            return arch
    return None


def download(f: RufusFile, progress: Callable[[int, int], None] | None = None,
             timeout: float = 60) -> str:
    """Download the pinned file into the user cache; returns its path."""
    import urllib.request

    dest = os.path.join(cache_dir(), f.name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(f.url, headers={"User-Agent": "Rufux"})
    h = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(prefix=f".{f.name}-", dir=os.path.dirname(dest))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, os.fdopen(fd, "wb") as out:
            total = int(resp.headers.get("Content-Length") or f.size)
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > MAX_SIZE:
                    raise OSError("Unexpectedly large download")
                if progress:
                    progress(done, total)
        if h.hexdigest() != f.sha256:
            raise OSError(f"The downloaded {f.name} failed its checksum verification")
        os.replace(tmp, dest)
        tmp = ""
        return dest
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
