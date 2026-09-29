"""End-to-end tests of the privileged helper against loop devices.

These need root, loop devices with partition scanning and a few tools; they are
skipped automatically elsewhere.  File systems the running kernel cannot mount
are mounted through FUSE drivers (fusefat, ntfs-3g) when available.
"""

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import time

import pytest

from conftest import ROOT, have, needs, run
from rufux import HELPER_PROTOCOL
from rufux.helper import formatting, isomode
from rufux.helper import main as helper_main
from rufux.helper.context import Cancelled, Context, Emitter, HelperError

MiB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not os.path.exists("/dev/loop-control") or not have("losetup", "sfdisk"),
    reason="needs root and loop devices")


def kernel_fs(name: str) -> bool:
    with open("/proc/filesystems") as f:
        return any(line.split()[-1] == name for line in f if line.strip())


@pytest.fixture(autouse=True)
def allow_loop(monkeypatch):
    monkeypatch.setenv("RUFUX_ALLOW_LOOP", "1")
    saved = dict(formatting.MOUNT_OVERRIDES)
    if not kernel_fs("vfat") and have("fusefat"):
        for fs in ("fat16", "fat32"):
            formatting.MOUNT_OVERRIDES[fs] = lambda dev, mp, ro: ["fusefat", "-o", "ro" if ro else "rw+", dev, mp]
    if not kernel_fs("exfat") and have("mount.exfat-fuse"):
        formatting.MOUNT_OVERRIDES["exfat"] = lambda dev, mp, ro: (
            ["mount.exfat-fuse"] + (["-o", "ro"] if ro else []) + [dev, mp])
    yield
    formatting.MOUNT_OVERRIDES.clear()
    formatting.MOUNT_OVERRIDES.update(saved)


class Loop:
    def __init__(self, path: str, backing: str, size: int):
        self.path = path
        self.backing = backing
        self.size = size

    def part(self, n: int) -> str:
        return f"{self.path}p{n}"

    def spec(self) -> dict:
        return {"path": self.path, "size": self.size}


@pytest.fixture
def loop(tmp_path):
    loops = []

    def make(size: int = 256 * MiB) -> Loop:
        backing = tmp_path / f"usb{len(loops)}.img"
        with open(backing, "wb") as f:
            f.truncate(size)
        dev = run("losetup", "--find", "--show", "--partscan", str(backing)).stdout.decode().strip()
        lp = Loop(dev, str(backing), size)
        loops.append(lp)
        return lp

    yield make
    for lp in loops:
        with open("/proc/self/mounts") as f:
            mps = [line.split()[1] for line in f if line.split()[0].startswith(lp.path)]
        for mp in mps:
            subprocess.run(["umount", "-l", mp], capture_output=True)
        subprocess.run(["losetup", "-d", lp.path], capture_output=True)


def run_job(job: dict) -> tuple[bool, str, list[dict]]:
    stream = io.StringIO()
    ctx = Context(Emitter(stream))
    job = {"protocol": HELPER_PROTOCOL, "action": "write", **job}
    try:
        msg = helper_main.do_write(ctx, job)
        ok = True
    except (HelperError, Cancelled) as exc:
        ok, msg = False, str(exc)
    finally:
        ctx.run_cleanups()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    return ok, msg, events


def logs(events) -> str:
    return "\n".join(e.get("msg", "") for e in events if e["t"] in ("log", "status"))


def blkid(dev: str) -> dict:
    if have("udevadm"):
        subprocess.run(["udevadm", "settle"], capture_output=True)
    out = subprocess.run(["blkid", "-p", "-o", "export", dev], capture_output=True, text=True).stdout
    info = {}
    for line in out.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            info[key] = shlex.split(val)[0] if val else ""
    return info


def sfdisk_json(dev: str) -> dict:
    return json.loads(run("sfdisk", "--json", dev).stdout)["partitiontable"]


@pytest.fixture
def fuse_mount(tmp_path):
    mounted = []

    def mount(dev: str, fs: str) -> str:
        mp = tmp_path / f"m{len(mounted)}"
        mp.mkdir()
        if fs == "fat32" and not kernel_fs("vfat"):
            run("fusefat", "-o", "ro", dev, str(mp))
        elif fs == "ntfs" and not kernel_fs("ntfs3"):
            run("ntfs-3g", "-o", "ro", dev, str(mp))
        else:
            run("mount", "-o", "ro", dev, str(mp))
        mounted.append(str(mp))
        return str(mp)

    yield mount
    for mp in mounted:
        subprocess.run(["umount", mp], capture_output=True)


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


