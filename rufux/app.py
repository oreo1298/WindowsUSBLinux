"""Application entry point."""

from __future__ import annotations

import argparse
import os
import signal
import sys

from . import APP_ID, APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rufux", description=f"{APP_NAME}: bootable USB creator")
    parser.add_argument("image", nargs="?", help="image file to preselect")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print(f"{APP_NAME} needs PySide6 (sudo pacman -S pyside6): {exc}", file=sys.stderr)
        return 1

    from .ui.main_window import MainWindow
    from .ui.widgets import app_icon

    QCoreApplication.setOrganizationName("rufux")
    QCoreApplication.setApplicationName("rufux")
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    image = os.path.abspath(args.image) if args.image else None
    win = MainWindow(image)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
