"""Application entry point."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import traceback

from . import APP_ID, APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rufux", description=f"{APP_NAME}: bootable USB creator")
    parser.add_argument("image", nargs="?", help="image file to preselect")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    try:
        from .ui.qt import QApplication, QCoreApplication
    except ImportError as exc:
        from .core.distro import install_hint

        print(f"{APP_NAME} needs PyQt6 or PySide6 ({install_hint('qt')}): {exc}", file=sys.stderr)
        return 1

    from .ui.main_window import MainWindow
    from .ui.widgets import app_icon

    def excepthook(exc_type, exc, tb) -> None:
        # Report instead of aborting (PyQt6 aborts on unhandled exceptions in slots).
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = excepthook

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