def test_refuses_system_disk():
    root_dev = None
    with open("/proc/self/mounts") as f:
        for line in f:
            dev, mp = line.split()[:2]
            if mp == "/" and dev.startswith("/dev/"):
                root_dev = dev
    if root_dev is None:
        pytest.skip("no block device root")
    size = int(open(f"/sys/class/block/{os.path.basename(root_dev)}/size").read()) * 512
    ok, msg, _ = run_job({"mode": "format", "device": {"path": root_dev, "size": size},
                          "filesystem": "ext4", "scheme": "gpt"})
    assert not ok and "Refusing" in msg


def test_refuses_changed_size(loop):
    lp = loop(64 * MiB)
    ok, msg, _ = run_job({"mode": "format", "device": {"path": lp.path, "size": lp.size + 512},
                          "filesystem": "ext4", "scheme": "gpt"})
    assert not ok and "size" in msg


def test_refuses_image_on_target(loop, tmp_path):
    lp = loop(64 * MiB)
    ok, msg, _ = run_job({"mode": "format", "device": lp.spec(), "filesystem": "ext4", "scheme": "mbr",
                          "label": "HOSTS"})
    assert ok, msg
    mp = tmp_path / "mnt"
    mp.mkdir()
    run("mount", lp.part(1), str(mp))
    try:
        img = mp / "x.img"
        img.write_bytes(b"\x00" * 4096)
        ok, msg, _ = run_job({"mode": "dd", "device": lp.spec(), "image": str(img)})
        assert not ok and "stored on the drive" in msg
    finally:
        run("umount", str(mp))


# ---------------------------------------------------------------------------
# DD mode
# ---------------------------------------------------------------------------


def test_dd_raw_and_verify(loop, tmp_path):
    lp = loop(64 * MiB)
    img = tmp_path / "disk.img"
    data = os.urandom(10 * MiB + 1234)  # deliberately not sector aligned
    img.write_bytes(data)
    ok, msg, events = run_job({"mode": "dd", "device": lp.spec(), "image": str(img), "verify": True})
    assert ok, msg + logs(events)
    assert "Verification successful" in logs(events)
    with open(lp.backing, "rb") as f:
        assert f.read(len(data)) == data
    phases = {e.get("phase") for e in events if e["t"] == "progress"}
    assert {"write", "verify"} <= phases


@needs("xz", "zstd")
@pytest.mark.parametrize("comp", ["xz", "zst", "gz", "zip"])
def test_dd_compressed(loop, tmp_path, comp):
    lp = loop(64 * MiB)
    raw = tmp_path / "disk.img"
    data = os.urandom(3 * MiB) + b"\x00" * (5 * MiB)
    raw.write_bytes(data)
    if comp == "xz":
        run("xz", "-k", str(raw))
        img = tmp_path / "disk.img.xz"
    elif comp == "zst":
        run("zstd", "-q", str(raw), "-o", str(tmp_path / "disk.img.zst"))
        img = tmp_path / "disk.img.zst"
    elif comp == "gz":
        run("gzip", "-k", str(raw))
        img = tmp_path / "disk.img.gz"
    else:
        import zipfile

        img = tmp_path / "disk.zip"
        with zipfile.ZipFile(img, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(raw, "disk.img")
    ok, msg, events = run_job({"mode": "dd", "device": lp.spec(), "image": str(img),
                               "compression": comp, "verify": True})
    assert ok, msg + logs(events)
    with open(lp.backing, "rb") as f:
        assert f.read(len(data)) == data


def test_dd_image_too_large(loop, tmp_path):
    lp = loop(16 * MiB)
    img = tmp_path / "big.img"
    with open(img, "wb") as f:
        f.truncate(32 * MiB)
    ok, msg, _ = run_job({"mode": "dd", "device": lp.spec(), "image": str(img)})
    assert not ok and "larger than the drive" in msg


def test_helper_subprocess_protocol(loop, tmp_path):
    lp = loop(32 * MiB)
    img = tmp_path / "disk.img"
    img.write_bytes(os.urandom(2 * MiB))
    job = {"protocol": HELPER_PROTOCOL, "action": "write", "mode": "dd", "device": lp.spec(),
           "image": str(img), "verify": True}
    env = dict(os.environ, RUFUX_ALLOW_LOOP="1")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "rufux-helper")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    out, _ = proc.communicate(json.dumps(job).encode() + b"\n", timeout=120)
    events = [json.loads(line) for line in out.decode().splitlines()]
    assert events[0]["t"] == "hello"
    assert events[-1]["t"] == "done" and events[-1]["ok"], events[-1]
    assert proc.returncode == 0


