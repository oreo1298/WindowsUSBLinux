import pytest

from rufux.core import plan
from rufux.core.blockdevs import Drive
from rufux.core.image import ImageInfo, WindowsInfo
from rufux.core.plan import PlanError, Selection, Tools
from rufux.core.unattend import WueOptions
from rufux.core.util import GiB

ALL_FS = {fs: True for fs in ("fat16", "fat32", "ntfs", "udf", "exfat", "ext2", "ext3", "ext4")}


def tools(wimlib=True, grub=True, fs=None):
    return Tools(wimlib=wimlib, grub_bios=grub, fs=dict(fs or ALL_FS))


def drive(size=32 * 1000 ** 3):
    return Drive(name="sdz", path="/dev/sdz", size=size, model="Stick", serial="123", tran="usb",
                 removable=True)


def win11(big=True, esd=False, build=26100):
    wim = "sources/install.esd" if esd else "sources/install.wim"
    info = ImageInfo(path="/tmp/Win11.iso", name="Win11.iso", size=6 * GiB, kind="iso",
                     label="CCCOMA_X64FRE_EN-US_DV9", fs_kind="udf", efi_bootable=True,
                     efi_archs=["x64"], has_bootmgr=True, is_windows=True, file_count=1000,
                     total_bytes=6 * GiB)
    info.windows = WindowsInfo(product="Windows 11", build=build, arch="x64", wim_path=wim,
                               wim_size=5 * GiB if big else 3 * GiB, boot_wim="sources/boot.wim",
                               setup_arch="x64")
    if big:
        info.big_files = [wim]
        info.largest_file, info.largest_size = wim, 5 * GiB
    return info


def arch_iso():
    return ImageInfo(path="/tmp/archlinux.iso", name="archlinux.iso", size=GiB, kind="iso",
                     label="ARCH_202609", part_table="mbr", is_hybrid=True, bios_bootable=True,
                     efi_bootable=True, efi_archs=["x64"], has_eltorito_efi=True, file_count=50,
                     total_bytes=GiB, distro="Arch Linux")


def test_windows_defaults_split():
    sel = Selection(drive=drive(), image=win11())
    t = tools()
    sel.mode = plan.modes_for(sel.image)[0]
    assert plan.modes_for(sel.image) == ["iso"]
    assert plan.default_scheme(sel) == "gpt"
    sel.scheme = "gpt"
    assert plan.targets_for(sel, t) == ["uefi"]
    assert plan.default_filesystem(sel, t, sel.drive.size) == "fat32"
    sel.fs = "fat32"
    assert plan.will_split_wim(sel, t)
    sel.label = plan.default_label(sel)
    plan.validate(sel, t)
    job = plan.build_job(sel, t)
    assert job["mode"] == "iso" and job["filesystem"] == "fat32"
    assert job["label"] == "CCCOMA_X64F"
    assert job["iso"]["split_wim"] == "sources/install.wim"
    assert "uefi_ntfs" not in job


def test_windows_without_wimlib_falls_back_to_ntfs():
    sel = Selection(drive=drive(), image=win11(), mode="iso", scheme="gpt")
    t = tools(wimlib=False)
    assert plan.default_filesystem(sel, t, sel.drive.size) == "ntfs"
    sel.fs = "fat32"
    with pytest.raises(PlanError, match="wimlib"):
        plan.validate(sel, t)
    sel.fs = "ntfs"
    with pytest.raises(PlanError, match="UEFI:NTFS"):
        plan.validate(sel, t)
    sel.uefi_ntfs = "/usr/share/rufux/uefi-ntfs.img"
    plan.validate(sel, t)
    job = plan.build_job(sel, t)
    assert job["uefi_ntfs"] == sel.uefi_ntfs and "split_wim" not in job["iso"]


def test_esd_cannot_be_split():
    sel = Selection(drive=drive(), image=win11(esd=True), mode="iso", scheme="gpt", fs="fat32")
    assert not plan.splittable_wim(sel.image)
    assert plan.default_filesystem(sel, tools(), sel.drive.size) == "ntfs"
    with pytest.raises(PlanError, match="larger than 4 GB"):
        plan.validate(sel, tools())


def test_windows_mbr_bios_with_grub_and_wue():
    sel = Selection(drive=drive(), image=win11(big=False), mode="iso", scheme="mbr", fs="fat32")
    t = tools()
    assert plan.targets_for(sel, t) == ["bios+uefi", "uefi"]
    assert plan.targets_for(sel, tools(grub=False)) == ["uefi"]
    sel.target = "bios+uefi"
    sel.wue = WueOptions(bypass_requirements=True, local_account="me")
    job = plan.build_job(sel, t)
    assert job["iso"]["bios_grub"] is True
    assert job["iso"]["unattend_target"] == "bootwim"
    assert job["iso"]["boot_wim"] == "sources/boot.wim"
    assert job["iso"]["bypass_appraiser"] is True
    assert "BypassTPMCheck" in job["iso"]["unattend"]


