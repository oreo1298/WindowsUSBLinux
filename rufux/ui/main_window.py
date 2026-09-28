"""The main window, laid out like Rufus."""

from __future__ import annotations

import os

from PySide6.QtCore import QElapsedTimer, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QProgressDialog, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QStyle, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, HELPER_PROTOCOL, __version__
from ..core import blockdevs, image, plan, uefintfs
from ..core.fsdefs import FILESYSTEMS, cluster_label, cluster_sizes
from ..core.plan import BOOT_IMAGE, BOOT_NONE, MODE_DD, MODE_ISO, PlanError, Selection, Tools
from ..core.unattend import detect_regional_settings
from ..core.util import GiB, format_duration, human_size
from .dialogs import ChecksumDialog, DownloadDialog, LogDialog, WueDialog, ask, show_about
from .helper_client import HelperClient
from .widgets import Collapsible, SectionHeader, Task, app_icon, small_button, theme_icon

IMAGE_FILTER = ("Disk images (" + " ".join(f"*.{e}" for e in image.IMAGE_EXTENSIONS) + ");;"
                "ISO images (*.iso);;All files (*)")

PHASES = {
    "write": "Writing image", "verify": "Verifying", "copy": "Copying ISO files",
    "split": "Splitting install.wim", "extract": "Extracting install.wim", "zero": "Formatting",
    "badblocks": "Checking bad blocks", "save": "Saving drive", "verify-wim": "Verifying install.swm",
}

MODE_TEXT = {
    MODE_DD: "Write in DD Image mode",
    MODE_ISO: "Write in ISO Image mode (file copy)",
}


