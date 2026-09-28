"""File system definitions: tools, labels and Rufus-compatible cluster sizes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .util import GiB, MiB, TiB, which

KB = 1024
GUID_MS_BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GUID_EFI_SYSTEM = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GUID_LINUX_DATA = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"

FAT32_CLUSTER_THRESHOLD = 1.011  # cluster size changes happen slightly above powers of 2


@dataclass(frozen=True)
class FsDef:
    id: str
    name: str
    tools: tuple[str, ...]
    package: str
    label_max: int
    mbr_type: str
    gpt_type: str = GUID_MS_BASIC_DATA
    max_file: int | None = None
    min_size: int = 1 * MiB
    max_size: int | None = None
    uefi_native: bool = False  # UEFI firmware can read it without extra drivers
    label_upper: bool = False

    def tool(self) -> str | None:
        return which(*self.tools)

    @property
    def available(self) -> bool:
        return self.tool() is not None


FILESYSTEMS: dict[str, FsDef] = {
    "fat16": FsDef("fat16", "FAT", ("mkfs.fat", "mkfs.vfat", "mkdosfs"), "dosfstools", 11, "0e",
                   max_file=0xFFFFFFFF, min_size=8 * MiB, max_size=4 * GiB - 1, uefi_native=True,
                   label_upper=True),
    "fat32": FsDef("fat32", "FAT32", ("mkfs.fat", "mkfs.vfat", "mkdosfs"), "dosfstools", 11, "0c",
                   max_file=0xFFFFFFFF, min_size=32 * MiB, max_size=2 * TiB, uefi_native=True,
                   label_upper=True),
    "ntfs": FsDef("ntfs", "NTFS", ("mkfs.ntfs", "mkntfs"), "ntfs-3g", 32, "07", min_size=8 * MiB),
    "exfat": FsDef("exfat", "exFAT", ("mkfs.exfat",), "exfatprogs", 15, "07", min_size=8 * MiB),
    "udf": FsDef("udf", "UDF", ("mkudffs", "mkfs.udf"), "udftools", 30, "07", min_size=8 * MiB),
    "ext2": FsDef("ext2", "ext2", ("mkfs.ext2",), "e2fsprogs", 16, "83", GUID_LINUX_DATA,
                  min_size=16 * MiB),
    "ext3": FsDef("ext3", "ext3", ("mkfs.ext3",), "e2fsprogs", 16, "83", GUID_LINUX_DATA,
                  min_size=16 * MiB),
    "ext4": FsDef("ext4", "ext4", ("mkfs.ext4",), "e2fsprogs", 16, "83", GUID_LINUX_DATA,
                  min_size=16 * MiB),
}

ORDER = ["fat16", "fat32", "ntfs", "udf", "exfat", "ext2", "ext3", "ext4"]


def cluster_sizes(fs: str, size: int, sector_size: int = 512) -> tuple[list[int], int]:
    """Return (allowed cluster sizes in bytes, default) the way Rufus computes them.

    An empty list means the file system is not possible for that size; a list
    containing only 0 means "use the formatter's default" (no choice).
    """
    allowed = 0
    default = 0
    if fs == "fat16":
        if size >= 4 * GiB or size < 8 * MiB:
            return [], 0
        allowed = 0x00001E00
        i = 32
        while i <= 4096:
            if size < i * MiB:
                default = 16 * i
                break
            allowed <<= 1
            i <<= 1
        allowed &= 0x0001FE00
    elif fs == "fat32":
        if size < 32 * MiB or size >= 2 * TiB:
            return [], 0
        allowed = 0x000001F8
        i = 32
        while i <= 32 * 1024:
            if size < i * MiB * FAT32_CLUSTER_THRESHOLD:
                default = 8 * i
                break
            allowed <<= 1
            i <<= 1
        allowed &= 0x0001FE00
        if 256 * MiB <= size < 32 * GiB:
            for g in (8, 16, 32):
                if size < g * GiB * FAT32_CLUSTER_THRESHOLD:
                    default = (g // 2) * KB
                    break
        if size >= 32 * GiB:
            allowed &= 0x0001C000
            default = 0x8000
    elif fs == "ntfs":
        if size >= 256 * TiB or size < 8 * MiB:
            return [], 0
        allowed = 0x0001F000
        for t in (16, 32, 64, 128, 256):
            if size < t * TiB:
                default = (t // 4) * KB
                break
    elif fs == "exfat":
        if size < 8 * MiB:
            return [], 0
        allowed = 0x03FFFE00
        if size < 256 * MiB:
            default = 4 * KB
        elif size < 32 * GiB:
            default = 32 * KB
        else:
            default = 128 * KB
    elif fs in ("udf", "ext2", "ext3", "ext4"):
        fsd = FILESYSTEMS[fs]
        if size < fsd.min_size:
            return [], 0
        return [0], 0
    else:
        return [], 0
    allowed &= ~(sector_size - 1)
    sizes = [1 << b for b in range(9, 27) if allowed & (1 << b)]
    if not sizes:
        return [], 0
    if default not in sizes:
        default = sizes[0]
    return sizes, default


def cluster_label(n: int, default: bool = False) -> str:
    """Rufus-style cluster size label, e.g. "4096 bytes (Default)"."""
    if n == 0:
        return "Default"
    if n <= 8192:
        text = f"{n} bytes"
    elif n < MiB:
        text = f"{n // KB} kilobytes"
    else:
        text = f"{n // MiB} megabytes"
    return f"{text} (Default)" if default else text


_FAT_INVALID = re.compile(r'[\x00-\x1f"*+,./:;<=>?\[\\\]|\x7f]')


def sanitize_label(fs: str, label: str) -> str:
    fsd = FILESYSTEMS.get(fs)
    label = "".join(ch for ch in label if ch >= " " and ch != "\x7f").strip()
    if fsd is None:
        return label
    if fs in ("fat16", "fat32"):
        label = label.encode("ascii", "replace").decode("ascii").replace("?", "_")
        label = _FAT_INVALID.sub("_", label).upper()
        return label[:11].rstrip()
    if fs.startswith("ext"):
        out = ""
        for ch in label:
            if len((out + ch).encode("utf-8")) > 16:
                break
            out += ch
        return out
    if fs == "exfat":
        return _truncate_utf16(label, 15)
    if fs == "ntfs":
        return _truncate_utf16(label, 32)
    return label[:fsd.label_max]


def _truncate_utf16(text: str, units: int) -> str:
    out = ""
    for ch in text:
        if len((out + ch).encode("utf-16-le")) // 2 > units:
            break
        out += ch
    return out
