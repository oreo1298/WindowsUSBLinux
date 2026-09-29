"""Secondary dialogs: log, checksums, Windows customisation, about, downloads."""

from __future__ import annotations

import getpass
import hashlib
import os

from .qt import (QCheckBox, QDesktopServices, QDialog, QDialogButtonBox, QFileDialog, QFont,
                 QFontDatabase, QGridLayout, QGuiApplication, QHBoxLayout, QLabel, QLineEdit,
                 QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSettings, Qt, QUrl,
                 QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core.distro import install_hint
from ..core.unattend import WueOptions, sanitize_username, username_problem
from ..core.util import human_size
from .widgets import Task, app_icon


class LogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setMaximumBlockCount(50000)
        lay.addWidget(self.text)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        save = QPushButton("Save")
        close = QPushButton("Close")
        clear.clicked.connect(self.text.clear)
        save.clicked.connect(self._save)
        close.clicked.connect(self.hide)
        row.addWidget(clear)
        row.addWidget(save)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

    def append(self, line: str) -> None:
        self.text.appendPlainText(line)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save log", os.path.expanduser("~/rufux.log"),
                                              "Log files (*.log *.txt);;All files (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.text.toPlainText() + "\n")
            except OSError as exc:
                QMessageBox.critical(self, "Save log", f"Could not save the log: {exc}")


class ChecksumDialog(QDialog):
    ALGOS = (("MD5", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256"), ("SHA512", "sha512"))

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Checksums")
        self.path = path
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{os.path.basename(path)}</b>"))
        grid = QGridLayout()
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.fields: dict[str, QLineEdit] = {}
        for row, (label, algo) in enumerate(self.ALGOS):
            grid.addWidget(QLabel(label + ":"), row, 0)
            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setFont(mono)
            edit.setPlaceholderText("computing...")
            grid.addWidget(edit, row, 1)
            copy = QPushButton("Copy")
            copy.clicked.connect(lambda _=False, e=edit: QGuiApplication.clipboard().setText(e.text()))
            grid.addWidget(copy, row, 2)
            self.fields[algo] = edit
        lay.addLayout(grid)
        compare_row = QHBoxLayout()
        compare_row.addWidget(QLabel("Compare with:"))
        self.compare = QLineEdit()
        self.compare.setPlaceholderText("paste an expected checksum here")
        self.compare.textChanged.connect(self._compare)
        compare_row.addWidget(self.compare, 1)
        lay.addLayout(compare_row)
        self.match = QLabel("")
        lay.addWidget(self.match)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.task = Task(self._compute, self)
        self.task.progress.connect(self._on_progress)
        self.task.done.connect(self._on_done)
        self.task.failed.connect(self._on_failed)
        self.task.start()

    def _compute(self, task: Task) -> dict:
        hashers = {algo: hashlib.new(algo) for _, algo in self.ALGOS}
        size = os.path.getsize(self.path)
        done = 0
        with open(self.path, "rb") as f:
            while True:
                if task.cancelled:
                    return {}
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                for h in hashers.values():
                    h.update(chunk)
                done += len(chunk)
                task.report(done, size)
        return {algo: h.hexdigest() for algo, h in hashers.items()}

    def _on_progress(self, done, total) -> None:
        self.bar.setValue(int(done * 1000 / max(total, 1)))
        self.bar.setFormat(f"{human_size(done)} of {human_size(total)}")

    def _on_failed(self, msg: str) -> None:
        self.match.setText(f"Error: {msg}")

    def _on_done(self, result: dict) -> None:
        for algo, value in result.items():
            self.fields[algo].setText(value)
        self.bar.setVisible(False)
        self._compare(self.compare.text())

    def _compare(self, text: str) -> None:
        text = text.strip().lower().split()[0] if text.strip() else ""
        if not text:
            self.match.setText("")
            return
        values = {e.text().lower(): name for name, e in self.fields.items() if e.text()}
        if text in values:
            self.match.setText(f"<span style='color:#1a7f37'><b>✔ Matches the {values[text].upper()} "
                               "checksum</b></span>")
        elif values:
            self.match.setText("<span style='color:#cf222e'><b>✘ Does not match</b></span>")

    def reject(self) -> None:
        self.task.cancelled = True
        self.task.wait(2000)
        super().reject()


class WueDialog(QDialog):
    """Rufus' 'Windows User Experience' dialog."""

    def __init__(self, win11: bool, parent=None, wimlib: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Windows User Experience")
        self.settings = QSettings()
        lay = QVBoxLayout(self)
        title = QLabel("Customize Windows installation?")
        f = QFont(title.font())
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        s = self.settings

        def box(text: str, key: str, default: bool) -> QCheckBox:
            cb = QCheckBox(text)
            cb.setChecked(s.value(f"wue/{key}", default, type=bool))
            lay.addWidget(cb)
            return cb

        self.bypass = box("Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0", "bypass", True)
        self.bypass.setVisible(win11)
        if win11 and not wimlib:
            # The setup part of the answer file goes into boot.wim, which needs wimlib.
            self.bypass.setChecked(False)
            self.bypass.setEnabled(False)
            missing = QLabel(f"<small>Removing the requirements needs wimlib: <b>{install_hint('wimlib')}</b></small>")
            missing.setWordWrap(True)
            missing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(missing)
        self.no_msa = box("Remove requirement for an online Microsoft account", "no_msa", True)
        row = QHBoxLayout()
        self.user = QCheckBox("Create a local account with username:")
        self.user.setChecked(s.value("wue/user", False, type=bool))
        self.username = QLineEdit(s.value("wue/username", _default_username(), type=str))
        self.username.setMaxLength(20)
        self.username.setEnabled(self.user.isChecked())
        self.user.toggled.connect(self.username.setEnabled)
        row.addWidget(self.user)
        row.addWidget(self.username, 1)
        lay.addLayout(row)
        self.locale = box("Set regional options to the same values as this user's", "locale", False)
        self.privacy = box("Disable data collection (Skip privacy questions)", "privacy", True)
        self.bitlocker = box("Disable BitLocker automatic device encryption", "bitlocker", False)
        note = QLabel("<small>The local account is created without a password; Windows asks for a "
                      "new one at first logon.</small>")
        note.setWordWrap(True)
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.win11 = win11

    def _accept(self) -> None:
        if self.user.isChecked():
            problem = username_problem(self.username.text())
            if problem:
                QMessageBox.warning(self, "Windows User Experience", problem)
                return
        s = self.settings
        if self.bypass.isEnabled():  # keep the saved choice while wimlib is missing
            s.setValue("wue/bypass", self.bypass.isChecked())
        s.setValue("wue/no_msa", self.no_msa.isChecked())
        s.setValue("wue/user", self.user.isChecked())
        s.setValue("wue/username", self.username.text())
        s.setValue("wue/locale", self.locale.isChecked())
        s.setValue("wue/privacy", self.privacy.isChecked())
        s.setValue("wue/bitlocker", self.bitlocker.isChecked())
        self.accept()

    def options(self) -> WueOptions:
        return WueOptions(
            bypass_requirements=self.win11 and self.bypass.isChecked(),
            no_online_account=self.no_msa.isChecked(),
            local_account=sanitize_username(self.username.text()) if self.user.isChecked() else "",
            duplicate_locale=self.locale.isChecked(),
            no_data_collection=self.privacy.isChecked(),
            disable_bitlocker=self.bitlocker.isChecked(),
        )


def _default_username() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        name = "User"
    name = sanitize_username(name) or "User"
    return name[:1].upper() + name[1:]


DOWNLOADS = [
    ("Windows 11", "https://www.microsoft.com/software-download/windows11"),
    ("Windows 10", "https://www.microsoft.com/software-download/windows10ISO"),
    ("Ubuntu", "https://ubuntu.com/download/desktop"),
    ("Linux Mint", "https://linuxmint.com/download.php"),
    ("Debian", "https://www.debian.org/distrib/"),
    ("Fedora", "https://fedoraproject.org/workstation/download"),
    ("openSUSE", "https://get.opensuse.org/"),
    ("Arch Linux", "https://archlinux.org/download/"),
    ("CachyOS", "https://cachyos.org/download/"),
    ("EndeavourOS", "https://endeavouros.com/"),
    ("Manjaro", "https://manjaro.org/products/download/x86"),
    ("SystemRescue", "https://www.system-rescue.org/Download/"),
]


class DownloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download an image")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Open the official download page of:"))
        grid = QGridLayout()
        for i, (name, url) in enumerate(DOWNLOADS):
            b = QPushButton(name)
            b.setToolTip(url)
            b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)
        tip = QLabel("<small>After downloading, click SELECT and pick the file. Use the <b>#</b> button "
                     "to compare its checksum with the one published by the distribution.</small>")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


def show_about(parent: QWidget) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(f"About {APP_NAME}")
    box.setIconPixmap(app_icon().pixmap(64, 64))
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<h3>{APP_NAME} {__version__}</h3>"
        "<p>A Rufus-style tool for creating bootable USB drives from ISO and disk images "
        "on Linux.</p>"
        "<p>Inspired by <a href='https://rufus.ie'>Rufus</a> by Pete Batard (not affiliated).<br>"
        "UEFI:NTFS &copy; Pete Batard (GPLv2+).<br>"
        "Uses util-linux, dosfstools, ntfs-3g, exfatprogs, e2fsprogs, wimlib and GRUB.</p>"
        "<p>License: GNU GPL v3 or later.</p>")
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def ask(parent: QWidget, title: str, text: str, icon=QMessageBox.Icon.Warning, informative: str = "") -> bool:
    box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, parent)
    if informative:
        box.setInformativeText(informative)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() == box.button(QMessageBox.StandardButton.Ok)


