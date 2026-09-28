"""Qt binding selection: PySide6 or PyQt6, whichever is installed.

PySide6 is tried first; set RUFUX_QT=pyqt6 (or pyside6) to force one.  The UI
code only uses fully scoped enums (e.g. Qt.AlignmentFlag.AlignCenter), which
both bindings accept.
"""

# ruff: noqa: F401, F403
from __future__ import annotations

import os

_order = ["pyside6", "pyqt6"]
_forced = os.environ.get("RUFUX_QT", "").strip().lower()
if _forced in _order:
    _order.remove(_forced)
    _order.insert(0, _forced)

API = ""
_errors: list[str] = []
for _name in _order:
    try:
        if _name == "pyside6":
            from PySide6 import QtCore, QtGui, QtWidgets
            from PySide6.QtCore import *
            from PySide6.QtGui import *
            from PySide6.QtWidgets import *

            Signal = QtCore.Signal
        else:
            from PyQt6 import QtCore, QtGui, QtWidgets
            from PyQt6.QtCore import *
            from PyQt6.QtGui import *
            from PyQt6.QtWidgets import *

            Signal = QtCore.pyqtSignal
        API = _name
        break
    except ImportError as exc:
        _errors.append(f"{_name}: {exc}")
else:
    raise ImportError("Neither PySide6 nor PyQt6 could be imported (" + "; ".join(_errors) + ")")


def binding_description() -> str:
    return f"{'PySide6' if API == 'pyside6' else 'PyQt6'} with Qt {QtCore.qVersion()}"


def dialog_accepted(dialog) -> bool:
    """Run a modal dialog; True if it was accepted (QDialog::Accepted == 1)."""
    return int(dialog.exec()) == 1
