"""Image analysis: work out what an image is and how it can be written.

The result (`ImageInfo`) drives the defaults in the GUI, the same way Rufus'
"image report" does: partition scheme, target system, file system, label and
whether ISO (file copy) and/or DD (raw) mode are possible.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field

from .isofs import ImageFS, IsoError
from .wim import WimError, WimInfo, read_wim_info

FAT32_MAX_FILE = 0xFFFFFFFF  # 4 GiB - 1

IMAGE_EXTENSIONS = (
    "iso", "img", "raw", "bin", "dd", "ima", "vhd", "hdd",
    "gz", "xz", "bz2", "zst", "zstd", "lzma", "zip",
)

COMPRESSED_EXTENSIONS = {
    ".gz": "gz", ".xz": "xz", ".bz2": "bz2", ".zst": "zst", ".zstd": "zst",
    ".lzma": "lzma", ".zip": "zip",
}

EFI_ARCH_FILES = {
    "bootx64.efi": "x64",
    "bootia32.efi": "ia32",
    "bootaa64.efi": "arm64",
    "bootarm.efi": "arm",
    "bootriscv64.efi": "riscv64",
    "bootloongarch64.efi": "loongarch64",
}

PE_MACHINES = {0x8664: "x64", 0xAA64: "arm64", 0x014C: "x86", 0x01C4: "arm"}


def pe_arch(data: bytes) -> str:
    """Architecture of a Windows executable, from the start of the file ('' if unknown)."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return ""
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if off + 6 > len(data) or data[off:off + 4] != b"PE\x00\x00":
        return ""
    return PE_MACHINES.get(struct.unpack_from("<H", data, off + 4)[0], "")


@dataclass
class WindowsInfo:
    product: str = "Windows"
    build: int = 0
    arch: str = ""
    editions: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    is_server: bool = False
    wim_path: str = ""
    wim_size: int = 0
    wim_is_esd: bool = False
    wim_images: int = 0
    boot_wim: str = ""    # path of sources/boot.wim in the image ('' if missing)
    setup_arch: str = ""  # architecture of the setup.exe at the root of the image

    @property
    def is_win11(self) -> bool:
        return self.build >= 22000 and not self.is_server

    @property
    def supports_wue(self) -> bool:
        """Whether the Rufus "Windows User Experience" customisations apply."""
        return self.build >= 10240 and not self.is_server


@dataclass
class ImageInfo:
    path: str
    name: str
    size: int = 0
    kind: str = "unknown"  # iso | disk | fsimage | unknown
    compression: str | None = None
    inner_name: str | None = None
    uncompressed_size: int | None = None
    label: str = ""
    fs_kind: str = ""
    part_table: str | None = None  # mbr | gpt | None (from the first sectors)
    is_hybrid: bool = False
    bios_bootable: bool = False
    efi_bootable: bool = False
    efi_archs: list[str] = field(default_factory=list)
    has_eltorito_efi: bool = False
    has_bootmgr: bool = False
    has_bootmgr_efi: bool = False
    is_windows: bool = False
    windows: WindowsInfo | None = None
    has_syslinux: bool = False
    has_grub: bool = False
    has_systemd_boot: bool = False
    distro: str = ""
    persistence: str | None = None  # "casper" | "live" | None
    file_count: int = 0
    dir_count: int = 0
    total_bytes: int = 0
    largest_file: str = ""
    largest_size: int = 0
    big_files: list[str] = field(default_factory=list)  # files > FAT32 limit
    config_files: list[str] = field(default_factory=list)
    has_symlinks: bool = False
    errors: list[str] = field(default_factory=list)

    # -- derived -----------------------------------------------------------

    @property
    def is_iso(self) -> bool:
        return self.kind == "iso" and self.compression is None

    @property
    def data_size(self) -> int:
        """Number of bytes that a raw (DD) write will put on the drive."""
        if self.compression:
            return self.uncompressed_size or 0
        return self.size

    @property
    def has_4gb_file(self) -> bool:
        return bool(self.big_files)

    @property
    def can_iso_mode(self) -> bool:
        return self.is_iso and self.file_count > 0

    @property
    def can_dd_mode(self) -> bool:
        if self.kind == "iso" and self.compression is None:
            # Non-hybrid ISOs (e.g. Windows) cannot boot when written raw.
            return self.is_hybrid
        return self.kind != "unknown" or self.size > 0

    @property
    def recommended_mode(self) -> str:
        if not self.is_iso:
            return "dd"
        if self.is_windows:
            return "iso"
        if self.is_hybrid:
            return "dd"
        return "iso"

    @property
    def only_splittable_big_files(self) -> bool:
        """True if every >4GB file is a Windows install image we can split."""
        if not self.big_files or not self.windows or not self.windows.wim_path:
            return False
        return all(p.lower() == self.windows.wim_path.lower() for p in self.big_files)

    def describe(self) -> str:
        if self.windows is not None:
            w = self.windows
            desc = w.product
            if w.build:
                desc += f" (build {w.build})"
            if w.arch:
                desc += f" {w.arch}"
            return desc
        if self.is_windows:
            return "Windows boot media"
        if self.distro:
            return self.distro
        if self.kind == "iso":
            return "ISO image"
        if self.kind == "disk":
            return "Disk image"
        if self.kind == "fsimage":
            return "File system image"
        return "Unknown image"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def detect_compression(path: str, head: bytes) -> str | None:
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"\x1f\x8b"):
        return "gz"
    if head.startswith(b"BZh") and len(head) > 3 and head[3:4].isdigit():
        return "bz2"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zst"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lzma" and head[:1] == b"\x5d":
        return "lzma"
    return None


