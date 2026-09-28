import os
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


def needs(*tools: str):
    return pytest.mark.skipif(not have(*tools), reason=f"requires {', '.join(tools)}")


def run(*args, **kw):
    return subprocess.run(list(args), check=True, capture_output=True, **kw)


def make_fake_wim(path: str, build: int = 26100, arch: int = 9, images: int = 2,
                  installation_type: str = "Client", pad: int = 0) -> None:
    """Write a minimal WIM file with a valid header and XML metadata."""
    imgs = []
    for i in range(1, images + 1):
        imgs.append(
            f'<IMAGE INDEX="{i}"><NAME>Windows 11 Edition {i}</NAME>'
            f"<DISPLAYNAME>Windows 11 Pro {i}</DISPLAYNAME>"
            f"<WINDOWS><ARCH>{arch}</ARCH><EDITIONID>Professional</EDITIONID>"
            f"<INSTALLATIONTYPE>{installation_type}</INSTALLATIONTYPE>"
            "<LANGUAGES><LANGUAGE>en-US</LANGUAGE><DEFAULT>en-US</DEFAULT></LANGUAGES>"
            f"<VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>{build}</BUILD><SPBUILD>1</SPBUILD>"
            "</VERSION></WINDOWS></IMAGE>"
        )
    xml = ("<WIM><TOTALBYTES>1234</TOTALBYTES>" + "".join(imgs) + "</WIM>").encode("utf-16-le")
    xml = b"\xff\xfe" + xml
    hdr = bytearray(208)
    hdr[0:8] = b"MSWIM\x00\x00\x00"
    struct.pack_into("<I", hdr, 8, 208)
    struct.pack_into("<I", hdr, 12, 0x10D00)
    struct.pack_into("<HH", hdr, 40, 1, 1)
    struct.pack_into("<I", hdr, 44, images)
    xml_offset = 208 + pad
    struct.pack_into("<Q", hdr, 72, len(xml))  # size (7 bytes) + flags (0)
    struct.pack_into("<Q", hdr, 80, xml_offset)
    struct.pack_into("<Q", hdr, 88, len(xml))
    with open(path, "wb") as f:
        f.write(hdr)
        if pad:
            f.write(b"\x00" * pad)
        f.write(xml)


@pytest.fixture
def src_tree(tmp_path):
    src = tmp_path / "src"
    (src / "EFI" / "BOOT").mkdir(parents=True)
    (src / "boot" / "grub").mkdir(parents=True)
    (src / "sources").mkdir()
    (src / "bigdir").mkdir()
    deep = src / "deep" / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j"
    deep.mkdir(parents=True)
    (src / "README.TXT").write_text("hello\n")
    (src / "empty.txt").write_bytes(b"")
    (src / "EFI" / "BOOT" / "BOOTX64.EFI").write_bytes(os.urandom(100_000))
    (src / "boot" / "grub" / "grub.cfg").write_text("linux /vmlinuz archisolabel=TEST_LABEL quiet\n")
    (deep / "file.txt").write_text("deep\n")
    (src / "Mixed Case Name With Spaces And A Very Long File Name Exceeding 64 Chars.txt").write_text("x")
    (src / "unicode_é_名.txt").write_text("uni\n")
    for i in range(300):
        (src / "bigdir" / f"file_number_{i}.dat").write_text(str(i))
    (src / "data.bin").write_bytes(os.urandom(3_000_000))
    return src
