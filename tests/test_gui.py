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

from conftest import have, make_fake_wim, needs, run  # noqa: E402
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


def test_progress_survives_multi_gigabyte_values(app):
    # Regression: byte counts above 2 GiB used to overflow a 32-bit signal argument
    # (PySide6 raised, so the bar never moved; PyQt6 wrapped them to >100% and negatives).
    from rufux.ui.helper_client import HelperClient

    client = HelperClient()
    seen = []
    client.progress.connect(lambda phase, done, total, msg: seen.append((done, total)))
    total = 6_000_000_000
    steps = [1_000_000_000, 2_500_000_000, 4_500_000_000, total]
    for done in steps:
        client._dispatch({"t": "progress", "phase": "copy", "done": done, "total": total, "msg": ""})
    assert seen == [(done, total) for done in steps]


def test_progress_bar_stays_within_bounds(window):
    w = window()
    w._on_helper_progress("copy", 4_500_000_000, 6_000_000_000, "")
    assert w.progress.value() == 750
    assert w.progress.format() == "Copying ISO files: 75.0%"
    w._on_helper_progress("write", 7_000_000_000, 6_000_000_000, "")
    assert w.progress.value() == 1000 and w.progress.format() == "Writing image: 100.0%"
    w._on_helper_progress("write", -5, 6_000_000_000, "")
    assert w.progress.value() == 0 and w.progress.format() == "Writing image: 0.0%"


def test_wue_dialog_without_wimlib(app):
    from rufux.ui.dialogs import WueDialog

    settings = qt.QSettings()
    settings.setValue("wue/bypass", True)
    dlg = WueDialog(True, None, wimlib=False)
    assert not dlg.bypass.isEnabled() and not dlg.bypass.isChecked()
    assert not dlg.options().bypass_requirements
    dlg._accept()
    assert settings.value("wue/bypass", type=bool)  # the saved choice is kept for later
    dlg = WueDialog(True, None, wimlib=True)
    assert dlg.bypass.isEnabled() and dlg.bypass.isChecked() and dlg.options().bypass_requirements


def test_setup_wrapper_lookup(window, monkeypatch):
    from rufux.core import rufusfiles
    from rufux.ui import main_window

    w = window()
    asked = []
    monkeypatch.setattr(main_window, "ask", lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(rufusfiles, "find", lambda f: "/usr/share/rufux/" + f.name)
    assert w._find_setup_wrapper("x64") == "/usr/share/rufux/setup_x64.exe" and not asked
    # Not installed and the user declines the download: the drive is made without it.
    monkeypatch.setattr(rufusfiles, "find", lambda f: None)
    assert w._find_setup_wrapper("arm64") is None and len(asked) == 1


@needs("genisoimage")
def test_start_windows_11_24h2_job(window, tmp_path, monkeypatch):
    from conftest import make_fake_pe
    from rufux.core import rufusfiles
    from rufux.ui import main_window

    src = tmp_path / "win"
    (src / "efi" / "boot").mkdir(parents=True)
    (src / "sources").mkdir()
    (src / "bootmgr").write_bytes(b"\0" * 512)
    (src / "efi" / "boot" / "bootx64.efi").write_bytes(b"MZ" + b"\0" * 510)
    (src / "sources" / "boot.wim").write_bytes(b"\0" * 4096)
    (src / "setup.exe").write_bytes(make_fake_pe(0x8664))
    make_fake_wim(str(src / "sources" / "install.wim"), build=26100)
    iso = tmp_path / "Win11_24H2.iso"
    run("genisoimage", "-quiet", "-udf", "-V", "CCCOMA_X64FRE_EN-US_DV9", "-o", str(iso), str(src))
    qt.QSettings().setValue("wue/bypass", True)
    w = window(str(iso))
    w.tools.wimlib = True
    monkeypatch.setattr(main_window.Tools, "detect", classmethod(lambda cls: w.tools))
    jobs = []
    monkeypatch.setattr(main_window, "dialog_accepted", lambda dlg: True)
    monkeypatch.setattr(main_window, "ask", lambda *a, **k: True)
    monkeypatch.setattr(rufusfiles, "find", lambda f: "/usr/share/rufux/" + f.name)
    monkeypatch.setattr(w, "_launch", jobs.append)
    w.on_start()
    assert len(jobs) == 1
    opts = jobs[0]["iso"]
    assert opts["unattend_target"] == "bootwim" and opts["boot_wim"] == "sources/boot.wim"
    assert "BypassTPMCheck" in opts["unattend"] and opts["bypass_appraiser"] is True
    assert opts["setup_wrapper"] == "/usr/share/rufux/setup_x64.exe"


def test_start_detects_newly_installed_tools(window, monkeypatch):
    from rufux.ui import main_window

    w = window()
    fresh = plan.Tools(wimlib=True, grub_bios=True, fs=dict(w.tools.fs))
    monkeypatch.setattr(main_window.Tools, "detect", classmethod(lambda cls: fresh))
    errors = []
    monkeypatch.setattr(main_window.QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a)))
    w.on_start()  # no image selected: stops with an error, after detecting the tools
    assert w.tools is fresh and errors


@pytest.mark.skipif(os.geteuid() != 0, reason="the helper is started through pkexec when not root")
def test_helper_client_closes_stdin_when_done(app, tmp_path, monkeypatch):
    from rufux.ui.helper_client import HelperClient

    fake = tmp_path / "fake-helper"
    fake.write_text(
        "import json, os\n"
        "print(json.dumps({'t': 'hello', 'version': 'test', 'protocol': 2}), flush=True)\n"
        "os.read(0, 65536)  # the job\n"
        "print(json.dumps({'t': 'done', 'ok': True, 'msg': 'fine'}), flush=True)\n"
        "while os.read(0, 4096):  # only exits once the GUI closes our stdin\n"
        "    pass\n")
    monkeypatch.setenv("RUFUX_HELPER", str(fake))
    client = HelperClient()
    results = []
    client.finished.connect(lambda ok, cancelled, msg: results.append((ok, msg)))
    client.start({"action": "write"})
    wait(app, lambda: results and not client.running, timeout=15)
    assert results == [(True, "fine")]
