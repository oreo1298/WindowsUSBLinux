"""Small shared helpers (no GUI dependencies)."""

from __future__ import annotations

import os
import shutil

KiB = 1024
MiB = 1024 ** 2
GiB = 1024 ** 3
TiB = 1024 ** 4


def human_size(n: int | float | None, marketing: bool = False) -> str:
    """Format a byte count.

    marketing=True uses decimal units (what is printed on a USB stick's box),
    otherwise binary IEC units are used.
    """
    if n is None:
        return "?"
    n = float(n)
    if marketing:
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        base = 1000.0
    else:
        units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
        base = 1024.0
    i = 0
    while n >= base and i < len(units) - 1:
        n /= base
        i += 1
    if i == 0:
        return f"{int(n)} {units[0]}"
    if marketing:
        if n >= 10:
            return f"{n:.0f} {units[i]}"
        text = f"{n:.1f}".rstrip("0").rstrip(".")
        return f"{text} {units[i]}"
    return f"{n:.1f} {units[i]}" if n < 100 else f"{n:.0f} {units[i]}"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"


GRUB_I386_DIRS = ("/usr/lib/grub/i386-pc", "/usr/lib/grub2/i386-pc", "/usr/share/grub2/i386-pc")


def grub_bios_available() -> bool:
    """grub-install and the i386-pc (legacy BIOS) modules are both installed."""
    return which("grub-install", "grub2-install") is not None and any(
        os.path.isdir(d) for d in GRUB_I386_DIRS)


def which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name, path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        if path:
            return path
    return None
