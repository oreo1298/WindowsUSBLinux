"""Small reusable widgets and helpers for the Rufus-like look."""

from __future__ import annotations

import os
import traceback
from typing import Callable

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy, QStyle, QToolButton,
                               QVBoxLayout, QWidget)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def app_icon() -> QIcon:
    from .. import APP_ID

    icon = QIcon.fromTheme(APP_ID)
    if icon.isNull():
        icon = QIcon(os.path.join(DATA_DIR, "rufux.svg"))
    return icon


def theme_icon(widget: QWidget, names: tuple[str, ...], fallback: QStyle.StandardPixmap) -> QIcon:
    for n in names:
        icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    return widget.style().standardIcon(fallback)


class SectionHeader(QWidget):
    """Bold title followed by a horizontal rule, like Rufus' group titles."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        label = QLabel(title)
        font = QFont(label.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.25)
        label.setFont(font)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay.addWidget(label)
        lay.addWidget(line, 1)


class Collapsible(QWidget):
    """'Show advanced ... properties' toggle with a hideable body."""

    toggled = Signal(bool)

    def __init__(self, show_text: str, hide_text: str, parent=None):
        super().__init__(parent)
        self.show_text, self.hide_text = show_text, hide_text
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.expanded = False
        self.button = QToolButton()
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setAutoRaise(True)
        self.button.setArrowType(Qt.RightArrow)
        self.button.setText(show_text)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self._on_toggle(not self.expanded))
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(18, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self.body.setVisible(False)
        lay.addWidget(self.button)
        lay.addWidget(self.body)

    def _on_toggle(self, checked: bool) -> None:
        self.expanded = checked
        self.button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.button.setText(self.hide_text if checked else self.show_text)
        self.body.setVisible(checked)
        self.toggled.emit(checked)

    def set_expanded(self, expanded: bool) -> None:
        if expanded != self.expanded:
            self._on_toggle(expanded)

    def add_widget(self, w: QWidget) -> None:
        self.body_layout.addWidget(w)


class Task(QThread):
    """Run `fn(task)` in a thread.  Connect its signals to *bound methods of QObjects*
    (not lambdas) so that Qt queues them to the GUI thread."""

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object, object)
    percent = Signal(int)

    def __init__(self, fn: Callable[["Task"], object], parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancelled = False
        self.result: object = None
        self.error: str | None = None
        self.context: dict = {}

    def report(self, done, total) -> None:
        self.progress.emit(done, total)
        self.percent.emit(int(done * 100 / total) if total else 0)

    def run(self) -> None:
        try:
            self.result = self.fn(self)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            traceback.print_exc()
            self.error = str(exc) or exc.__class__.__name__
            self.failed.emit(self.error)
            return
        self.done.emit(self.result)


def small_button(text: str = "", tooltip: str = "", icon: QIcon | None = None) -> QToolButton:
    b = QToolButton()
    if icon is not None and not icon.isNull():
        b.setIcon(icon)
        b.setIconSize(QSize(18, 18))
    else:
        b.setText(text)
    b.setToolTip(tooltip)
    b.setAutoRaise(False)
    return b
