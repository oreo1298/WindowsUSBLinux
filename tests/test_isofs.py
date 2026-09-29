import hashlib
import os
import shutil

import pytest

from conftest import make_fake_pe, make_fake_wim, needs, run
from rufux.core import image as image_mod
from rufux.core.isofs import ImageFS
from rufux.core.wim import read_wim_file


def tree_files(src):
    out = {}
    for dirpath, dirnames, filenames in os.walk(src):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, src)
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def iso_files(path):
    out = {}
    with ImageFS(path) as img:
        for e in img.walk():
            if e.is_dir or e.is_symlink:
                continue
            out[e.path] = hashlib.sha256(img.read(e)).hexdigest()
        kind = img.kind
        label = img.label
    return out, kind, label


@needs("xorriso")
def test_rockridge_joliet_xorriso(src_tree, tmp_path):
    os.symlink("README.TXT", src_tree / "link")
    iso = tmp_path / "rrj.iso"
    run("xorriso", "-as", "mkisofs", "-R", "-J", "-V", "TEST_LABEL", "-o", str(iso), str(src_tree))
    files, kind, label = iso_files(iso)
    assert kind == "iso9660+rockridge"
    assert label == "TEST_LABEL"
    assert files == tree_files(src_tree)
    with ImageFS(iso) as img:
        link = img.lookup("link")
        assert link is not None and link.symlink == "README.TXT"
        assert img.read("readme.txt") == b"hello\n"  # case-insensitive lookup


@needs("genisoimage")
def test_rockridge_relocation_genisoimage(src_tree, tmp_path):
    iso = tmp_path / "rr.iso"
    run("genisoimage", "-quiet", "-input-charset", "utf-8", "-R", "-V", "TEST_LABEL",
        "-o", str(iso), str(src_tree))
    files, kind, _ = iso_files(iso)
    assert kind == "iso9660+rockridge"
    assert files == tree_files(src_tree)
    with ImageFS(iso) as img:
        names = [e.name for e in img.listdir("")]
        assert "rr_moved" not in names


@needs("genisoimage")
def test_joliet_only(src_tree, tmp_path):
    iso = tmp_path / "joliet.iso"
    run("genisoimage", "-quiet", "-input-charset", "utf-8", "-J", "-joliet-long", "-D",
        "-V", "TEST_LABEL", "-o", str(iso), str(src_tree))
    files, kind, _ = iso_files(iso)
    assert kind == "iso9660+joliet"
    assert files == tree_files(src_tree)


@needs("genisoimage")
def test_udf_bridge(src_tree, tmp_path):
    iso = tmp_path / "udf.iso"
    run("genisoimage", "-quiet", "-input-charset", "utf-8", "-udf", "-D", "-V", "TEST_LABEL",
        "-o", str(iso), str(src_tree))
    files, kind, label = iso_files(iso)
    assert kind == "udf"
    assert label == "TEST_LABEL"
    assert files == tree_files(src_tree)


@needs("genisoimage")
def test_plain_iso9660_lowercases(src_tree, tmp_path):
    iso = tmp_path / "plain.iso"
    run("genisoimage", "-quiet", "-input-charset", "utf-8", "-iso-level", "3", "-D",
        "-V", "TEST_LABEL", "-o", str(iso), str(src_tree))
    files, kind, _ = iso_files(iso)
    assert kind == "iso9660"
    assert "efi/boot/bootx64.efi" in files
    assert files["efi/boot/bootx64.efi"] == tree_files(src_tree)["EFI/BOOT/BOOTX64.EFI"]


@needs("xorriso")
def test_eltorito_and_hybrid(tmp_path):
    src = tmp_path / "src"
    (src / "isolinux").mkdir(parents=True)
    (src / "EFI" / "BOOT").mkdir(parents=True)
    (src / "isolinux" / "isolinux.bin").write_bytes(b"\x00" * 4096)
    (src / "EFI" / "BOOT" / "BOOTX64.EFI").write_bytes(b"MZ" + b"\x00" * 4094)
    efi_img = src / "efiboot.img"
    efi_img.write_bytes(b"\x00" * (1024 * 1024))
    iso = tmp_path / "hybrid.iso"
    args = ["xorriso", "-as", "mkisofs", "-R", "-J", "-V", "ARCH_202609",
            "-b", "isolinux/isolinux.bin", "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
            "-eltorito-alt-boot", "-e", "efiboot.img", "-no-emul-boot",
            "-isohybrid-gpt-basdat", "-o", str(iso), str(src)]
    mbr = "/usr/lib/ISOLINUX/isohdpfx.bin"
    if os.path.exists(mbr):
        args[3:3] = ["-isohybrid-mbr", mbr]
    run(*args)
    with ImageFS(iso) as img:
        boot = img.boot_entries()
    assert any(b.platform == 0 and b.bootable for b in boot)
    assert any(b.is_efi for b in boot)
    info = image_mod.analyze(str(iso))
    assert info.kind == "iso"
    assert info.label == "ARCH_202609"
    assert info.has_eltorito_efi
    assert info.efi_bootable and info.efi_archs == ["x64"]
    assert info.has_syslinux
    if os.path.exists(mbr):
        assert info.is_hybrid
        assert info.part_table in ("mbr", "gpt")
        assert info.recommended_mode == "dd"
        assert info.can_dd_mode and info.can_iso_mode
    assert info.distro == "Arch Linux"


