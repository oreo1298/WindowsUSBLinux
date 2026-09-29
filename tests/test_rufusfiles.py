import hashlib
import io
import os
import urllib.request

import pytest

from rufux.core import rufusfiles, uefintfs
from rufux.core.image import pe_arch
from rufux.core.rufusfiles import RufusFile

from conftest import make_fake_pe


def fake_file(data: bytes, name: str = "setup_x64.exe") -> RufusFile:
    return RufusFile(name, "setup/" + name, hashlib.sha256(data).hexdigest(), len(data))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Only look in a temporary cache, never in /usr/share or the package."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(rufusfiles, "SHARE_DIRS", ())
    monkeypatch.setattr(rufusfiles, "PACKAGE_DIR", str(tmp_path / "pkg"))
    return tmp_path / "cache" / "rufux"


def test_pins_match_the_rufus_release():
    assert rufusfiles.RUFUS_VERSION == "v4.15"
    assert set(rufusfiles.SETUP_WRAPPERS) == {"x64", "arm64"}
    for f in [rufusfiles.UEFI_NTFS, *rufusfiles.SETUP_WRAPPERS.values()]:
        assert f.url == f"https://raw.githubusercontent.com/pbatard/rufus/v4.15/res/{f.repo_path}"
        assert len(f.sha256) == 64
    assert uefintfs.SHA256 == rufusfiles.UEFI_NTFS.sha256 and uefintfs.URL == rufusfiles.UEFI_NTFS.url


def test_find_only_accepts_the_pinned_checksum(isolated):
    data = make_fake_pe()
    f = fake_file(data)
    assert rufusfiles.find(f) is None
    isolated.mkdir(parents=True)
    (isolated / f.name).write_bytes(data + b"tampered")
    assert rufusfiles.find(f) is None
    (isolated / f.name).write_bytes(data)
    assert rufusfiles.find(f) == str(isolated / f.name)


def test_identify_setup_wrapper(monkeypatch):
    data = make_fake_pe(0xAA64)
    assert rufusfiles.identify_setup_wrapper(data) is None
    monkeypatch.setitem(rufusfiles.SETUP_WRAPPERS, "arm64", fake_file(data, "setup_arm64.exe"))
    assert rufusfiles.identify_setup_wrapper(data) == "arm64"


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __exit__(self, *exc):
        self.close()


def test_download_checks_the_checksum(isolated, monkeypatch):
    data = make_fake_pe()
    f = fake_file(data)
    served = [data + b"x", data]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(served.pop(0)))
    with pytest.raises(OSError, match="checksum"):
        rufusfiles.download(f)
    assert os.listdir(isolated) == []  # no partial file left behind
    seen = []
    path = rufusfiles.download(f, lambda done, total: seen.append((done, total)))
    assert open(path, "rb").read() == data and seen[-1] == (len(data), len(data))
    assert rufusfiles.find(f) == path


def test_pe_arch():
    assert pe_arch(make_fake_pe(0x8664)) == "x64"
    assert pe_arch(make_fake_pe(0xAA64)) == "arm64"
    assert pe_arch(make_fake_pe(0x014C)) == "x86"
    assert pe_arch(make_fake_pe(0x1234)) == ""
    assert pe_arch(b"MZ" + b"\x00" * 100) == ""
    assert pe_arch(b"\x7fELF" + os.urandom(200)) == ""