def test_requirement_bypass_needs_wimlib_and_boot_wim():
    sel = Selection(drive=drive(), image=win11(), mode="iso", scheme="gpt", fs="ntfs",
                    uefi_ntfs="/usr/share/rufux/uefi-ntfs.img",
                    wue=WueOptions(bypass_requirements=True))
    plan.validate(sel, tools())
    with pytest.raises(PlanError, match="wimlib"):
        plan.validate(sel, tools(wimlib=False))
    # Options that only apply after installation go to $OEM$ and need nothing extra.
    sel.wue = WueOptions(no_online_account=True)
    plan.validate(sel, tools(wimlib=False))
    assert plan.build_job(sel, tools(wimlib=False))["iso"]["unattend_target"] == "oem"
    sel.wue = WueOptions(bypass_requirements=True)
    sel.image.windows.boot_wim = ""
    with pytest.raises(PlanError, match="boot.wim"):
        plan.validate(sel, tools())


def test_setup_wrapper_for_24h2_upgrades():
    sel = Selection(drive=drive(), image=win11(), mode="iso", scheme="gpt", fs="fat32",
                    wue=WueOptions(bypass_requirements=True))
    assert plan.setup_wrapper_arch(sel) == "x64"
    assert "setup_wrapper" not in plan.build_job(sel, tools())["iso"]  # not located (yet)
    sel.setup_wrapper = "/usr/share/rufux/setup_x64.exe"
    assert plan.build_job(sel, tools())["iso"]["setup_wrapper"] == sel.setup_wrapper
    assert any("setup.dll" in line for line in plan.summary(sel, tools()))
    # Not without the bypass, not before 24H2, and not for a setup.exe we have no wrapper for.
    sel.wue = WueOptions(no_online_account=True)
    assert plan.setup_wrapper_arch(sel) is None
    assert "setup_wrapper" not in plan.build_job(sel, tools())["iso"]
    sel.wue = WueOptions(bypass_requirements=True)
    sel.image = win11(build=22631)
    assert plan.setup_wrapper_arch(sel) is None
    sel.image = win11()
    sel.image.windows.setup_arch = "x86"
    assert plan.setup_wrapper_arch(sel) is None
    sel.image.windows.setup_arch = "arm64"
    assert plan.setup_wrapper_arch(sel) == "arm64"


def test_hybrid_linux_prefers_dd():
    sel = Selection(drive=drive(), image=arch_iso())
    assert plan.modes_for(sel.image) == ["dd", "iso"]
    sel.mode = "dd"
    assert plan.filesystems_for(sel, tools(), sel.drive.size) == []
    assert plan.schemes_for(sel) == ["mbr"]
    assert plan.targets_for(sel, tools()) == ["bios+uefi"]
    plan.validate(sel, tools())
    job = plan.build_job(sel, tools())
    assert job == {"protocol": job["protocol"], "action": "write", "device": job["device"],
                   "verify": True, "badblocks": 0, "eject": False, "mode": "dd",
                   "image": "/tmp/archlinux.iso", "compression": None}
    sel.mode = "iso"
    sel.scheme = "mbr"
    assert plan.targets_for(sel, tools()) == ["uefi"]
    sel.fs = "fat32"
    warnings = plan.validate(sel, tools())
    assert any("DD mode" in w for w in warnings)


def test_image_too_big_for_drive():
    info = arch_iso()
    info.size = 40 * GiB
    sel = Selection(drive=drive(8 * 1000 ** 3), image=info, mode="dd")
    with pytest.raises(PlanError, match="too big"):
        plan.validate(sel, tools())


def test_non_bootable_defaults():
    sel = Selection(drive=drive(64 * 1000 ** 3), boot=plan.BOOT_NONE)
    t = tools()
    assert plan.default_scheme(sel) == "mbr"
    assert plan.default_filesystem(sel, t, sel.drive.size) == "exfat"
    sel.drive = drive(16 * 1000 ** 3)
    assert plan.default_filesystem(sel, t, sel.drive.size) == "fat32"
    assert "fat16" not in plan.filesystems_for(sel, t, sel.drive.size)
    sel.fs = "ext4"
    sel.label = "Data"
    job = plan.build_job(sel, t)
    assert job["mode"] == "format" and "image" not in job and job["label"] == "Data"
    t.fs["ext4"] = False
    with pytest.raises(PlanError, match="e2fsprogs"):
        plan.validate(sel, t)


def test_persistence_job():
    info = arch_iso()
    info.persistence = "casper"
    sel = Selection(drive=drive(), image=info, mode="iso", scheme="mbr", fs="fat32",
                    persistence_size=4 * GiB)
    job = plan.build_job(sel, tools())
    assert job["iso"]["persistence"] == "casper" and job["persistence_size"] == 4 * GiB
    assert plan.max_persistence(sel) > 20 * GiB
