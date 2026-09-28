"""Offscreen smoke tests of the Qt window (skipped when neither PySide6 nor PyQt6 is installed)."""

import os
import struct
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from rufux.ui import qt
except ImportError as exc:  # no Qt binding installed
    pytest.skip(str(exc), allow_module_level=True)

from conftest import have, make_fake_wim, run  # noqa: E402
from rufux.core import blockdevs, plan  # noqa: E402

FAKE = blockdevs.Drive(name="sdz", path="/dev/sdz", size=32_010_928_128, model="Ultra", vendor="SanDisk",
                       serial="0001", tran="usb", removable=True)


@pytest.fixture(scope="module")
def app():
    qt.QCoreApplication.setOrganizationName("rufux-tests")
    qt.QCoreApplication.setApplicationName("rufux-tests")
    inst = qt.QApplication.instance() or qt.QApplication(["rufux-tests"])
    yield inst


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(blockdevs, "list_drives", lambda **kw: [FAKE])
    from rufux.ui.main_window import MainWindow

    windows = []

    def make(image=None):
        w = MainWindow(image)
        windows.append(w)
        wait(app, lambda: w.sel.drive is not None and (image is None or w.sel.image is not None))
        return w

    yield make
    for w in windows:
        w.refresh_timer.stop()
        w.close()


def wait(app, cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def make_disk_image(path):
    """A raw disk image with an MBR partition table (built in Python)."""
    mbr = bytearray(512)
    struct.pack_into("<BBBBBBBBII", mbr, 446, 0x80, 0, 0, 0, 0x0C, 0, 0, 0, 2048, 8192)
    mbr[510:512] = b"\x55\xaa"
    with open(path, "wb") as f:
        f.write(mbr)
        f.truncate(8 * 1024 * 1024)


def test_raw_image_uses_dd(window, tmp_path):
    img = tmp_path / "raspios.img"
    make_disk_image(img)
    w = window(str(img))
    assert w.device_combo.currentText() == "SanDisk Ultra (sdz) [32 GB]"
    assert w.sel.mode == plan.MODE_DD
    assert not w.mode_combo.isVisible()
    assert not w.fs_combo.isEnabled() and not w.label_edit.isEnabled()
    assert w.start_btn.isEnabled()
    w._sync_selection()
    job = plan.build_job(w.sel, w.tools)
    assert job["mode"] == "dd" and job["image"] == str(img)


@pytest.mark.skipif(not have("genisoimage"), reason="needs genisoimage")
def test_windows_iso_defaults(window, tmp_path):
    src = tmp_path / "win"
    (src / "efi" / "boot").mkdir(parents=True)
    (src / "sources").mkdir()
    (src / "bootmgr").write_bytes(b"\0" * 512)
    (src / "efi" / "boot" / "bootx64.efi").write_bytes(b"MZ" + b"\0" * 510)
    make_fake_wim(str(src / "sources" / "install.wim"))
    iso = tmp_path / "Win11.iso"
    run("genisoimage", "-quiet", "-udf", "-V", "CCCOMA_X64FRE_EN-US_DV9", "-o", str(iso), str(src))
    w = window(str(iso))
    assert w.boot_combo.currentText() == "Win11.iso"
    assert w.scheme_combo.currentData() == "gpt"
    assert w.target_combo.currentData() == plan.TARGET_UEFI
    assert w.fs_combo.currentData() == "fat32"
    assert w.label_edit.text() == "CCCOMA_X64FRE_EN-US_DV9"
    # MBR offers BIOS boot only when GRUB is available
    w.scheme_combo.setCurrentIndex(w.scheme_combo.findData("mbr"))
    targets = [w.target_combo.itemData(i) for i in range(w.target_combo.count())]
    assert (plan.TARGET_BIOS_UEFI in targets) == w.tools.grub_bios
    # Non bootable
    w.boot_combo.setCurrentIndex(1)
    assert w.sel.boot == plan.BOOT_NONE
    assert w.scheme_combo.currentData() == "mbr"
    assert w.fs_combo.isEnabled()
    fss = [w.fs_combo.itemData(i) for i in range(w.fs_combo.count())]
    assert "fat16" not in fss  # the drive is larger than 4 GB


def test_busy_state_toggles_controls(window):
    w = window()
    w.set_busy(True)
    assert not w.device_combo.isEnabled() and not w.start_btn.isEnabled()
    assert w.close_btn.text() == "CANCEL"
    w.set_busy(False)
    assert w.device_combo.isEnabled() and w.close_btn.text() == "CLOSE"
