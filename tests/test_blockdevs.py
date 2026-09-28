import pytest

from rufux.core import blockdevs
from rufux.core.fsdefs import cluster_label, cluster_sizes, sanitize_label
from rufux.core.util import GiB, MiB, TiB, human_size


def dev(name, size, tran, rm=False, rota=False, children=None, mountpoints=None, typ="disk",
        model="Model", vendor="Vendor", label=None):
    return {
        "name": name, "kname": name, "path": f"/dev/{name}", "pkname": None, "type": typ,
        "size": size, "model": model, "vendor": vendor, "serial": f"SER-{name}", "tran": tran,
        "rm": rm, "hotplug": rm, "ro": False, "rota": rota, "log-sec": 512, "phy-sec": 512,
        "pttype": "gpt", "fstype": None, "label": label, "uuid": None, "partlabel": None,
        "mountpoints": mountpoints or [None], "maj:min": "8:0", "children": children or [],
    }


def part(name, mountpoints=None, label=None, fstype="vfat", children=None, typ="part"):
    d = dev(name, 1000, None, mountpoints=mountpoints, typ=typ, label=label)
    d["fstype"] = fstype
    d["children"] = children or []
    return d


FAKE = [
    dev("nvme0n1", 1 * TiB, "nvme", children=[
        part("nvme0n1p1", ["/boot"]),
        part("nvme0n1p2", children=[part("luks-root", ["/"], typ="crypt", fstype="btrfs")]),
    ]),
    dev("sda", 2 * TiB, "sata", rota=True, children=[part("sda1", ["/data"])]),
    dev("sdb", 16 * 1000 ** 3, "usb", rm=True, model="Cruzer Blade", vendor="SanDisk",
        children=[part("sdb1", ["/run/media/me/ARCH_202609"], label="ARCH_202609")]),
    dev("sdc", 2 * TiB, "usb", rm=False, rota=True, model="Elements", vendor="WD"),
    dev("sdd", 64 * 1000 ** 3, "usb", rm=False, model="Flash Drive FIT", vendor="Samsung"),
    dev("sde", 32 * 1000 ** 3, "usb", rm=True, children=[part("sde1", ["[SWAP]"], fstype="swap")]),
    dev("mmcblk0", 64 * 1000 ** 3, "mmc", model=None, vendor=None),
    dev("sdf", 8 * 1000 ** 3, "usb", rm=True, children=[
        part("sdf1", children=[part("usbcrypt", None, typ="crypt", fstype="ext4")])]),
]


def test_list_drives(monkeypatch):
    monkeypatch.setattr(blockdevs, "run_lsblk", lambda extra=None: FAKE)
    monkeypatch.setattr(blockdevs, "_active_swaps", lambda: set())
    monkeypatch.setattr(blockdevs, "_sysfs_read", lambda p: "SD" if "mmcblk0" in p else "")
    names = [d.name for d in blockdevs.list_drives()]
    # nvme (system), sata (internal), sdc (USB HDD), sde (swap) are hidden
    assert names == ["sdb", "sdd", "mmcblk0", "sdf"]
    names = [d.name for d in blockdevs.list_drives(show_usb_hdd=True)]
    assert names == ["sdb", "sdc", "sdd", "mmcblk0", "sdf"]
    drives = {d.name: d for d in blockdevs.list_drives(include_hidden=True)}
    assert drives["nvme0n1"].is_system and "/boot" in drives["nvme0n1"].system_reason
    assert drives["sde"].is_system
    assert drives["sdf"].busy_reason
    sdb = drives["sdb"]
    assert sdb.display_name() == "ARCH_202609 (sdb) [16 GB]"
    assert sdb.mountpoints == ["/run/media/me/ARCH_202609"]
    assert drives["sdd"].display_name() == "Samsung Flash Drive FIT (sdd) [64 GB]"
    assert drives["mmcblk0"].display_name() == "SD card (mmcblk0) [64 GB]"


def test_image_on_drive(monkeypatch, tmp_path):
    monkeypatch.setattr(blockdevs, "run_lsblk", lambda extra=None: FAKE)
    monkeypatch.setattr(blockdevs, "_active_swaps", lambda: set())
    monkeypatch.setattr(blockdevs, "disk_of_path", lambda p: None)
    drives = {d.name: d for d in blockdevs.list_drives(include_hidden=True)}
    assert blockdevs.image_on_drive("/run/media/me/ARCH_202609/x.iso", drives["sdb"])
    assert not blockdevs.image_on_drive("/home/me/x.iso", drives["sdb"])


def test_real_lsblk_runs():
    # Just make sure parsing the host's real lsblk output works.
    try:
        blockdevs.list_drives(include_hidden=True)
    except blockdevs.BlockDevError as exc:  # e.g. inside a minimal build chroot
        pytest.skip(str(exc))


def test_cluster_sizes_match_rufus():
    sizes, default = cluster_sizes("fat32", 16 * 1000 ** 3)
    assert default == 8192  # 16 GB stick: 8 KB clusters
    assert sizes == [4096, 8192, 16384, 32768, 65536]  # MS table: 15GB+: 4K - 64K
    assert cluster_sizes("fat32", 8 * 1000 ** 3)[1] == 4096
    assert cluster_sizes("fat32", 32 * 1000 ** 3)[1] == 16384
    sizes, default = cluster_sizes("fat32", 64 * GiB)
    assert default == 32768 and sizes == [16384, 32768, 65536]
    assert cluster_sizes("ntfs", 64 * GiB)[1] == 4096
    assert cluster_sizes("exfat", 64 * GiB)[1] == 128 * 1024
    assert cluster_sizes("exfat", 16 * GiB)[1] == 32 * 1024
    assert cluster_sizes("fat16", 8 * GiB) == ([], 0)
    assert cluster_sizes("fat16", 1 * GiB)[1] == 32768
    assert cluster_sizes("ext4", 8 * GiB) == ([0], 0)
    # 4Kn drive: nothing below the sector size
    sizes, default = cluster_sizes("fat32", 16 * GiB, sector_size=4096)
    assert min(sizes) == 4096 and default == 8192
    assert cluster_sizes("fat32", 16 * MiB) == ([], 0)


def test_labels():
    assert sanitize_label("fat32", "Win11 x64: Pro/Home") == "WIN11 X64_ "[:11].rstrip()
    assert sanitize_label("fat32", "CCCOMA_X64FRE_EN-US_DV9") == "CCCOMA_X64F"
    assert sanitize_label("fat32", "été") == "_T_"
    assert sanitize_label("ntfs", "CCCOMA_X64FRE_EN-US_DV9") == "CCCOMA_X64FRE_EN-US_DV9"
    assert len(sanitize_label("exfat", "A" * 40)) == 15
    assert sanitize_label("ext4", "ünïcødé-label-too-long") .encode().__len__() <= 16
    assert cluster_label(4096, True) == "4096 bytes (Default)"
    assert cluster_label(32768) == "32 kilobytes"


def test_human_size():
    assert human_size(16 * 1000 ** 3, marketing=True) == "16 GB"
    assert human_size(15_931_539_456, marketing=True) == "16 GB"
    assert human_size(7_751_073_792, marketing=True) == "7.8 GB"
    assert human_size(1024 ** 3) == "1.0 GiB"
    assert human_size(512) == "512 B"