@needs("genisoimage")
def test_windows_detection(tmp_path):
    src = tmp_path / "win"
    for d in ("efi/boot", "efi/microsoft/boot", "sources", "boot"):
        (src / d).mkdir(parents=True, exist_ok=True)
    (src / "bootmgr").write_bytes(b"\x00" * 1000)
    (src / "bootmgr.efi").write_bytes(b"MZ" + b"\x00" * 1000)
    (src / "efi" / "boot" / "bootx64.efi").write_bytes(b"MZ" + b"\x00" * 1000)
    (src / "efi" / "microsoft" / "boot" / "bcd").write_bytes(b"regf" + b"\x00" * 1000)
    (src / "sources" / "boot.wim").write_bytes(b"\x00" * 5000)
    (src / "setup.exe").write_bytes(make_fake_pe(0xAA64))
    make_fake_wim(str(src / "sources" / "install.wim"), build=26100, arch=9, images=3, pad=10000)
    iso = tmp_path / "win.iso"
    run("genisoimage", "-quiet", "-udf", "-V", "CCCOMA_X64FRE_EN-US_DV9", "-o", str(iso), str(src))
    info = image_mod.analyze(str(iso))
    assert info.kind == "iso"
    assert info.fs_kind == "udf"
    assert info.is_windows and info.has_bootmgr
    assert info.windows is not None
    assert info.windows.product == "Windows 11"
    assert info.windows.build == 26100
    assert info.windows.arch == "x64"
    assert info.windows.wim_images == 3
    assert info.windows.is_win11 and info.windows.supports_wue
    assert info.windows.languages == ["en-US"]
    assert info.windows.boot_wim == "sources/boot.wim"
    assert info.windows.setup_arch == "arm64"
    assert info.recommended_mode == "iso"
    assert not info.can_dd_mode
    assert info.label == "CCCOMA_X64FRE_EN-US_DV9"
    assert info.describe().startswith("Windows 11")


def test_wim_parser(tmp_path):
    p = tmp_path / "install.wim"
    make_fake_wim(str(p), build=19045, arch=9, images=1)
    w = read_wim_file(str(p))
    assert w.build == 19045
    assert w.product == "Windows 10"
    assert w.arch == "x64"
    make_fake_wim(str(p), build=26100, arch=12, installation_type="Server")
    w = read_wim_file(str(p))
    assert w.is_server and w.product == "Windows Server 2025" and w.arch == "arm64"


def test_boot_wim_setup_index():
    from rufux.core.wim import WimInfo

    assert WimInfo(2, 1, 1, 2, []).setup_index == 2   # Microsoft media: boot index 2
    assert WimInfo(2, 1, 1, 0, []).setup_index == 2   # no boot index: Rufus uses 2
    assert WimInfo(1, 1, 1, 0, []).setup_index == 1   # single-image boot.wim
    assert WimInfo(3, 1, 1, 3, []).setup_index == 3
    assert WimInfo(2, 1, 1, 7, []).setup_index == 2   # invalid boot index


def _make_disk_image(path, size=8 * 1024 * 1024):
    with open(path, "wb") as f:
        f.truncate(size)
    run("sfdisk", "-q", str(path), input=b"label: dos\nstart=2048, type=c, bootable\n")


@needs("sfdisk")
def test_raw_disk_image(tmp_path):
    img = tmp_path / "disk.img"
    _make_disk_image(img)
    info = image_mod.analyze(str(img))
    assert info.kind == "disk"
    assert info.part_table == "mbr"
    assert info.recommended_mode == "dd"
    assert info.can_dd_mode and not info.can_iso_mode


@needs("sfdisk", "xz", "gzip", "bzip2", "zstd")
@pytest.mark.parametrize("comp", ["xz", "gz", "bz2", "zst", "zip"])
def test_compressed_images(tmp_path, comp):
    img = tmp_path / "disk.img"
    _make_disk_image(img)
    size = img.stat().st_size
    if comp == "xz":
        run("xz", "-k", "-T1", str(img))
        out = tmp_path / "disk.img.xz"
    elif comp == "gz":
        run("gzip", "-k", str(img))
        out = tmp_path / "disk.img.gz"
    elif comp == "bz2":
        run("bzip2", "-k", str(img))
        out = tmp_path / "disk.img.bz2"
    elif comp == "zst":
        run("zstd", "-q", str(img), "-o", str(tmp_path / "disk.img.zst"))
        out = tmp_path / "disk.img.zst"
    else:
        import zipfile

        out = tmp_path / "disk.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(img, "disk.img")
    info = image_mod.analyze(str(out))
    assert info.compression == comp
    assert info.kind == "disk"
    assert info.part_table == "mbr"
    if comp in ("xz", "zst", "zip"):
        assert info.uncompressed_size == size
    assert info.recommended_mode == "dd"
    stream = image_mod.open_decompressed(str(out), comp)
    try:
        data = b""
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        stream.close()
    assert data == img.read_bytes()


@pytest.mark.skipif(not os.environ.get("RUFUX_SLOW_TESTS"), reason="set RUFUX_SLOW_TESTS=1")
@needs("xorriso")
def test_multi_extent_large_file(tmp_path):
    src = tmp_path / "big"
    src.mkdir()
    big = src / "install.wim"
    size = 4 * 1024 ** 3 + 12345
    with open(big, "wb") as f:
        f.truncate(size)
        f.seek(size - 5)
        f.write(b"TAIL!")
    iso = tmp_path / "big.iso"
    run("xorriso", "-as", "mkisofs", "-iso-level", "3", "-R", "-J", "-o", str(iso), str(src))
    with ImageFS(iso) as img:
        e = img.lookup("install.wim")
        assert e is not None and e.size == size
        assert len(e.extents) >= 2
        assert img.read_range(e, size - 5, 5) == b"TAIL!"
    info = image_mod.analyze(str(iso))
    assert info.big_files == ["install.wim"]
    shutil.rmtree(src)
