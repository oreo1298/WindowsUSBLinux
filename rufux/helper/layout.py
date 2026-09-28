"""Partition layout planning (pure logic, mirrors Rufus' CreatePartition())."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.fsdefs import FILESYSTEMS, GUID_LINUX_DATA, GUID_MS_BASIC_DATA

MiB = 1024 * 1024
MIN_MAIN_SIZE = 8 * MiB


class LayoutError(Exception):
    pass


@dataclass
class PartSpec:
    role: str  # main | uefi-ntfs | persistence
    start: int  # bytes
    size: int  # bytes
    name: str
    mbr_type: str
    gpt_type: str
    bootable: bool = False
    gpt_attrs: str = ""


def _down(x: int, a: int) -> int:
    return x // a * a


def _up(x: int, a: int) -> int:
    return (x + a - 1) // a * a


def plan_layout(disk_size: int, sector_size: int, scheme: str, main_fs: str, bootable: bool,
                uefi_ntfs_size: int = 0, persistence_size: int = 0) -> list[PartSpec]:
    if scheme not in ("mbr", "gpt"):
        raise LayoutError(f"Unknown partition scheme {scheme}")
    if main_fs not in FILESYSTEMS:
        raise LayoutError(f"Unknown file system {main_fs}")
    align = MiB
    first = MiB
    end = disk_size
    if scheme == "gpt":
        end -= 16384 + sector_size  # backup partition entries + header
    end = _down(end, align)
    tail: list[PartSpec] = []
    cur = end
    if uefi_ntfs_size:
        size = _up(uefi_ntfs_size, align)
        cur -= size
        tail.insert(0, PartSpec("uefi-ntfs", cur, size, "UEFI:NTFS", "ef", GUID_MS_BASIC_DATA,
                                gpt_attrs="GUID:63"))
    if persistence_size:
        size = _down(persistence_size, align)
        if size < 16 * MiB:
            raise LayoutError("Persistent partition is too small")
        cur -= size
        tail.insert(0, PartSpec("persistence", cur, size, "Linux Persistence", "83", GUID_LINUX_DATA))
    main_size = cur - first
    if main_size < MIN_MAIN_SIZE:
        raise LayoutError("The drive is too small for this layout")
    fsd = FILESYSTEMS[main_fs]
    main = PartSpec("main", first, main_size, "Main Data Partition", fsd.mbr_type, fsd.gpt_type,
                    bootable=bootable and scheme == "mbr")
    return [main] + tail


def sfdisk_script(specs: list[PartSpec], scheme: str, sector_size: int) -> str:
    lines = [f"label: {'gpt' if scheme == 'gpt' else 'dos'}", "unit: sectors"]
    for s in specs:
        if s.start % sector_size or s.size % sector_size:
            raise LayoutError("Partition not aligned to the sector size")
        fields = [f"start={s.start // sector_size}", f"size={s.size // sector_size}"]
        if scheme == "gpt":
            fields.append(f"type={s.gpt_type}")
            fields.append(f'name="{s.name}"')
            if s.gpt_attrs:
                fields.append(f'attrs="{s.gpt_attrs}"')
        else:
            fields.append(f"type={s.mbr_type}")
            if s.bootable:
                fields.append("bootable")
        lines.append(", ".join(fields))
    return "\n".join(lines) + "\n"


def describe(specs: list[PartSpec]) -> list[str]:
    out = []
    for i, s in enumerate(specs, 1):
        out.append(f"Partition {i}: {s.name} (offset {s.start}, size {s.size // MiB} MiB"
                   + (", active" if s.bootable else "") + ")")
    return out


__all__ = ["PartSpec", "LayoutError", "plan_layout", "sfdisk_script", "describe", "MiB",
           "GUID_MS_BASIC_DATA"]
