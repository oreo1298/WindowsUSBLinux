"""Windows-specific steps of ISO mode, run on a plain directory (no root needed)."""

import hashlib
import io
import os

import pytest

from conftest import make_boot_wim, make_fake_pe, needs, run, wim_listing
from rufux.core.wim import read_wim_file
from rufux.helper import context, isomode
from rufux.helper.context import Context, Emitter, HelperError

UNATTEND = ('<?xml version="1.0" encoding="utf-8"?>\n<unattend xmlns="urn:schemas-microsoft-com:unattend">'
            '<settings pass="windowsPE"></settings></unattend>\n')


def sha256(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "RUN_DIR", str(tmp_path / "run"))
    c = Context(Emitter(io.StringIO()))
    yield c
    c.run_cleanups()


@pytest.fixture
def drive(tmp_path):
    """A directory laid out like a Windows drive after the file copy."""
    mnt = tmp_path / "mnt"
    (mnt / "sources").mkdir(parents=True)
    (mnt / "setup.exe").write_bytes(make_fake_pe(0x8664))
    return mnt


@needs("wimlib-imagex")
def test_answer_file_goes_into_the_setup_image_of_boot_wim(ctx, drive, tmp_path):
    boot_wim = drive / "sources" / "boot.wim"
    make_boot_wim(str(boot_wim), str(tmp_path))
    hashes = {"sources/boot.wim": sha256(boot_wim)}
    job = isomode.IsoJob(fs="fat32", label="WIN", unattend=UNATTEND, unattend_target="bootwim")
    isomode._write_unattend(ctx, str(drive), job, hashes)
    assert sorted(os.listdir(drive)) == ["setup.exe", "sources"]  # nothing at the root
    assert "/Autounattend.xml" in wim_listing(str(boot_wim), 2)
    assert "/Autounattend.xml" not in wim_listing(str(boot_wim), 1)
    xml = run("wimlib-imagex", "extract", str(boot_wim), "2", "/Autounattend.xml", "--to-stdout").stdout
    assert xml.decode() == UNATTEND
    info = read_wim_file(str(boot_wim))
    assert info.image_count == 2 and info.boot_index == 2
    assert hashes["sources/boot.wim"] == sha256(boot_wim)  # verification checks the new file
    run("wimlib-imagex", "verify", str(boot_wim))
    assert not os.listdir(tmp_path / "run" / os.listdir(tmp_path / "run")[0])  # temp copy removed


def test_answer_file_without_windows_pe_goes_to_oem(ctx, drive):
    hashes = {}
    job = isomode.IsoJob(fs="fat32", label="WIN", unattend=UNATTEND, unattend_target="oem")
    isomode._write_unattend(ctx, str(drive), job, hashes)
    target = drive / "sources" / "$OEM$" / "$$" / "Panther" / "unattend.xml"
    assert target.read_text() == UNATTEND
    assert hashes == {"sources/$OEM$/$$/Panther/unattend.xml": sha256(target)}


@needs("wimlib-imagex")
def test_missing_boot_wim_is_an_error(ctx, drive):
    job = isomode.IsoJob(fs="fat32", label="WIN", unattend=UNATTEND, unattend_target="bootwim")
    with pytest.raises(HelperError, match="boot.wim"):
        isomode._write_unattend(ctx, str(drive), job, {})


def test_setup_wrapper_replaces_setup_exe(ctx, drive):
    original = (drive / "setup.exe").read_bytes()
    wrapper = make_fake_pe(0x8664, 1500)
    hashes = {"setup.exe": hashlib.sha256(original).hexdigest()}
    isomode._wrap_setup(ctx, str(drive), ("x64", wrapper), hashes)
    assert (drive / "setup.exe").read_bytes() == wrapper
    assert (drive / "setup.dll").read_bytes() == original
    assert hashes == {"setup.exe": hashlib.sha256(wrapper).hexdigest(),
                      "setup.dll": hashlib.sha256(original).hexdigest()}


def test_setup_wrapper_must_match_setup_exe(ctx, drive):
    original = (drive / "setup.exe").read_bytes()
    hashes = {}
    isomode._wrap_setup(ctx, str(drive), ("arm64", make_fake_pe(0xAA64)), hashes)
    assert (drive / "setup.exe").read_bytes() == original
    assert not (drive / "setup.dll").exists() and hashes == {}
    assert any("not added" in line for line in ctx.out.log_lines)
