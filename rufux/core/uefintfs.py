"""Locate or download the UEFI:NTFS boot image (by Pete Batard, from the Rufus project).

UEFI firmware can only read FAT.  To boot an NTFS/exFAT drive, Rufus adds a
tiny FAT partition containing UEFI:NTFS, which loads an NTFS/exFAT driver and
chainloads the real boot loader.  Installs can bundle the image (see the
Makefile's UEFI_NTFS option); otherwise it is downloaded once, pinned to a
Rufus release and verified by hash (see rufusfiles).
"""

from __future__ import annotations

import hashlib
import os
from typing import Callable

from . import rufusfiles

RUFUS_VERSION = rufusfiles.RUFUS_VERSION
URL = rufusfiles.UEFI_NTFS.url
SHA256 = rufusfiles.UEFI_NTFS.sha256
SIZE = rufusfiles.UEFI_NTFS.size


def cache_path() -> str:
    return os.path.join(rufusfiles.cache_dir(), rufusfiles.UEFI_NTFS.name)


def candidates() -> list[str]:
    out = []
    env = os.environ.get("RUFUX_UEFI_NTFS")
    if env:
        out.append(env)
    return out + rufusfiles.candidates(rufusfiles.UEFI_NTFS)


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
    return rufusfiles.download(rufusfiles.UEFI_NTFS, progress, timeout)
