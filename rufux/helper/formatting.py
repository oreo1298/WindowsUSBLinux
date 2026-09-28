"""File system creation and temporary mounts."""

from __future__ import annotations

import contextlib
import os
import time
from typing import Callable, Iterator

from ..core.fsdefs import FILESYSTEMS, sanitize_label
from .context import Context, HelperError, syncfs

MOUNT_TYPES: dict[str, list[str]] = {
    "fat16": ["vfat"],
    "fat32": ["vfat"],
    "ntfs": ["ntfs3", "ntfs-3g"],
    "exfat": ["exfat"],
    "udf": ["udf"],
    "ext2": ["ext2", "ext4"],
    "ext3": ["ext3", "ext4"],
    "ext4": ["ext4"],
}

MOUNT_OPTIONS: dict[str, list[str]] = {
    "vfat": ["utf8", "shortname=mixed"],
    "exfat": ["iocharset=utf8"],
    "ntfs3": ["iocharset=utf8"],
}

# Tests can plug in alternative mount commands (e.g. FUSE drivers) here:
# fs id -> callable(dev, mountpoint, readonly) -> argv
MOUNT_OVERRIDES: dict[str, Callable[[str, str, bool], list[str]]] = {}


def mkfs(ctx: Context, fs: str, dev: str, label: str = "", cluster: int = 0,
         sector_size: int = 512) -> str:
    """Create a file system; returns the label actually used."""
    fsd = FILESYSTEMS.get(fs)
    if fsd is None:
        raise HelperError(f"Unsupported file system '{fs}'")
    tool = ctx.tool(*fsd.tools, package=fsd.package)
    label = sanitize_label(fs, label or "")
    ctx.status(f"Formatting ({fsd.name})...")
    if fs in ("fat16", "fat32"):
        cmd = [tool, "-F", "16" if fs == "fat16" else "32"]
        if label:
            cmd += ["-n", label]
        if cluster:
            cmd += ["-s", str(max(1, cluster // sector_size))]
        cmd.append(dev)
    elif fs == "ntfs":
        cmd = [tool, "--quick"]
        if label:
            cmd += ["--label", label]
        if cluster:
            cmd += ["--cluster-size", str(cluster)]
        cmd.append(dev)
    elif fs == "exfat":
        cmd = [tool]
        if label:
            cmd += ["-L", label]
        if cluster:
            cmd += ["-c", str(cluster)]
        cmd.append(dev)
    elif fs == "udf":
        cmd = [tool, "--media-type=hd", "--udfrev=2.01", f"--blocksize={sector_size}"]
        if label:
            cmd += [f"--label={label}"]
        cmd.append(dev)
    else:  # ext2/3/4
        cmd = [tool, "-F", "-q"]
        if label:
            cmd += ["-L", label]
        cmd.append(dev)
    ctx.run(cmd, timeout=None)
    return label


def _mount_cmd(fs: str, fstype: str, dev: str, mp: str, readonly: bool) -> list[str]:
    opts = ["ro" if readonly else "rw", "nosuid", "nodev", "noexec"]
    opts += MOUNT_OPTIONS.get(fstype, [])
    return ["mount", "-t", fstype, "-o", ",".join(opts), dev, mp]


@contextlib.contextmanager
def mounted(ctx: Context, dev: str, fs: str, readonly: bool = False) -> Iterator[str]:
    mp = ctx.make_temp_dir("mnt-")
    commands: list[list[str]] = []
    if fs in MOUNT_OVERRIDES:
        commands.append(MOUNT_OVERRIDES[fs](dev, mp, readonly))
    else:
        for fstype in MOUNT_TYPES.get(fs, [fs]):
            commands.append(_mount_cmd(fs, fstype, dev, mp, readonly))
    errors = []
    for cmd in commands:
        proc = ctx.run(cmd, check=False)
        if proc.returncode == 0 and os.path.ismount(mp):
            break
        errors.append((proc.stderr or b"").decode("utf-8", "replace").strip() or f"exit {proc.returncode}")
    else:
        hint = ""
        if fs == "ntfs":
            hint = " (NTFS needs the ntfs3 kernel driver or the ntfs-3g package)"
        raise HelperError(f"Could not mount {dev}: {errors[-1] if errors else 'unknown error'}{hint}")
    remove = ctx.add_cleanup(f"umount {mp}", lambda: umount(ctx, mp, quiet=True))
    try:
        yield mp
    finally:
        with contextlib.suppress(OSError):
            syncfs(mp)
        umount(ctx, mp)
        remove()


def umount(ctx: Context, mp: str, quiet: bool = False) -> None:
    if not os.path.ismount(mp):
        return
    for attempt in range(20):
        proc = ctx.run(["umount", mp], check=False, quiet=quiet or attempt > 0)
        if proc.returncode == 0 or not os.path.ismount(mp):
            return
        time.sleep(0.5)
    if not quiet:
        raise HelperError(f"Could not unmount {mp}")