def detect_partition_table(head: bytes) -> str | None:
    """Detect an MBR or GPT partition table in the first sectors of a disk."""
    if len(head) >= 520 and head[512:520] == b"EFI PART":
        return "gpt"
    if len(head) >= 4104 and head[4096:4104] == b"EFI PART":
        return "gpt"
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return None
    # A FAT/NTFS boot sector also ends with 55 AA: rule those out.
    if head[3:11] == b"NTFS    " or head[3:11] == b"EXFAT   ":
        return None
    if head[54:59] == b"FAT12" or head[54:59] == b"FAT16" or head[82:87] == b"FAT32":
        return None
    entries = [head[446 + i * 16:446 + (i + 1) * 16] for i in range(4)]
    if any(e[0] not in (0x00, 0x80) for e in entries):
        return None
    used = [e for e in entries if e[4] != 0]
    if not used:
        return None
    for e in used:
        start, count = struct.unpack_from("<II", e, 8)
        if count == 0:
            return None
    if any(e[4] == 0xEE for e in used):
        return "gpt"
    return "mbr"


def detect_fs_image(head: bytes) -> bool:
    if len(head) < 512:
        return False
    if head[3:11] in (b"NTFS    ", b"EXFAT   "):
        return True
    if head[54:59] in (b"FAT12", b"FAT16") or head[82:87] == b"FAT32":
        return True
    if len(head) >= 1080 and head[1080:1082] == b"\x53\xef":  # ext2/3/4 magic
        return True
    return False


