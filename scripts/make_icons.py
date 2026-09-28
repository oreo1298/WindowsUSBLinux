#!/usr/bin/env python3
"""Regenerate rufux/data/autorun.ico from rufux/data/rufux.svg (needs PySide6)."""
import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG = os.path.join(ROOT, "rufux", "data", "rufux.svg")
ICO = os.path.join(ROOT, "rufux", "data", "autorun.ico")
SIZES = (16, 24, 32, 48, 64, 256)


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def main() -> int:
    QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
    renderer = QSvgRenderer(SVG)
    pngs = [(s, render_png(renderer, s)) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    blobs = b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    with open(ICO, "wb") as f:
        f.write(header + entries + blobs)
    print(f"wrote {ICO} ({os.path.getsize(ICO)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