class MainWindow(QMainWindow):
    def __init__(self, image_path: str | None = None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.settings = QSettings()
        self.tools = Tools.detect()
        self.sel = Selection()
        self.drives: dict[str, blockdevs.Drive] = {}
        self._drive_sig: list = []
        self.user_label = False
        self.busy = False
        self.close_after = False
        self._tasks: set[Task] = set()
        self._refreshing = False
        self._force_refresh = False
        self._updating = False
        self.helper = HelperClient(self)
        self.helper.log.connect(self.log)
        self.helper.status.connect(self._on_helper_status)
        self.helper.progress.connect(self._on_helper_progress)
        self.helper.finished.connect(self._on_helper_finished)
        self.log_dialog = LogDialog(self)
        self.elapsed = QElapsedTimer()
        self._build_ui()
        self._load_settings()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh_devices)
        self.refresh_timer.start()
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._tick)
        self.log(f"{APP_NAME} {__version__}")
        self._log_tools()
        self.refresh_devices(force=True)
        self.refresh_options()
        self.setAcceptDrops(True)
        if image_path:
            QTimer.singleShot(0, lambda: self.load_image(image_path))

    # ------------------------------------------------------------------ UI --

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(14, 6, 14, 6)
        v.setSpacing(3)

        v.addWidget(SectionHeader("Drive Properties"))
        v.addWidget(QLabel("Device"))
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.device_combo.setMinimumContentsLength(30)
        self.save_btn = small_button(
            "💾", "Save the drive to an image file",
            theme_icon(self, ("document-save", "media-floppy"), QStyle.SP_DialogSaveButton))
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.save_btn)
        v.addLayout(row)

        v.addWidget(QLabel("Boot selection"))
        row = QHBoxLayout()
        self.boot_combo = QComboBox()
        self.boot_combo.addItem("Disk or ISO image (Please select)", BOOT_IMAGE)
        self.boot_combo.addItem("Non bootable", BOOT_NONE)
        self.boot_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.hash_btn = small_button("#", "Compute the image checksums (MD5, SHA1, SHA256, SHA512)")
        f = QFont(self.hash_btn.font())
        f.setBold(True)
        self.hash_btn.setFont(f)
        self.select_btn = QToolButton()
        self.select_btn.setText("SELECT")
        self.select_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.select_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.select_btn.setMinimumWidth(96)
        menu = QMenu(self.select_btn)
        act_select = QAction("Select an image...", self)
        act_select.triggered.connect(self.select_image)
        act_download = QAction("Download an image...", self)
        act_download.triggered.connect(lambda: DownloadDialog(self).exec())
        menu.addAction(act_select)
        menu.addAction(act_download)
        self.select_btn.setMenu(menu)
        row.addWidget(self.boot_combo, 1)
        row.addWidget(self.hash_btn)
        row.addWidget(self.select_btn)
        v.addLayout(row)

        self.mode_label = QLabel("Image option")
        self.mode_combo = QComboBox()
        v.addWidget(self.mode_label)
        v.addWidget(self.mode_combo)

        self.persist_label = QLabel("Persistent partition size")
        self.persist_row = QWidget()
        pr = QHBoxLayout(self.persist_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.persist_slider = QSlider(Qt.Horizontal)
        self.persist_spin = QSpinBox()
        self.persist_spin.setSuffix(" GB")
        self.persist_spin.setSpecialValueText("No persistence")
        self.persist_spin.setMinimumWidth(130)
        pr.addWidget(self.persist_slider, 1)
        pr.addWidget(self.persist_spin)
        v.addWidget(self.persist_label)
        v.addWidget(self.persist_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.addWidget(QLabel("Partition scheme"), 0, 0)
        grid.addWidget(QLabel("Target system"), 0, 1)
        self.scheme_combo = QComboBox()
        self.target_combo = QComboBox()
        grid.addWidget(self.scheme_combo, 1, 0)
        grid.addWidget(self.target_combo, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)

        self.adv_drive = Collapsible("Show advanced drive properties", "Hide advanced drive properties")
        self.usb_hdd_cb = QCheckBox("List USB Hard Drives")
        self.adv_drive.add_widget(self.usb_hdd_cb)
        v.addWidget(self.adv_drive)

        v.addWidget(SectionHeader("Format Options"))
        v.addWidget(QLabel("Volume label"))
        self.label_edit = QLineEdit()
        v.addWidget(self.label_edit)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.addWidget(QLabel("File system"), 0, 0)
        grid.addWidget(QLabel("Cluster size"), 0, 1)
        self.fs_combo = QComboBox()
        self.cluster_combo = QComboBox()
        grid.addWidget(self.fs_combo, 1, 0)
        grid.addWidget(self.cluster_combo, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)

        self.adv_format = Collapsible("Show advanced format options", "Hide advanced format options")
        self.quick_cb = QCheckBox("Quick format")
        self.extlabel_cb = QCheckBox("Create extended label and icon files")
        bad = QWidget()
        br = QHBoxLayout(bad)
        br.setContentsMargins(0, 0, 0, 0)
        self.bad_cb = QCheckBox("Check device for bad blocks")
        self.bad_combo = QComboBox()
        for n in range(1, 5):
            self.bad_combo.addItem(f"{n} pass" + ("es" if n > 1 else ""), n)
        br.addWidget(self.bad_cb)
        br.addWidget(self.bad_combo)
        br.addStretch(1)
        self.verify_cb = QCheckBox("Verify written data")
        self.eject_cb = QCheckBox("Safely remove the drive when finished")
        for w in (self.quick_cb, self.extlabel_cb, bad, self.verify_cb, self.eject_cb):
            self.adv_format.add_widget(w)
        v.addWidget(self.adv_format)

        v.addWidget(SectionHeader("Status"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setAlignment(Qt.AlignCenter)
        self.progress.setFormat("READY")
        self.progress.setMinimumHeight(28)
        v.addWidget(self.progress)

        row = QHBoxLayout()
        self.about_btn = small_button("i", "About", theme_icon(self, ("help-about",), QStyle.SP_MessageBoxInformation))
        self.log_btn = small_button("Log", "Show the log",
                                    theme_icon(self, ("text-x-generic", "document-properties"),
                                               QStyle.SP_FileDialogDetailedView))
        self.start_btn = QPushButton("START")
        self.close_btn = QPushButton("CLOSE")
        for b in (self.start_btn, self.close_btn):
            b.setMinimumWidth(96)
            b.setMinimumHeight(30)
        self.start_btn.setDefault(True)
        row.addWidget(self.about_btn)
        row.addWidget(self.log_btn)
        row.addStretch(1)
        row.addWidget(self.start_btn)
        row.addWidget(self.close_btn)
        v.addSpacing(4)
        v.addLayout(row)

        self.status_msg = QLabel("")
        self.status_time = QLabel("00:00:00")
        self.statusBar().addWidget(self.status_msg, 1)
        self.statusBar().addPermanentWidget(self.status_time)
        self.statusBar().setSizeGripEnabled(False)

        # signals
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.boot_combo.currentIndexChanged.connect(self._on_boot_changed)
        self.select_btn.clicked.connect(self.select_image)
        self.hash_btn.clicked.connect(self._show_checksums)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.fs_combo.currentIndexChanged.connect(self._on_fs_changed)
        self.cluster_combo.currentIndexChanged.connect(self._on_cluster_changed)
        self.label_edit.textEdited.connect(self._on_label_edited)
        self.usb_hdd_cb.toggled.connect(lambda _: self.refresh_devices(force=True))
        self.persist_slider.valueChanged.connect(self.persist_spin.setValue)
        self.persist_spin.valueChanged.connect(self._on_persistence)
        self.bad_cb.toggled.connect(self.bad_combo.setEnabled)
        self.start_btn.clicked.connect(self.on_start)
        self.close_btn.clicked.connect(self._on_close_clicked)
        self.save_btn.clicked.connect(self._on_save_drive)
        self.about_btn.clicked.connect(lambda: show_about(self))
        self.log_btn.clicked.connect(self._toggle_log)
        self.adv_drive.toggled.connect(lambda _: QTimer.singleShot(0, self._fit))
        self.adv_format.toggled.connect(lambda _: QTimer.singleShot(0, self._fit))
        self.setMinimumWidth(500)

    def _fit(self) -> None:
        self.adjustSize()
        self.resize(max(self.width(), 520), self.sizeHint().height())

    def _load_settings(self) -> None:
        s = self.settings
        self.usb_hdd_cb.setChecked(s.value("show_usb_hdd", False, type=bool))
        self.quick_cb.setChecked(s.value("quick_format", True, type=bool))
        self.extlabel_cb.setChecked(s.value("extended_label", True, type=bool))
        self.verify_cb.setChecked(s.value("verify", True, type=bool))
        self.eject_cb.setChecked(s.value("eject", False, type=bool))
        self.bad_cb.setChecked(False)
        self.bad_combo.setCurrentIndex(max(0, min(3, s.value("badblock_passes", 1, type=int) - 1)))
        self.bad_combo.setEnabled(False)
        self.adv_drive.set_expanded(s.value("adv_drive", False, type=bool))
        self.adv_format.set_expanded(s.value("adv_format", False, type=bool))

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("show_usb_hdd", self.usb_hdd_cb.isChecked())
        s.setValue("quick_format", self.quick_cb.isChecked())
        s.setValue("extended_label", self.extlabel_cb.isChecked())
        s.setValue("verify", self.verify_cb.isChecked())
        s.setValue("eject", self.eject_cb.isChecked())
        s.setValue("badblock_passes", self.bad_combo.currentData())
        s.setValue("adv_drive", self.adv_drive.expanded)
        s.setValue("adv_format", self.adv_format.expanded)

    def _log_tools(self) -> None:
        missing = [f"{FILESYSTEMS[fs].name} ({FILESYSTEMS[fs].package})"
                   for fs, ok in self.tools.fs.items() if not ok]
        if missing:
            self.log("Not available (package not installed): " + ", ".join(missing))
        if not self.tools.wimlib:
            self.log("wimlib is not installed: Windows images with install.wim > 4 GB need NTFS")
        if not self.tools.grub_bios:
            self.log("GRUB (i386-pc) is not installed: legacy BIOS boot of Windows media is unavailable")

    # ------------------------------------------------------------- logging --

    def log(self, msg: str) -> None:
        self.log_dialog.append(msg)

    def status(self, msg: str) -> None:
        self.status_msg.setText(msg)

    def _toggle_log(self) -> None:
        if self.log_dialog.isVisible():
            self.log_dialog.hide()
        else:
            self.log_dialog.show()
            self.log_dialog.raise_()

    # ------------------------------------------------------------- devices --

    def refresh_devices(self, force: bool = False) -> None:
        if self.busy or self._refreshing:
            if force:
                self._force_refresh = True
            return
        self._refreshing = True
        show_hdd = self.usb_hdd_cb.isChecked()
        allow_loop = os.environ.get("RUFUX_ALLOW_LOOP") == "1"
        task = Task(lambda t: blockdevs.list_drives(show_usb_hdd=show_hdd, allow_loop=allow_loop), self)
        task.context["force"] = force or self._force_refresh
        self._force_refresh = False
        task.done.connect(self._on_drives_task)
        task.failed.connect(self._on_drives_failed)
        task.finished.connect(self._on_refresh_finished)
        self._tasks.add(task)
        task.start()

    def _on_drives_task(self, drives: list) -> None:
        task = self.sender()
        self._on_drives(drives, bool(getattr(task, "context", {}).get("force")))

    def _on_drives_failed(self, msg: str) -> None:
        self.status(f"Device detection failed: {msg}")

    def _on_refresh_finished(self) -> None:
        self._tasks.discard(self.sender())
        self._refreshing = False

    def _on_task_finished(self) -> None:
        self._tasks.discard(self.sender())

    def _on_drives(self, drives: list, force: bool) -> None:
        sig = [(d.key(), d.display_name()) for d in drives]
        new = {d.key(): d for d in drives}
        if sig == self._drive_sig and not force:
            self.drives = new
            if self.sel.drive is not None and self.sel.drive.key() in new:
                self.sel.drive = new[self.sel.drive.key()]
            return
        changed = sig != self._drive_sig
        self._drive_sig = sig
        current = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in drives:
            self.device_combo.addItem(d.display_name(), d.key())
            idx = self.device_combo.count() - 1
            self.device_combo.setItemData(idx, f"{d.path} - {d.vendor_model or 'unknown model'}, "
                                               f"{human_size(d.size)}, {d.tran or 'unknown bus'}",
                                          Qt.ToolTipRole)
        self.drives = new
        if current in new:
            self.device_combo.setCurrentIndex(list(new).index(current))
        elif drives:
            self.device_combo.setCurrentIndex(0)
        self.device_combo.blockSignals(False)
        if not self.busy and (changed or not self.status_msg.text()):
            n = len(drives)
            self.status(f"{n} device{'s' if n != 1 else ''} found" if n else "No device found")
        self._on_device_changed()

    def _on_device_changed(self, *_):
        key = self.device_combo.currentData()
        self.sel.drive = self.drives.get(key)
        self.refresh_options()

    # --------------------------------------------------------------- image --

    def select_image(self) -> None:
        start = self.settings.value("last_dir", "", type=str)
        if not start or not os.path.isdir(start):
            dl = os.path.expanduser("~/Downloads")
            start = dl if os.path.isdir(dl) else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Select an image", start, IMAGE_FILTER)
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> None:
        if not os.path.isfile(path):
            QMessageBox.critical(self, APP_NAME, f"'{path}' is not a file.")
            return
        self.settings.setValue("last_dir", os.path.dirname(os.path.abspath(path)))
        self.status("Analyzing image...")
        self.progress.setRange(0, 0)
        self.select_btn.setEnabled(False)
        task = Task(lambda t: image.analyze(path), self)
        task.done.connect(self._on_image)
        task.failed.connect(self._on_image_failed)
        task.finished.connect(self._on_task_finished)
        self._tasks.add(task)
        task.start()

    def _image_done(self) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("READY")
        self.select_btn.setEnabled(not self.busy)

    def _on_image_failed(self, msg: str) -> None:
        self._image_done()
        QMessageBox.critical(self, APP_NAME, f"Could not analyze the image:\n{msg}")
        self.status("")

    def _on_image(self, info: image.ImageInfo) -> None:
        self._image_done()
        if info.kind == "unknown" and info.errors:
            QMessageBox.critical(self, APP_NAME, "This image could not be read:\n" + "\n".join(info.errors[:3]))
            return
        self.sel.image = info
        self.sel.boot = BOOT_IMAGE
        modes = plan.modes_for(info)
        self.sel.mode = modes[0] if modes else MODE_DD
        self.sel.scheme = plan.default_scheme(self.sel)
        self.sel.fs = ""
        self.sel.cluster = 0
        self.sel.persistence_size = 0
        self.user_label = False
        self.boot_combo.blockSignals(True)
        self.boot_combo.setItemText(0, info.name)
        self.boot_combo.setCurrentIndex(0)
        self.boot_combo.blockSignals(False)
        self._log_image(info)
        self.refresh_options()
        self.status(f"Using image: {info.name}")

    def _log_image(self, info: image.ImageInfo) -> None:
        self.log(f"Image: {info.path}")
        self.log(f"  Size: {human_size(info.size)}" + (f", compressed ({info.compression})" if info.compression else ""))
        self.log(f"  Type: {info.describe()} [{info.kind}{', ' + info.fs_kind if info.fs_kind else ''}]")
        if info.label:
            self.log(f"  Label: '{info.label}'")
        if info.part_table:
            self.log(f"  Contains a {info.part_table.upper()} partition table" +
                     (" (ISOHybrid)" if info.is_hybrid else ""))
        if info.kind == "iso":
            self.log(f"  {info.file_count} files, {info.dir_count} folders, {human_size(info.total_bytes)}")
            self.log(f"  UEFI boot: {', '.join(info.efi_archs) or ('El Torito image' if info.has_eltorito_efi else 'no')}"
                     f"; BIOS boot: {'yes' if info.bios_bootable or info.has_bootmgr else 'no'}")
            if info.big_files:
                self.log(f"  Files larger than 4 GB: {', '.join(info.big_files)}")
            if info.windows:
                w = info.windows
                self.log(f"  {w.product} build {w.build} {w.arch}, {w.wim_path} "
                         f"({human_size(w.wim_size)}, {w.wim_images} edition(s))")
            if info.persistence:
                self.log(f"  Supports persistence ({'Ubuntu/casper' if info.persistence == 'casper' else 'Debian/live-boot'})")
        for e in info.errors:
            self.log(f"  Note: {e}")

    def _show_checksums(self) -> None:
        if self.sel.image is not None:
            ChecksumDialog(self.sel.image.path, self).exec()

    def dragEnterEvent(self, event) -> None:
        if not self.busy and event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.load_image(url.toLocalFile())
                break

    # ------------------------------------------------------------- options --

    def _on_boot_changed(self, *_):
        self.sel.boot = self.boot_combo.currentData()
        if self.sel.boot == BOOT_NONE:
            self.sel.scheme = "mbr"
            self.sel.fs = ""
        else:
            self.sel.scheme = plan.default_scheme(self.sel)
            self.sel.fs = ""
        self.user_label = False
        self.refresh_options()

    def _on_mode_changed(self, *_):
        if self._updating:
            return
        self.sel.mode = self.mode_combo.currentData() or MODE_ISO
        self.sel.scheme = plan.default_scheme(self.sel)
        self.sel.fs = ""
        self.refresh_options()

    def _on_scheme_changed(self, *_):
        if self._updating:
            return
        self.sel.scheme = self.scheme_combo.currentData() or "mbr"
        self.sel.target = ""
        self.refresh_options()

    def _on_target_changed(self, *_):
        if not self._updating:
            self.sel.target = self.target_combo.currentData() or ""

    def _on_fs_changed(self, *_):
        if self._updating:
            return
        self.sel.fs = self.fs_combo.currentData() or ""
        self.sel.cluster = -1
        self.refresh_options()

    def _on_cluster_changed(self, *_):
        if not self._updating:
            self.sel.cluster = self.cluster_combo.currentData() or 0

    def _on_label_edited(self, text: str) -> None:
        self.user_label = True
        self.sel.label = text

    def _on_persistence(self, gb: int) -> None:
        if self.persist_slider.value() != gb:
            self.persist_slider.setValue(gb)
        self.sel.persistence_size = gb * GiB
        if not self._updating:
            self.refresh_options()

    def refresh_options(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self._refresh_options()
        finally:
            self._updating = False

    def _refresh_options(self) -> None:
        sel, tools = self.sel, self.tools
        drive_size = sel.drive.size if sel.drive else 0
        has_image = sel.boot == BOOT_IMAGE and sel.image is not None
        # write mode
        modes = plan.modes_for(sel.image) if has_image else []
        if modes and sel.mode not in modes:
            sel.mode = modes[0]
        self.mode_combo.clear()
        for m in modes:
            text = MODE_TEXT[m]
            if m == modes[0] and len(modes) > 1:
                text += " (Recommended)"
            self.mode_combo.addItem(text, m)
        if modes:
            self.mode_combo.setCurrentIndex(modes.index(sel.mode))
        show_modes = len(modes) > 1
        self.mode_label.setVisible(show_modes)
        self.mode_combo.setVisible(show_modes)
        dd = has_image and sel.mode == MODE_DD

        # persistence
        show_persist = bool(has_image and sel.image.persistence and sel.mode == MODE_ISO)
        self.persist_label.setVisible(show_persist)
        self.persist_row.setVisible(show_persist)
        if show_persist:
            max_gb = int(plan.max_persistence(sel) // GiB)
            self.persist_slider.setRange(0, max_gb)
            self.persist_spin.setRange(0, max_gb)
            gb = min(int(sel.persistence_size // GiB), max_gb)
            self.persist_slider.setValue(gb)
            self.persist_spin.setValue(gb)
            sel.persistence_size = gb * GiB
        else:
            sel.persistence_size = 0

        # partition scheme
        schemes = plan.schemes_for(sel)
        if sel.scheme not in schemes:
            default = plan.default_scheme(sel)
            sel.scheme = default if default in schemes else schemes[0]
        self.scheme_combo.clear()
        for s in schemes:
            self.scheme_combo.addItem(s.upper(), s)
        self.scheme_combo.setCurrentIndex(schemes.index(sel.scheme))
        self.scheme_combo.setEnabled(len(schemes) > 1 and not dd)

        # target system
        targets = plan.targets_for(sel, tools)
        if sel.target not in targets:
            sel.target = targets[0]
        self.target_combo.clear()
        for t in targets:
            self.target_combo.addItem(plan.TARGET_LABELS[t], t)
        self.target_combo.setCurrentIndex(targets.index(sel.target))
        self.target_combo.setEnabled(len(targets) > 1)
        if has_image and sel.image.is_windows and sel.scheme == "mbr" and not tools.grub_bios:
            self.target_combo.setToolTip("Install 'grub' (sudo pacman -S grub) to also create "
                                         "legacy BIOS bootable Windows drives")
        else:
            self.target_combo.setToolTip("")

        # file system
        fss = plan.filesystems_for(sel, tools, drive_size)
        default_fs = plan.default_filesystem(sel, tools, drive_size)
        if sel.fs not in fss:
            sel.fs = default_fs
        self.fs_combo.clear()
        if dd:
            self.fs_combo.addItem("(from the image)", "")
        for fs in fss:
            name = FILESYSTEMS[fs].name
            if fs == "fat32" and drive_size > 32 * 1000 ** 3:
                name = "Large FAT32"
            if fs == default_fs:
                name += " (Default)"
            self.fs_combo.addItem(name, fs)
        if sel.fs in fss:
            self.fs_combo.setCurrentIndex(fss.index(sel.fs))
        self.fs_combo.setEnabled(bool(fss) and not dd)
        split_hint = plan.will_split_wim(sel, tools)
        self.fs_combo.setToolTip("install.wim is larger than 4 GB and will be split into install.swm "
                                 "files (supported by Windows Setup)" if split_hint else "")

        # cluster size
        self.cluster_combo.clear()
        if sel.fs and not dd:
            sector = sel.drive.log_sec if sel.drive else 512
            sizes, default = cluster_sizes(sel.fs, drive_size or 16 * GiB, sector)
            if sel.cluster not in sizes:
                sel.cluster = default
            for n in sizes:
                self.cluster_combo.addItem(cluster_label(n, n == default) if n else "Default", n)
            if sizes:
                self.cluster_combo.setCurrentIndex(sizes.index(sel.cluster))
        else:
            self.cluster_combo.addItem("(from the image)" if dd else "", 0)
        self.cluster_combo.setEnabled(self.cluster_combo.count() > 1 and not dd)

        # label
        if not self.user_label:
            sel.label = plan.default_label(sel)
            self.label_edit.setText(sel.label)
        self.label_edit.setEnabled(not dd)
        if sel.fs:
            self.label_edit.setMaxLength(64)
            fsd = FILESYSTEMS[sel.fs]
            self.label_edit.setToolTip(f"{fsd.name} labels are limited to {fsd.label_max} characters"
                                       + (" and are stored in upper case" if fsd.label_upper else ""))

        self.quick_cb.setEnabled(not dd)
        self.extlabel_cb.setEnabled(not dd)
        self.hash_btn.setEnabled(has_image)
        self.save_btn.setEnabled(sel.drive is not None and not self.busy)
        ready = sel.drive is not None and (sel.boot == BOOT_NONE or has_image)
        self.start_btn.setEnabled(ready and not self.busy)
        QTimer.singleShot(0, self._fit)

    # --------------------------------------------------------------- start --

    def _sync_selection(self) -> None:
        sel = self.sel
        sel.label = self.label_edit.text()
        sel.quick = self.quick_cb.isChecked()
        sel.extended_label = self.extlabel_cb.isChecked()
        sel.badblocks = self.bad_combo.currentData() if self.bad_cb.isChecked() else 0
        sel.verify = self.verify_cb.isChecked()
        sel.eject = self.eject_cb.isChecked()
        sel.target = self.target_combo.currentData() or sel.target
        sel.cluster = self.cluster_combo.currentData() or 0

    def on_start(self) -> None:
        if self.busy:
            return
        self._sync_selection()
        self._save_settings()
        sel = self.sel
        if sel.drive is None:
            QMessageBox.warning(self, APP_NAME, "Please select a device.")
            return
        if plan.needs_uefi_ntfs(sel):
            path = uefintfs.find() or self._download_uefi_ntfs()
            if not path:
                return
            sel.uefi_ntfs = path
        else:
            sel.uefi_ntfs = None
        info = sel.image if sel.boot == BOOT_IMAGE else None
        sel.wue = plan.WueOptions()
        sel.regional = None
        if info is not None and sel.mode == MODE_ISO and info.windows is not None and info.windows.supports_wue:
            dlg = WueDialog(info.windows.is_win11, self)
            if dlg.exec() != WueDialog.Accepted:
                return
            sel.wue = dlg.options()
            if sel.wue.duplicate_locale:
                sel.regional = detect_regional_settings(info.windows.languages)
        try:
            warnings = plan.validate(sel, self.tools)
        except PlanError as exc:
            QMessageBox.critical(self, APP_NAME, str(exc))
            return
        for w in warnings:
            if not ask(self, APP_NAME, w, QMessageBox.Warning, "Do you want to continue anyway?"):
                return
        d = sel.drive
        extra = ""
        if d.mountpoints:
            extra = "The following will be unmounted: " + ", ".join(d.mountpoints)
        if sel.badblocks:
            extra = (extra + "\n\n" if extra else "") + (
                "The bad blocks check writes over the whole drive several times and can take a long time.")
        if not ask(self, APP_NAME,
                   f"WARNING: ALL DATA ON DEVICE '{d.display_name()}' WILL BE DESTROYED.\n"
                   "To continue with this operation, click OK. To quit click CANCEL.",
                   QMessageBox.Warning, extra):
            return
        job = plan.build_job(sel, self.tools)
        self.log("")
        self.log("Format operation started")
        for line in plan.summary(sel, self.tools):
            self.log("  " + line)
        self._launch(job)

    def _download_uefi_ntfs(self) -> str | None:
        if not ask(self, APP_NAME,
                   "Booting an NTFS or exFAT drive through UEFI requires the small UEFI:NTFS boot image "
                   "from the Rufus project.",
                   QMessageBox.Question,
                   f"It is normally installed with the {APP_NAME} package. Download it now from GitHub "
                   f"(pbatard/rufus {uefintfs.RUFUS_VERSION}, verified by checksum)?"):
            return None
        dlg = QProgressDialog("Downloading UEFI:NTFS...", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)
        task = Task(lambda t: uefintfs.download(t.report), self)
        task.percent.connect(dlg.setValue)
        task.finished.connect(dlg.accept)
        task.start()
        dlg.exec()
        task.wait()
        if task.error:
            QMessageBox.critical(self, APP_NAME, f"Download failed: {task.error}")
            return None
        path = task.result if isinstance(task.result, str) else None
        if path:
            self.log(f"Downloaded UEFI:NTFS to {path}")
        return path

    def _on_save_drive(self) -> None:
        d = self.sel.drive
        if d is None or self.busy:
            return
        default = os.path.join(os.path.expanduser("~"), f"{(d.label or d.name).replace('/', '_')}.img")
        path, _ = QFileDialog.getSaveFileName(self, "Save drive to image", default,
                                              "Raw disk image (*.img);;All files (*)")
        if not path:
            return
        if not path.lower().endswith((".img", ".bin", ".raw")):
            path += ".img"
        free = os.statvfs(os.path.dirname(path))
        if free.f_bavail * free.f_frsize < d.size:
            if not ask(self, APP_NAME, f"There may not be enough free space for {human_size(d.size)}.",
                       QMessageBox.Warning, "Continue anyway?"):
                return
        job = {"protocol": HELPER_PROTOCOL, "action": "save",
               "device": {"path": d.path, "size": d.size, "serial": d.serial, "model": d.model},
               "output": os.path.abspath(path)}
        self.log("")
        self.log(f"Saving {d.path} to {path}")
        self._launch(job)

    def _launch(self, job: dict) -> None:
        self.set_busy(True)
        try:
            self.helper.start(job)
        except RuntimeError as exc:
            self.set_busy(False)
            QMessageBox.critical(self, APP_NAME, str(exc))

    # --------------------------------------------------------- helper events --

    def _on_helper_status(self, msg: str) -> None:
        self.status(msg)
        self.progress.setFormat(msg.rstrip("."))

    def _on_helper_progress(self, phase: str, done: int, total: int, msg: str) -> None:
        frac = done / total if total > 0 else 0.0
        self.progress.setValue(int(min(frac, 1.0) * 1000))
        self.progress.setFormat(f"{PHASES.get(phase, 'Working')}: {frac * 100:.1f}%")
        if msg:
            self.status(msg)

    def _on_helper_finished(self, ok: bool, cancelled: bool, msg: str) -> None:
        self.set_busy(False)
        elapsed = format_duration(self.elapsed.elapsed() / 1000) if self.elapsed.isValid() else ""
        if ok:
            self.progress.setValue(1000)
            self.progress.setFormat("READY")
            self.status(msg)
            self.log(f"{msg} (elapsed {elapsed})")
            QApplication.alert(self)
        else:
            self.progress.setValue(0)
            self.progress.setFormat("READY")
            self.log(("Cancelled: " if cancelled else "Error: ") + msg)
            self.status("Operation cancelled" if cancelled else "Error")
            if cancelled:
                QMessageBox.warning(self, APP_NAME, msg)
            else:
                QMessageBox.critical(self, APP_NAME, msg + "\n\nSee the log for details.")
        if self.close_after:
            self.close()
            return
        self.refresh_devices(force=True)

    # ---------------------------------------------------------- busy state --

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for w in (self.device_combo, self.save_btn, self.boot_combo, self.select_btn, self.hash_btn,
                  self.mode_combo, self.persist_row, self.scheme_combo, self.target_combo, self.adv_drive,
                  self.label_edit, self.fs_combo, self.cluster_combo, self.adv_format):
            w.setEnabled(not busy)
        self.start_btn.setEnabled(not busy)
        self.close_btn.setText("CANCEL" if busy else "CLOSE")
        if busy:
            self.progress.setValue(0)
            self.progress.setFormat("Starting...")
            self.elapsed.start()
            self.status_time.setText("00:00:00")
            self.clock.start()
        else:
            self.clock.stop()
            self.refresh_options()

    def _tick(self) -> None:
        if self.elapsed.isValid():
            self.status_time.setText(format_duration(self.elapsed.elapsed() / 1000))

    def _on_close_clicked(self) -> None:
        if self.busy:
            if ask(self, APP_NAME, "Cancel the current operation?", QMessageBox.Question,
                   "The drive will most likely be unusable until it is written again."):
                self.status("Cancelling...")
                self.helper.cancel()
        else:
            self.close()

    def closeEvent(self, event) -> None:
        if self.busy:
            if ask(self, APP_NAME, "An operation is in progress. Cancel it and quit?", QMessageBox.Warning):
                self.close_after = True
                self.helper.cancel()
            event.ignore()
            return
        self._save_settings()
        for t in list(self._tasks):
            t.cancelled = True
            t.wait(3000)
        if self.helper.proc is not None and self.helper.running:
            self.helper.proc.waitForFinished(5000)
        event.accept()