def xz_uncompressed_size(path: str) -> int | None:
    """Sum the uncompressed sizes stored in the xz index(es)."""

    def read_vli(buf: bytes, pos: int) -> tuple[int, int]:
        value = 0
        shift = 0
        while True:
            b = buf[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value, pos
            shift += 7
            if shift > 63:
                raise ValueError("bad vli")

    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            total = 0
            while end > 12:
                f.seek(end - 12)
                footer = f.read(12)
                if footer[10:12] != b"YZ":
                    # skip stream padding (multiple of 4 null bytes)
                    if footer[8:12] == b"\x00\x00\x00\x00":
                        end -= 4
                        continue
                    return None
                backward = (struct.unpack_from("<I", footer, 4)[0] + 1) * 4
                f.seek(end - 12 - backward)
                index = f.read(backward)
                if not index or index[0] != 0:
                    return None
                count, pos = read_vli(index, 1)
                unpadded_total = 0
                for _ in range(count):
                    unpadded, pos = read_vli(index, pos)
                    usize, pos = read_vli(index, pos)
                    total += usize
                    unpadded_total += (unpadded + 3) & ~3
                # stream = header(12) + blocks + index + footer(12)
                end = end - 12 - backward - unpadded_total - 12
                if end < 0:
                    return None
            return total
    except (OSError, ValueError, IndexError, struct.error):
        return None


def zstd_content_size(head: bytes) -> int | None:
    if len(head) < 6 or head[:4] != b"\x28\xb5\x2f\xfd":
        return None
    fhd = head[4]
    fcs_flag = fhd >> 6
    single_segment = (fhd >> 5) & 1
    did_flag = fhd & 3
    pos = 5
    if not single_segment:
        pos += 1
    pos += (0, 1, 2, 4)[did_flag]
    if fcs_flag == 0:
        if not single_segment:
            return None
        return head[pos] if len(head) > pos else None
    size = (0, 2, 4, 8)[fcs_flag]
    if len(head) < pos + size:
        return None
    value = int.from_bytes(head[pos:pos + size], "little")
    if fcs_flag == 1:
        value += 256
    return value


def open_decompressed(path: str, compression: str):
    """Return a readable binary stream of the decompressed data (or a Popen)."""
    if compression == "gz":
        return gzip.open(path, "rb")
    if compression in ("xz", "lzma"):
        return lzma.open(path, "rb")
    if compression == "bz2":
        return bz2.open(path, "rb")
    if compression == "zip":
        zf = zipfile.ZipFile(path)
        member = largest_zip_member(zf)
        return zf.open(member)
    if compression == "zst":
        try:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+

            return zstd.open(path, "rb")
        except ImportError:
            pass
        try:
            import zstandard  # type: ignore[import-not-found]

            fh = open(path, "rb")
            return zstandard.ZstdDecompressor().stream_reader(fh, closefd=True)
        except ImportError:
            pass
        exe = shutil.which("zstd")
        if exe is None:
            raise OSError("zstd support requires the 'zstd' program")
        proc = subprocess.Popen([exe, "-dc", "--no-progress", "-q", path], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        return _PopenReader(proc)
    raise ValueError(f"Unknown compression {compression}")


class _PopenReader:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def read(self, n: int = -1) -> bytes:
        assert self.proc.stdout is not None
        return self.proc.stdout.read(n)

    def readinto(self, buf) -> int:
        assert self.proc.stdout is not None
        return self.proc.stdout.readinto(buf)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def largest_zip_member(zf: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [m for m in zf.infolist() if not m.is_dir()]
    if not members:
        raise ValueError("Empty zip archive")
    return max(members, key=lambda m: m.file_size)


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def analyze(path: str) -> ImageInfo:
    path = os.path.abspath(path)
    info = ImageInfo(path=path, name=os.path.basename(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        info.errors.append(str(exc))
        return info
    info.size = st.st_size
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError as exc:
        info.errors.append(str(exc))
        return info

    info.compression = detect_compression(path, head)
    if info.compression:
        _analyze_compressed(info, head)
        return info

    info.part_table = detect_partition_table(head)
    is_iso_fs = len(head) >= 32774 and head[32769:32774] in (b"CD001", b"BEA01")
    if is_iso_fs:
        info.kind = "iso"
        info.is_hybrid = info.part_table is not None
        _analyze_iso(info)
    elif info.part_table:
        info.kind = "disk"
    elif detect_fs_image(head):
        info.kind = "fsimage"
    elif path.lower().endswith((".iso",)):
        # Maybe a pure UDF image without ISO 9660 descriptors at 32K.
        try:
            with ImageFS(path):
                info.kind = "iso"
            _analyze_iso(info)
        except IsoError:
            info.kind = "unknown"
    else:
        info.kind = "unknown"
    return info


def _analyze_compressed(info: ImageInfo, head: bytes) -> None:
    comp = info.compression
    assert comp is not None
    if comp == "zip":
        try:
            with zipfile.ZipFile(info.path) as zf:
                member = largest_zip_member(zf)
                info.inner_name = member.filename
                info.uncompressed_size = member.file_size
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            info.errors.append(f"zip: {exc}")
            return
    elif comp == "xz":
        info.uncompressed_size = xz_uncompressed_size(info.path)
    elif comp == "zst":
        info.uncompressed_size = zstd_content_size(head)
    if info.inner_name is None:
        base = info.name
        for ext in COMPRESSED_EXTENSIONS:
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        info.inner_name = base
    try:
        stream = open_decompressed(info.path, comp)
        try:
            inner = b""
            while len(inner) < 40 * 1024:
                chunk = stream.read(40 * 1024 - len(inner))
                if not chunk:
                    break
                inner += chunk
        finally:
            stream.close()
    except Exception as exc:  # noqa: BLE001 - any decoder error is reported
        info.errors.append(f"Could not decompress: {exc}")
        info.kind = "unknown"
        return
    info.part_table = detect_partition_table(inner)
    if len(inner) >= 32774 and inner[32769:32774] == b"CD001":
        info.kind = "iso"
        info.is_hybrid = info.part_table is not None
    elif info.part_table:
        info.kind = "disk"
    elif detect_fs_image(inner):
        info.kind = "fsimage"
    else:
        info.kind = "disk"  # assume a raw disk image


def _analyze_iso(info: ImageInfo) -> None:
    try:
        img = ImageFS(info.path)
    except IsoError as exc:
        info.errors.append(str(exc))
        return
    with img:
        info.errors.extend(img.errors)
        info.label = img.label
        info.fs_kind = img.kind
        boot = img.boot_entries()
        info.has_eltorito_efi = any(b.is_efi for b in boot)
        info.bios_bootable = any(b.platform == 0 and b.bootable for b in boot) or info.is_hybrid
        paths: dict[str, int] = {}
        dirs: set[str] = set()
        try:
            for e in img.walk():
                low = e.path.lower()
                if e.is_dir:
                    info.dir_count += 1
                    dirs.add(low)
                    continue
                if e.is_symlink:
                    info.has_symlinks = True
                    continue
                info.file_count += 1
                info.total_bytes += e.size
                paths[low] = e.size
                if e.size > info.largest_size:
                    info.largest_size, info.largest_file = e.size, e.path
                if e.size > FAT32_MAX_FILE:
                    info.big_files.append(e.path)
                if low.endswith((".cfg", ".conf")) and e.size < 1024 * 1024:
                    info.config_files.append(e.path)
        except (IsoError, struct.error) as exc:
            info.errors.append(f"Could not read the whole image: {exc}")

        # Boot loaders
        for p in paths:
            base = p.rsplit("/", 1)[-1]
            if p.startswith("efi/boot/") and base in EFI_ARCH_FILES:
                arch = EFI_ARCH_FILES[base]
                if arch not in info.efi_archs:
                    info.efi_archs.append(arch)
        info.efi_bootable = bool(info.efi_archs)
        info.has_bootmgr = "bootmgr" in paths
        info.has_bootmgr_efi = "bootmgr.efi" in paths or "efi/microsoft/boot/bcd" in paths
        info.has_syslinux = any(
            p.endswith(("isolinux.bin", "syslinux.cfg", "isolinux.cfg", "ldlinux.c32", "ldlinux.sys"))
            for p in paths)
        info.has_grub = any(p.endswith(("grub.cfg",)) or "/grub/" in p or p.startswith("boot/grub")
                            for p in paths)
        info.has_systemd_boot = "loader/loader.conf" in paths or any(
            p.startswith("loader/entries/") for p in paths)

        # Windows
        wim_path = None
        for candidate in ("sources/install.wim", "sources/install.esd"):
            if candidate in paths:
                wim_path = candidate
                break
        info.is_windows = info.has_bootmgr or (
            "sources/boot.wim" in paths and ("efi/microsoft/boot/bcd" in paths or info.has_bootmgr_efi))
        if wim_path is not None:
            info.is_windows = True
            entry = img.lookup(wim_path)
            wi = WindowsInfo(wim_path=entry.path if entry else wim_path, wim_size=paths[wim_path],
                             wim_is_esd=wim_path.endswith(".esd"))
            if entry is not None:
                try:
                    w: WimInfo = read_wim_info(lambda off, n: img.read_range(entry, off, n), entry.size)
                    wi.build = w.build
                    wi.arch = w.arch
                    wi.is_server = w.is_server
                    wi.product = w.product
                    wi.languages = w.languages
                    wi.wim_images = len(w.images)
                    wi.editions = [i.display_name or i.name for i in w.images]
                except (WimError, IsoError, struct.error) as exc:
                    info.errors.append(f"WIM: {exc}")
            if not wi.arch:
                wi.arch = {"x64": "x64", "arm64": "arm64", "ia32": "x86"}.get(
                    info.efi_archs[0] if info.efi_archs else "", "")
            info.windows = wi
        elif "sources/install.swm" in paths:
            info.is_windows = True
            info.windows = WindowsInfo(product="Windows (split image)", wim_path="sources/install.swm",
                                       wim_size=paths["sources/install.swm"])
        if info.windows is not None:
            _windows_setup_files(img, paths, info.windows)

        info.distro = _guess_distro(img, info, paths, dirs)
        if "casper" in dirs and not info.is_windows:
            info.persistence = "casper"
        elif "live" in dirs and not info.is_windows and any(
                p.startswith("live/") and ("vmlinuz" in p or p.endswith(".squashfs")) for p in paths):
            info.persistence = "live"


def _windows_setup_files(img: ImageFS, paths: dict[str, int], wi: WindowsInfo) -> None:
    """Locate boot.wim (where the answer file goes) and identify setup.exe's architecture."""
    if "sources/boot.wim" in paths:
        entry = img.lookup("sources/boot.wim")
        wi.boot_wim = entry.path if entry else "sources/boot.wim"
    if "setup.exe" in paths:
        try:
            wi.setup_arch = pe_arch(img.read("setup.exe", 4096))
        except (IsoError, struct.error):
            wi.setup_arch = ""


def _guess_distro(img: ImageFS, info: ImageInfo, paths: dict[str, int], dirs: set[str]) -> str:
    if info.is_windows:
        return ""
    for disk_info in (".disk/info", ".disk/info.txt"):
        if disk_info in paths and paths[disk_info] < 4096:
            try:
                text = img.read(disk_info, 4096).decode("utf-8", "replace").strip()
                if text:
                    return text.splitlines()[0][:80]
            except IsoError:
                pass
    label = info.label.upper()
    checks = [
        ("CACHYOS", "CachyOS"), ("ARCH_", "Arch Linux"), ("EOS_", "EndeavourOS"),
        ("MANJARO", "Manjaro"), ("FEDORA", "Fedora"), ("UBUNTU", "Ubuntu"),
        ("DEBIAN", "Debian"), ("OPENSUSE", "openSUSE"), ("MINT", "Linux Mint"),
        ("POP_OS", "Pop!_OS"), ("GARUDA", "Garuda Linux"), ("NIXOS", "NixOS"),
        ("TAILS", "Tails"), ("KALI", "Kali Linux"), ("ALPINE", "Alpine Linux"),
        ("RHEL", "Red Hat Enterprise Linux"), ("ROCKY", "Rocky Linux"),
        ("ALMA", "AlmaLinux"), ("ARTIX", "Artix Linux"), ("VOID", "Void Linux"),
    ]
    for key, name in checks:
        if key in label:
            return name
    if "arch" in dirs and ("arch/boot" in dirs or any(p.startswith("arch/x86_64") for p in paths)):
        return "Arch Linux (or derivative)"
    if "liveos" in dirs:
        return "Fedora-based live image"
    if "casper" in dirs:
        return "Ubuntu-based live image"
    if "live" in dirs:
        return "Debian-based live image"
    if info.has_grub or info.has_syslinux or info.has_systemd_boot:
        return "Linux / bootable ISO"
    return ""
