"""UI decision logic, independent of Qt: allowed choices, defaults, validation, jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .. import HELPER_PROTOCOL
from .blockdevs import Drive, image_on_drive
from .fsdefs import FILESYSTEMS, ORDER, cluster_sizes, sanitize_label
from .image import ImageInfo
from .unattend import RegionalSettings, WueOptions, build_unattend
from .util import GiB, MiB, human_size, which

BOOT_IMAGE = "image"
BOOT_NONE = "nonboot"

TARGET_UEFI = "uefi"
TARGET_BIOS_UEFI = "bios+uefi"
TARGET_BIOS = "bios"

TARGET_LABELS = {
    TARGET_UEFI: "UEFI (non CSM)",
    TARGET_BIOS_UEFI: "BIOS or UEFI",
    TARGET_BIOS: "BIOS (or UEFI-CSM)",
}

MODE_DD = "dd"
MODE_ISO = "iso"


@dataclass
class Tools:
    """What is installed on this system (optional features depend on it)."""

    wimlib: bool = False
    grub_bios: bool = False
    fs: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def detect(cls) -> "Tools":
        grub = which("grub-install", "grub2-install") is not None and any(
            os.path.isdir(d) for d in ("/usr/lib/grub/i386-pc", "/usr/lib/grub2/i386-pc"))
        return cls(wimlib=which("wimlib-imagex") is not None, grub_bios=grub,
                   fs={fs: FILESYSTEMS[fs].available for fs in ORDER})


@dataclass
class Selection:
    drive: Drive | None = None
    boot: str = BOOT_IMAGE
    image: ImageInfo | None = None
    mode: str = MODE_ISO
    scheme: str = "gpt"
    target: str = TARGET_UEFI
    fs: str = "fat32"
    cluster: int = 0
    label: str = ""
    quick: bool = True
    extended_label: bool = True
    badblocks: int = 0
    verify: bool = True
    persistence_size: int = 0
    wue: WueOptions = field(default_factory=WueOptions)
    regional: RegionalSettings | None = None
    uefi_ntfs: str | None = None
    eject: bool = False


# ---------------------------------------------------------------------------
# choices
# ---------------------------------------------------------------------------


def modes_for(info: ImageInfo | None) -> list[str]:
    if info is None:
        return []
    out = []
    if info.can_dd_mode and info.recommended_mode == MODE_DD:
        out.append(MODE_DD)
    if info.can_iso_mode:
        out.append(MODE_ISO)
    if info.can_dd_mode and MODE_DD not in out:
        out.append(MODE_DD)
    return out


def splittable_wim(info: ImageInfo | None) -> bool:
    return bool(info and info.windows and info.windows.wim_path.lower().endswith(".wim")
                and info.only_splittable_big_files)


def filesystems_for(sel: Selection, tools: Tools, drive_size: int) -> list[str]:
    """File systems offered in the combo box for the current selection."""
    if sel.boot == BOOT_IMAGE and sel.image is not None and sel.mode == MODE_DD:
        return []
    size = drive_size or 64 * GiB
    if sel.boot == BOOT_NONE or sel.image is None:
        cands = ["fat16", "fat32", "ntfs", "udf", "exfat", "ext2", "ext3", "ext4"]
    else:
        info = sel.image
        if info.is_windows:
            cands = ["fat32", "ntfs", "exfat"]
        else:
            # Linux & others: UEFI firmware reads FAT natively; NTFS/exFAT via UEFI:NTFS.
            cands = ["fat32", "ntfs", "exfat"]
            if info.persistence:
                cands = ["fat32", "ntfs"]
    out = []
    for fs in cands:
        if not tools.fs.get(fs, True):
            continue
        sizes, _ = cluster_sizes(fs, size)
        if sizes:
            out.append(fs)
    return out


def default_filesystem(sel: Selection, tools: Tools, drive_size: int) -> str:
    options = filesystems_for(sel, tools, drive_size)
    if not options:
        return ""
    if sel.boot == BOOT_NONE or sel.image is None:
        pref = "fat32" if drive_size <= 32 * GiB else "exfat"
    else:
        info = sel.image
        fat_ok = not info.has_4gb_file or (splittable_wim(info) and tools.wimlib)
        if fat_ok:
            pref = "fat32"
        else:
            pref = "ntfs"
    if pref in options:
        return pref
    for fallback in ("fat32", "ntfs", "exfat"):
        if fallback in options:
            return fallback
    return options[0]


def schemes_for(sel: Selection) -> list[str]:
    if sel.boot == BOOT_IMAGE and sel.image is not None and sel.mode == MODE_DD:
        return [sel.image.part_table or "mbr"]
    return ["mbr", "gpt"]


def default_scheme(sel: Selection) -> str:
    if sel.boot == BOOT_IMAGE and sel.image is not None:
        if sel.mode == MODE_DD:
            return sel.image.part_table or "mbr"
        return "gpt"
    return "mbr"


def bios_possible(sel: Selection, tools: Tools) -> bool:
    info = sel.image
    return bool(info and info.is_windows and info.has_bootmgr and tools.grub_bios)


def targets_for(sel: Selection, tools: Tools) -> list[str]:
    if sel.boot == BOOT_NONE or sel.image is None:
        return [TARGET_BIOS_UEFI]
    if sel.mode == MODE_DD:
        info = sel.image
        if info.bios_bootable and (info.efi_bootable or info.has_eltorito_efi or info.part_table == "gpt"):
            return [TARGET_BIOS_UEFI]
        if info.efi_bootable or info.part_table == "gpt":
            return [TARGET_UEFI]
        return [TARGET_BIOS_UEFI]
    if sel.scheme == "gpt":
        return [TARGET_UEFI]
    if bios_possible(sel, tools):
        return [TARGET_BIOS_UEFI, TARGET_UEFI]
    return [TARGET_UEFI]


def default_label(sel: Selection) -> str:
    if sel.boot == BOOT_IMAGE and sel.image is not None:
        return sel.image.label or os.path.splitext(sel.image.name)[0]
    if sel.drive is not None:
        return sel.drive.label
    return ""


def needs_uefi_ntfs(sel: Selection) -> bool:
    return (sel.boot == BOOT_IMAGE and sel.image is not None and sel.mode == MODE_ISO
            and sel.fs in ("ntfs", "exfat") and sel.target in (TARGET_UEFI, TARGET_BIOS_UEFI))


def will_split_wim(sel: Selection, tools: Tools) -> bool:
    return (sel.boot == BOOT_IMAGE and sel.image is not None and sel.mode == MODE_ISO
            and sel.fs in ("fat32", "fat16") and splittable_wim(sel.image) and tools.wimlib)


def max_persistence(sel: Selection) -> int:
    if not sel.drive or not sel.image:
        return 0
    free = sel.drive.size - sel.image.total_bytes - 64 * MiB
    return max(0, free // MiB * MiB)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class PlanError(Exception):
    pass


def validate(sel: Selection, tools: Tools) -> list[str]:
    """Raise PlanError for blocking problems; return a list of warnings."""
    warnings: list[str] = []
    d = sel.drive
    if d is None:
        raise PlanError("Please select a device.")
    if d.read_only:
        raise PlanError("The selected device is write protected.")
    if d.busy_reason:
        raise PlanError(d.busy_reason)
    if sel.boot == BOOT_IMAGE:
        info = sel.image
        if info is None:
            raise PlanError("Please select an image first (click SELECT).")
        if info.errors and info.kind == "unknown":
            raise PlanError("The selected image could not be read: " + "; ".join(info.errors[:2]))
        if image_on_drive(info.path, d):
            raise PlanError("The selected image is stored on the target drive. "
                            "Copy it somewhere else first.")
        if sel.mode == MODE_DD:
            size = info.data_size
            if size and size > d.size:
                raise PlanError(f"The image ({human_size(size)}) is too big for the selected device "
                                f"({human_size(d.size)}).")
            if info.kind == "unknown":
                warnings.append("The image type could not be identified; it will be written as-is.")
        else:
            if not info.can_iso_mode:
                raise PlanError("This image cannot be written in ISO mode.")
            if info.total_bytes + 64 * MiB + sel.persistence_size > d.size:
                raise PlanError(f"The image contents ({human_size(info.total_bytes)}) do not fit on the "
                                f"selected device ({human_size(d.size)}).")
            if sel.fs in ("fat32", "fat16") and info.has_4gb_file:
                if not will_split_wim(sel, tools):
                    if splittable_wim(info) and not tools.wimlib:
                        raise PlanError("This image contains a file larger than 4 GB (install.wim), which "
                                        "FAT32 cannot store. Install 'wimlib' (sudo pacman -S wimlib) so it "
                                        "can be split, or select NTFS.")
                    raise PlanError(f"This image contains a file larger than 4 GB ({info.largest_file}), "
                                    "which is more than FAT32 allows. Please select NTFS or exFAT.")
            if needs_uefi_ntfs(sel) and not sel.uefi_ntfs:
                raise PlanError("The UEFI:NTFS boot image is required for NTFS/exFAT boot drives.")
            if sel.target == TARGET_BIOS_UEFI and info.is_windows and not tools.grub_bios:
                raise PlanError("Legacy BIOS boot needs GRUB (sudo pacman -S grub).")
            if not info.is_windows and not info.efi_bootable:
                warnings.append("This image has no UEFI boot loader files, so the drive may not boot in "
                                "ISO mode. DD mode is usually the better choice for this image.")
            if not info.is_windows and info.is_hybrid:
                warnings.append("Some Linux distributions only boot reliably when written in DD mode. "
                                "If the drive does not boot, write it again in DD mode.")
    if sel.boot == BOOT_NONE or sel.mode == MODE_ISO:
        if not sel.fs:
            raise PlanError("No usable file system is available for this drive.")
        fsd = FILESYSTEMS[sel.fs]
        if not tools.fs.get(sel.fs, True):
            raise PlanError(f"Formatting as {fsd.name} requires the '{fsd.package}' package "
                            f"(sudo pacman -S {fsd.package}).")
        sizes, _ = cluster_sizes(sel.fs, d.size, d.log_sec)
        if not sizes:
            raise PlanError(f"{fsd.name} cannot be used on a drive of this size.")
    return warnings


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------


def build_job(sel: Selection, tools: Tools) -> dict:
    d = sel.drive
    assert d is not None
    job: dict = {
        "protocol": HELPER_PROTOCOL,
        "action": "write",
        "device": {"path": d.path, "size": d.size, "serial": d.serial, "model": d.model},
        "verify": sel.verify,
        "badblocks": sel.badblocks,
        "eject": sel.eject,
    }
    if sel.boot == BOOT_IMAGE and sel.image is not None and sel.mode == MODE_DD:
        job.update(mode="dd", image=sel.image.path, compression=sel.image.compression)
        return job
    job.update(
        mode="format" if sel.boot == BOOT_NONE else "iso",
        scheme=sel.scheme,
        filesystem=sel.fs,
        cluster=sel.cluster,
        label=sanitize_label(sel.fs, sel.label),
        quick=sel.quick,
        extended_label=sel.extended_label,
    )
    if sel.boot == BOOT_IMAGE and sel.image is not None:
        info = sel.image
        job["image"] = info.path
        iso: dict = {"iso_label": info.label, "patch_labels": not info.is_windows}
        if will_split_wim(sel, tools) and info.windows is not None:
            iso["split_wim"] = info.windows.wim_path
        if sel.target == TARGET_BIOS_UEFI and info.is_windows and sel.scheme == "mbr":
            iso["bios_grub"] = True
        if info.windows is not None and info.windows.supports_wue and sel.wue.any():
            built = build_unattend(sel.wue, info.windows.arch or "x64", sel.regional)
            if built is not None:
                iso["unattend"], iso["unattend_target"] = built
                iso["bypass_appraiser"] = sel.wue.bypass_requirements
        if info.persistence and sel.persistence_size > 0:
            iso["persistence"] = info.persistence
            job["persistence_size"] = sel.persistence_size
        if needs_uefi_ntfs(sel):
            job["uefi_ntfs"] = sel.uefi_ntfs
        job["iso"] = iso
    return job


def summary(sel: Selection, tools: Tools) -> list[str]:
    """Human readable description of what is about to happen (for the log)."""
    out = []
    d = sel.drive
    if d:
        out.append(f"Device: {d.display_name()} ({d.path})")
    if sel.boot == BOOT_IMAGE and sel.image:
        out.append(f"Image: {sel.image.path} - {sel.image.describe()}")
        out.append(f"Write mode: {'DD image' if sel.mode == MODE_DD else 'ISO image (file copy)'}")
    else:
        out.append("Boot selection: Non bootable")
    if sel.boot == BOOT_NONE or sel.mode == MODE_ISO:
        out.append(f"Partition scheme: {sel.scheme.upper()}, target system: {TARGET_LABELS.get(sel.target, sel.target)}")
        out.append(f"File system: {FILESYSTEMS[sel.fs].name}, cluster size: {sel.cluster or 'default'}, "
                   f"label: '{sanitize_label(sel.fs, sel.label)}'")
        if will_split_wim(sel, tools):
            out.append("install.wim will be split into install*.swm files (FAT32 4 GB limit)")
        if needs_uefi_ntfs(sel):
            out.append("A UEFI:NTFS partition will be added for UEFI boot")
    if sel.badblocks:
        out.append(f"Bad blocks check: {sel.badblocks} pass(es)")
    return out