def test_helper_cancel(loop, tmp_path):
    lp = loop(256 * MiB)
    img = tmp_path / "disk.img"
    with open(img, "wb") as f:
        f.truncate(200 * MiB)
    job = {"protocol": HELPER_PROTOCOL, "action": "write", "mode": "dd", "device": lp.spec(),
           "image": str(img), "verify": True, "badblocks": 4}
    env = dict(os.environ, RUFUX_ALLOW_LOOP="1")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "rufux-helper")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    proc.stdin.write(json.dumps(job).encode() + b"\n")
    proc.stdin.flush()
    time.sleep(1.0)
    proc.stdin.write(b"cancel\n")
    proc.stdin.flush()
    out, _ = proc.communicate(timeout=120)
    events = [json.loads(line) for line in out.decode().splitlines()]
    assert events[-1]["t"] == "done" and events[-1]["cancelled"]
    assert proc.returncode == 3


# ---------------------------------------------------------------------------
# Format (non bootable)
# ---------------------------------------------------------------------------


def test_format_ext4_gpt(loop):
    lp = loop(128 * MiB)
    ok, msg, events = run_job({"mode": "format", "device": lp.spec(), "filesystem": "ext4",
                               "scheme": "gpt", "label": "My Data"})
    assert ok, msg + logs(events)
    pt = sfdisk_json(lp.path)
    assert pt["label"] == "gpt"
    assert len(pt["partitions"]) == 1 and pt["partitions"][0]["start"] == 2048
    info = blkid(lp.part(1))
    assert info["TYPE"] == "ext4" and info["LABEL"] == "My Data"


@needs("mkfs.fat")
def test_format_fat32_mbr_full(loop):
    lp = loop(96 * MiB)
    ok, msg, events = run_job({"mode": "format", "device": lp.spec(), "filesystem": "fat32",
                               "scheme": "mbr", "label": "usb stick", "quick": False,
                               "extended_label": False, "cluster": 4096})
    assert ok, msg + logs(events)
    pt = sfdisk_json(lp.path)
    assert pt["label"] == "dos" and pt["partitions"][0]["type"] == "c"
    assert not pt["partitions"][0].get("bootable")
    info = blkid(lp.part(1))
    assert info["TYPE"] == "vfat" and info["LABEL"] == "USB STICK"
    assert any(e.get("phase") == "zero" for e in events if e["t"] == "progress")


@needs("mkfs.ntfs", "ntfs-3g")
def test_format_ntfs_with_extended_label(loop, fuse_mount):
    lp = loop(96 * MiB)
    ok, msg, events = run_job({"mode": "format", "device": lp.spec(), "filesystem": "ntfs",
                               "scheme": "mbr", "label": "Backup Drive", "extended_label": True})
    assert ok, msg + logs(events)
    assert blkid(lp.part(1))["TYPE"] == "ntfs"
    mp = fuse_mount(lp.part(1), "ntfs")
    text = open(os.path.join(mp, "autorun.inf"), "rb").read().decode("utf-16")
    assert "label = Backup Drive" in text
    assert os.path.getsize(os.path.join(mp, "autorun.ico")) > 0


# ---------------------------------------------------------------------------
# ISO mode
# ---------------------------------------------------------------------------


