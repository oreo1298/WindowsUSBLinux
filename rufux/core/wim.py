"""Minimal WIM/ESD header + XML metadata parser (used to identify Windows media)."""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

WIM_MAGICS = (b"MSWIM\x00\x00\x00", b"WLPWM\x00\x00\x00")
MAX_XML = 32 * 1024 * 1024

ARCH_NAMES = {0: "x86", 5: "arm", 6: "ia64", 9: "x64", 12: "arm64"}
UNATTEND_ARCH = {"x86": "x86", "x64": "amd64", "arm": "arm", "arm64": "arm64"}


class WimError(Exception):
    pass


@dataclass
class WimImage:
    index: int
    name: str = ""
    display_name: str = ""
    edition: str = ""
    arch: str = ""
    installation_type: str = ""
    major: int = 0
    minor: int = 0
    build: int = 0
    sp_build: int = 0
    languages: list[str] = field(default_factory=list)
    default_language: str = ""


@dataclass
class WimInfo:
    image_count: int
    part_number: int
    total_parts: int
    boot_index: int
    images: list[WimImage]

    @property
    def build(self) -> int:
        return max((i.build for i in self.images), default=0)

    @property
    def arch(self) -> str:
        for i in self.images:
            if i.arch:
                return i.arch
        return ""

    @property
    def is_server(self) -> bool:
        return any("server" in i.installation_type.lower() for i in self.images)

    @property
    def languages(self) -> list[str]:
        out: list[str] = []
        for i in self.images:
            for lang in i.languages:
                if lang not in out:
                    out.append(lang)
        return out

    @property
    def product(self) -> str:
        return windows_product_name(self.build, self.is_server,
                                    self.images[0].major if self.images else 0,
                                    self.images[0].minor if self.images else 0)

    @property
    def setup_index(self) -> int:
        """For boot.wim: the image the PC boots, normally 2 ("Microsoft Windows Setup")."""
        if 1 <= self.boot_index <= self.image_count:
            return self.boot_index
        return 2 if self.image_count >= 2 else 1


def windows_product_name(build: int, server: bool, major: int = 10, minor: int = 0) -> str:
    if server:
        for b, name in ((26100, "Windows Server 2025"), (20348, "Windows Server 2022"),
                        (17763, "Windows Server 2019"), (14393, "Windows Server 2016"),
                        (9600, "Windows Server 2012 R2"), (9200, "Windows Server 2012"),
                        (7600, "Windows Server 2008 R2")):
            if build >= b:
                return name
        return "Windows Server"
    if build >= 22000:
        return "Windows 11"
    if build >= 10240:
        return "Windows 10"
    if build >= 9600:
        return "Windows 8.1"
    if build >= 9200:
        return "Windows 8"
    if build >= 7600:
        return "Windows 7"
    if build >= 6000:
        return "Windows Vista"
    return "Windows" if build == 0 else f"Windows (build {build})"


def parse_header(hdr: bytes) -> dict:
    if len(hdr) < 208 or hdr[:8] not in WIM_MAGICS:
        raise WimError("Not a WIM file")
    xml_size = struct.unpack_from("<Q", hdr, 72)[0] & 0x00FFFFFFFFFFFFFF
    xml_flags = hdr[79]
    xml_offset = struct.unpack_from("<Q", hdr, 80)[0]
    return {
        "header_size": struct.unpack_from("<I", hdr, 8)[0],
        "version": struct.unpack_from("<I", hdr, 12)[0],
        "flags": struct.unpack_from("<I", hdr, 16)[0],
        "part_number": struct.unpack_from("<H", hdr, 40)[0],
        "total_parts": struct.unpack_from("<H", hdr, 42)[0],
        "image_count": struct.unpack_from("<I", hdr, 44)[0],
        "xml_size": xml_size,
        "xml_flags": xml_flags,
        "xml_offset": xml_offset,
        "boot_index": struct.unpack_from("<I", hdr, 120)[0],
    }


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _int(el: ET.Element | None, path: str) -> int:
    try:
        return int(_text(el, path, "0"), 0)
    except ValueError:
        return 0


def parse_xml(raw: bytes) -> list[WimImage]:
    if raw.startswith(b"\xff\xfe"):
        text = raw[2:].decode("utf-16-le", "replace")
    elif raw.startswith(b"\xfe\xff"):
        text = raw[2:].decode("utf-16-be", "replace")
    else:
        try:
            text = raw.decode("utf-16-le")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
    text = text.rstrip("\x00")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise WimError(f"Invalid WIM XML: {exc}") from exc
    images: list[WimImage] = []
    for img in root.findall("IMAGE"):
        try:
            index = int(img.get("INDEX", "0"))
        except ValueError:
            index = 0
        win = img.find("WINDOWS")
        arch_num = _int(win, "ARCH")
        wi = WimImage(
            index=index,
            name=_text(img, "NAME"),
            display_name=_text(img, "DISPLAYNAME"),
            edition=_text(win, "EDITIONID"),
            arch=ARCH_NAMES.get(arch_num, "") if win is not None and win.find("ARCH") is not None else "",
            installation_type=_text(win, "INSTALLATIONTYPE"),
            major=_int(win, "VERSION/MAJOR"),
            minor=_int(win, "VERSION/MINOR"),
            build=_int(win, "VERSION/BUILD"),
            sp_build=_int(win, "VERSION/SPBUILD"),
            default_language=_text(win, "LANGUAGES/DEFAULT"),
        )
        if win is not None:
            wi.languages = [lang.text.strip() for lang in win.findall("LANGUAGES/LANGUAGE") if lang.text]
        images.append(wi)
    images.sort(key=lambda i: i.index)
    return images


def read_wim_info(read: Callable[[int, int], bytes], file_size: int | None = None) -> WimInfo:
    """Parse WIM metadata using `read(offset, length)` to access the file."""
    hdr = parse_header(read(0, 208))
    size = hdr["xml_size"]
    if size <= 0 or size > MAX_XML:
        raise WimError("WIM XML data missing or too large")
    if file_size is not None and hdr["xml_offset"] + size > file_size:
        raise WimError("WIM XML data out of range (split or truncated WIM?)")
    if hdr["xml_flags"] & 0x04:  # compressed resource: not expected for XML
        raise WimError("Compressed WIM XML data is not supported")
    images = parse_xml(read(hdr["xml_offset"], size))
    return WimInfo(hdr["image_count"], hdr["part_number"], hdr["total_parts"],
                   hdr["boot_index"], images)


def read_wim_file(path: str) -> WimInfo:
    import os

    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size

        def read(off: int, length: int) -> bytes:
            f.seek(off)
            return f.read(length)

        return read_wim_info(read, size)