def make_windows_iso(tmp_path, wim_mib=24):
    from conftest import make_boot_wim, make_fake_pe, make_fake_wim

    src = tmp_path / "winsrc"
    for d in ("efi/boot", "efi/microsoft/boot", "sources", "boot", "support"):
        (src / d).mkdir(parents=True, exist_ok=True)
    (src / "bootmgr").write_bytes(os.urandom(4000))
    (src / "bootmgr.efi").write_bytes(b"MZ" + os.urandom(4000))
    (src / "efi" / "boot" / "bootx64.efi").write_bytes(b"MZ" + os.urandom(8000))
    (src / "efi" / "microsoft" / "boot" / "bcd").write_bytes(b"regf" + os.urandom(1000))
    if have("wimlib-imagex"):
        make_boot_wim(str(src / "sources" / "boot.wim"), str(tmp_path))
    else:
        (src / "sources" / "boot.wim").write_bytes(os.urandom(300000))
    (src / "sources" / "appraiserres.dll").write_bytes(os.urandom(5000))
    (src / "setup.exe").write_bytes(make_fake_pe(0x8664))
    (src / "autorun.inf").write_text("[AutoRun.Amd64]\nopen=setup.exe\n")
    wim_src = tmp_path / "wimdata"
    wim_src.mkdir()
    (wim_src / "Windows").mkdir()
    for i in range(3):
        (wim_src / "Windows" / f"blob{i}.bin").write_bytes(os.urandom(wim_mib * MiB // 3))
    if have("wimlib-imagex"):
        run("wimlib-imagex", "capture", str(wim_src), str(src / "sources" / "install.wim"),
            "Windows 11 Pro", "--compress=none")
    else:
        make_fake_wim(str(src / "sources" / "install.wim"))
    iso = tmp_path / "win.iso"
    run("genisoimage", "-quiet", "-udf", "-iso-level", "3", "-V", "CCCOMA_X64FRE_EN-US_DV9",
        "-o", str(iso), str(src))
    return iso, src


UNATTEND = ('<?xml version="1.0" encoding="utf-8"?>\n<unattend xmlns="urn:schemas-microsoft-com:unattend">'
            '<settings pass="windowsPE"></settings></unattend>\n')


def fake_setup_wrapper(tmp_path, monkeypatch) -> tuple[str, bytes]:
    """A stand-in for Rufus' setup_x64.exe, accepted by the helper's checksum check."""
    from conftest import make_fake_pe
    from rufux.core import rufusfiles

    data = make_fake_pe(0x8664, 3000)
    path = tmp_path / "setup_x64.exe"
    path.write_bytes(data)
    monkeypatch.setitem(rufusfiles.SETUP_WRAPPERS, "x64", rufusfiles.RufusFile(
        "setup_x64.exe", "setup/setup_x64.exe", hashlib.sha256(data).hexdigest(), len(data)))
    return str(path), data


def assert_upgrade_ready(mp: str, src, wrapper: bytes | None) -> None:
    """What makes a drive usable for an in-place upgrade (running setup.exe from Windows)."""
    from conftest import wim_listing

    assert "autounattend.xml" not in {n.lower() for n in os.listdir(mp)}
    boot_wim = os.path.join(mp, "sources", "boot.wim")
    assert "/Autounattend.xml" in wim_listing(boot_wim, 2)
    assert "/Autounattend.xml" not in wim_listing(boot_wim, 1)
    xml = run("wimlib-imagex", "extract", boot_wim, "2", "/Autounattend.xml", "--to-stdout").stdout
    assert xml.decode() == UNATTEND
    if wrapper is not None:
        assert open(os.path.join(mp, "setup.exe"), "rb").read() == wrapper
        assert open(os.path.join(mp, "setup.dll"), "rb").read() == (src / "setup.exe").read_bytes()


@needs("genisoimage", "wimlib-imagex", "mkfs.fat", "fusefat")
def test_iso_windows_fat32_split(loop, tmp_path, fuse_mount, monkeypatch):
    monkeypatch.setattr(isomode, "SPLIT_SIZE_MIB", 10)
    iso, src = make_windows_iso(tmp_path)
    wrapper_path, wrapper = fake_setup_wrapper(tmp_path, monkeypatch)
    lp = loop(256 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "fat32",
           "scheme": "gpt", "label": "CCCOMA_X64FRE_EN-US_DV9", "verify": True,
           "extended_label": True,
           "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "split_wim": "sources/install.wim",
                   "unattend": UNATTEND, "unattend_target": "bootwim", "boot_wim": "sources/boot.wim",
                   "bypass_appraiser": True, "setup_wrapper": wrapper_path}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    text = logs(events)
    assert "Verification successful" in text
    assert "successfully verified" in text or "Verifying the split" in text
    assert "Added 'Autounattend.xml' to image 2 of 'sources/boot.wim'" in text
    pt = sfdisk_json(lp.path)
    assert pt["label"] == "gpt"
    assert pt["partitions"][0]["type"].upper() == "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
    assert pt["partitions"][0]["name"] == "Main Data Partition"
    assert blkid(lp.part(1))["LABEL"] == "CCCOMA_X64F"
    mp = fuse_mount(lp.part(1), "fat32")
    names = {n.lower() for n in os.listdir(os.path.join(mp, "sources"))}
    assert "install.wim" not in names
    assert {"install.swm", "install2.swm", "install3.swm"} <= names
    assert os.path.getsize(os.path.join(mp, "sources", "appraiserres.dll")) == 0
    assert "appraiserres.bak" in names
    assert_upgrade_ready(mp, src, wrapper)
    # the image's own autorun.inf must be kept
    assert "setup.exe" in open(os.path.join(mp, "autorun.inf")).read()
    with open(os.path.join(mp, "efi", "boot", "bootx64.efi"), "rb") as f:
        assert f.read() == (src / "efi" / "boot" / "bootx64.efi").read_bytes()


def fake_uefi_ntfs(tmp_path) -> str:
    path = tmp_path / "uefi-ntfs.img"
    with open(path, "wb") as f:
        f.truncate(MiB)
    run("mkfs.fat", "-F", "12", "-n", "UEFI_NTFS", str(path))
    return str(path)


@needs("genisoimage", "mkfs.ntfs", "ntfs-3g", "mkfs.fat")
def test_iso_windows_ntfs_uefi_ntfs(loop, tmp_path, fuse_mount):
    iso, src = make_windows_iso(tmp_path, wim_mib=12)
    uefi = fake_uefi_ntfs(tmp_path)
    lp = loop(256 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "ntfs",
           "scheme": "mbr", "label": "CCCOMA_X64FRE_EN-US_DV9", "verify": True, "uefi_ntfs": uefi,
           "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9",
                   "unattend": UNATTEND, "unattend_target": "oem"}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    pt = sfdisk_json(lp.path)
    parts = pt["partitions"]
    assert len(parts) == 2
    assert parts[0]["type"] == "7" and parts[0].get("bootable")
    assert parts[1]["type"] == "ef" and parts[1]["size"] == 2048
    assert parts[1]["start"] + parts[1]["size"] <= lp.size // 512
    with open(lp.backing, "rb") as f:
        f.seek(parts[1]["start"] * 512)
        assert f.read(MiB) == open(uefi, "rb").read()
    info = blkid(lp.part(1))
    assert info["TYPE"] == "ntfs" and info["LABEL"] == "CCCOMA_X64FRE_EN-US_DV9"
    mp = fuse_mount(lp.part(1), "ntfs")
    assert os.path.getsize(os.path.join(mp, "sources", "install.wim")) == \
        (src / "sources" / "install.wim").stat().st_size
    assert os.path.exists(os.path.join(mp, "sources", "$OEM$", "$$", "Panther", "unattend.xml"))
    assert not os.path.exists(os.path.join(mp, "autounattend.xml"))
    with open(os.path.join(mp, "sources", "boot.wim"), "rb") as f:
        assert f.read() == (src / "sources" / "boot.wim").read_bytes()  # left alone


@needs("genisoimage", "wimlib-imagex", "mkfs.ntfs", "ntfs-3g", "mkfs.fat")
def test_iso_windows_ntfs_answer_file_in_boot_wim(loop, tmp_path, fuse_mount, monkeypatch):
    iso, src = make_windows_iso(tmp_path, wim_mib=6)
    wrapper_path, wrapper = fake_setup_wrapper(tmp_path, monkeypatch)
    lp = loop(128 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "ntfs",
           "scheme": "gpt", "label": "WIN11", "verify": True, "uefi_ntfs": fake_uefi_ntfs(tmp_path),
           "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "unattend": UNATTEND,
                   "unattend_target": "bootwim", "boot_wim": "sources/boot.wim",
                   "bypass_appraiser": True, "setup_wrapper": wrapper_path}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    assert "Verification successful" in logs(events)
    assert_upgrade_ready(fuse_mount(lp.part(1), "ntfs"), src, wrapper)


@needs("genisoimage", "mkfs.fat")
def test_rejects_bad_windows_options_before_writing(loop, tmp_path):
    iso, _ = make_windows_iso(tmp_path, wim_mib=3)
    lp = loop(64 * MiB)
    bogus = tmp_path / "setup_x64.exe"
    bogus.write_bytes(b"MZ" + os.urandom(3000))
    base = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "fat32",
            "scheme": "gpt", "label": "WIN11"}
    for iso_opts, error in (
            ({"unattend": UNATTEND, "unattend_target": "root"}, "unattend_target"),
            ({"unattend": UNATTEND, "unattend_target": "bootwim", "boot_wim": "sources/nope.wim"},
             "not found in the image"),
            ({"setup_wrapper": str(bogus)}, "checksum mismatch")):
        if iso_opts.get("unattend_target") == "bootwim" and not have("wimlib-imagex"):
            continue
        ok, msg, _ = run_job({**base, "iso": {"iso_label": "X", **iso_opts}})
        assert not ok and error in msg
    with open(lp.backing, "rb") as f:
        assert f.read(MiB) == bytes(MiB)  # the drive was not touched


@needs("genisoimage", "grub-install", "mkfs.fat")
def test_iso_windows_bios_grub(loop, tmp_path, fuse_mount):
    if not os.path.isdir("/usr/lib/grub/i386-pc"):
        pytest.skip("no i386-pc GRUB")
    # grub-install must be able to map the mount back to its device, which the
    # fusefat driver does not allow: use NTFS (ntfs-3g) when vfat is unavailable.
    fs = "fat32" if kernel_fs("vfat") else "ntfs"
    if fs == "ntfs" and not have("mkfs.ntfs", "ntfs-3g"):
        pytest.skip("needs vfat or ntfs-3g")
    iso, _ = make_windows_iso(tmp_path, wim_mib=3)
    uefi = fake_uefi_ntfs(tmp_path)
    lp = loop(128 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": fs,
           "scheme": "mbr", "label": "WIN11", "verify": False,
           "uefi_ntfs": uefi if fs == "ntfs" else None,
           "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "bios_grub": True}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    with open(lp.backing, "rb") as f:
        mbr = f.read(512)
    assert mbr[510:512] == b"\x55\xaa" and b"GRUB" in mbr
    parts = sfdisk_json(lp.path)["partitions"]
    assert parts[0].get("bootable")
    mp = fuse_mount(lp.part(1), fs)
    cfg = open(os.path.join(mp, "boot", "grub", "grub.cfg")).read()
    assert "ntldr /bootmgr" in cfg
    assert os.path.exists(os.path.join(mp, "boot", "grub", "i386-pc", "ntldr.mod"))


def make_linux_iso(tmp_path, casper=False):
    src = tmp_path / "linuxsrc"
    (src / "EFI" / "BOOT").mkdir(parents=True)
    (src / "boot" / "grub").mkdir(parents=True)
    (src / "loader" / "entries").mkdir(parents=True)
    (src / "arch" / "boot" / "x86_64").mkdir(parents=True)
    (src / "EFI" / "BOOT" / "BOOTx64.EFI").write_bytes(b"MZ" + os.urandom(5000))
    (src / "arch" / "boot" / "x86_64" / "vmlinuz-linux").write_bytes(os.urandom(200000))
    (src / "boot" / "grub" / "grub.cfg").write_text(
        "search --no-floppy --set=root --label ARCH_TEST_LABEL_2026\n"
        "menuentry 'Arch' {\n"
        "  linux /arch/boot/x86_64/vmlinuz-linux archisobasedir=arch archisolabel=ARCH_TEST_LABEL_2026\n"
        "}\n")
    (src / "loader" / "entries" / "01-archiso.conf").write_text(
        "title Arch\nlinux /arch/boot/x86_64/vmlinuz-linux\n"
        "options archisobasedir=arch archisosearchuuid=2026-09-01-10-00-00-00\n")
    if casper:
        (src / "casper").mkdir()
        (src / "casper" / "vmlinuz").write_bytes(os.urandom(1000))
        (src / "boot" / "grub" / "grub.cfg").write_text(
            "menuentry 'Try Ubuntu' {\n  linux /casper/vmlinuz boot=casper quiet splash ---\n}\n")
    os.symlink("vmlinuz-linux", src / "arch" / "boot" / "x86_64" / "vmlinuz")
    iso = tmp_path / "linux.iso"
    run("xorriso", "-as", "mkisofs", "-R", "-J", "-V", "ARCH_TEST_LABEL_2026", "-o", str(iso), str(src))
    return iso


@needs("xorriso", "mkfs.fat", "fusefat")
def test_iso_linux_fat32_label_patching(loop, tmp_path, fuse_mount):
    iso = make_linux_iso(tmp_path)
    lp = loop(128 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "fat32",
           "scheme": "mbr", "label": "ARCH_TEST_LABEL_2026", "verify": True,
           "iso": {"iso_label": "ARCH_TEST_LABEL_2026", "patch_labels": True}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    assert blkid(lp.part(1))["LABEL"] == "ARCH_TEST_L"
    mp = fuse_mount(lp.part(1), "fat32")
    grub = open(os.path.join(mp, "boot", "grub", "grub.cfg")).read()
    assert "archisolabel=ARCH_TEST_L\n" in grub and "--label ARCH_TEST_L\n" in grub
    entry = open(os.path.join(mp, "loader", "entries", "01-archiso.conf")).read()
    assert "archisolabel=ARCH_TEST_L" in entry and "archisosearchuuid" not in entry
    # symlink dereferenced on FAT
    assert os.path.getsize(os.path.join(mp, "arch", "boot", "x86_64", "vmlinuz")) == 200000


@needs("xorriso", "mkfs.fat", "fusefat", "mkfs.ext3")
def test_iso_linux_persistence(loop, tmp_path, fuse_mount):
    iso = make_linux_iso(tmp_path, casper=True)
    lp = loop(192 * MiB)
    job = {"mode": "iso", "device": lp.spec(), "image": str(iso), "filesystem": "fat32",
           "scheme": "mbr", "label": "UBUNTU", "verify": False, "persistence_size": 64 * MiB,
           "iso": {"iso_label": "ARCH_TEST_LABEL_2026", "persistence": "casper"}}
    ok, msg, events = run_job(job)
    assert ok, msg + "\n" + logs(events)
    parts = sfdisk_json(lp.path)["partitions"]
    assert len(parts) == 2 and parts[1]["type"] == "83"
    assert blkid(lp.part(2))["LABEL"] == "casper-rw"
    mp = fuse_mount(lp.part(1), "fat32")
    grub = open(os.path.join(mp, "boot", "grub", "grub.cfg")).read()
    assert "boot=casper persistent quiet" in grub


def test_patch_config_text():
    text = ("linux /casper/vmlinuz file=/cdrom/preseed/ubuntu.seed maybe-ubiquity quiet\n"
            "append initrd=/live/initrd.img boot=live components\n"
            "  options root=live:CDLABEL=Fedora\\x20Live\\x2040 rd.live.image\n"
            "set timeout=5\n")
    out = isomode.patch_config_text(text, "Fedora Live 40", "FEDORA", "casper")
    assert "persistent file=/cdrom/preseed/ubuntu.seed quiet" in out
    assert "CDLABEL=FEDORA rd.live.image" in out
    assert "set timeout=5" in out
    out = isomode.patch_config_text(text, "", "X", "live")
    assert "boot=live persistence components" in out


# ---------------------------------------------------------------------------
# bad blocks / save
# ---------------------------------------------------------------------------


def test_badblocks_clean(loop):
    lp = loop(24 * MiB)
    ok, msg, events = run_job({"mode": "format", "device": lp.spec(), "filesystem": "ext4",
                               "scheme": "mbr", "badblocks": 2})
    assert ok, msg + logs(events)
    assert "no bad blocks found" in logs(events)


def test_badblocks_detects_fake_capacity(tmp_path, monkeypatch):
    from rufux.helper import badblocks, rawio

    path = tmp_path / "fake.bin"
    size = 8 * MiB
    with open(path, "wb") as f:
        f.truncate(size)
    real_pwrite = rawio.pwrite_all
    real_pread = rawio.pread_into
    half = size // 2
    monkeypatch.setattr(rawio, "pwrite_all", lambda fd, d, off: real_pwrite(fd, d, off % half))
    monkeypatch.setattr(rawio, "pread_into", lambda fd, b, off: real_pread(fd, b, off % half))
    ctx = Context(Emitter(io.StringIO()))
    with pytest.raises(HelperError, match="COUNTERFEIT"):
        badblocks.run(ctx, str(path), size, 1)


def test_save_drive(loop, tmp_path):
    lp = loop(16 * MiB)
    payload = os.urandom(lp.size)
    with open(lp.path, "wb") as f:
        f.write(payload)
    out = tmp_path / "saved.img"
    stream = io.StringIO()
    ctx = Context(Emitter(stream))
    try:
        msg = helper_main.do_save(ctx, {"device": lp.spec(), "output": str(out)})
    finally:
        ctx.run_cleanups()
    assert "saved" in msg
    assert out.read_bytes() == payload
