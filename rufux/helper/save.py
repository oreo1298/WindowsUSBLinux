"""Save a drive's contents to an image file (Rufus' "save drive to image")."""

from __future__ import annotations

import os

from ..core.util import human_size
from . import rawio
from .context import Context, HelperError

CHUNK = 4 * 1024 * 1024


def save_drive(ctx: Context, dev_path: str, size: int, out_fd: int) -> None:
    fd, direct = rawio.open_device(dev_path, write=False, exclusive=False)
    buf = rawio.aligned_buffer(CHUNK)
    try:
        done = 0
        while done < size:
            want = min(CHUNK, size - done)
            n = rawio.pread_into(fd, memoryview(buf)[:want], done)
            if n != want:
                raise HelperError(f"Read error at offset {done + n}")
            with memoryview(buf) as mv:
                pos = 0
                while pos < n:
                    pos += os.write(out_fd, mv[pos:n])
            done += n
            ctx.progress("save", done, size, f"Saving drive: {human_size(done)} of {human_size(size)}")
            ctx.check()
        os.fsync(out_fd)
    except OSError as exc:
        raise HelperError(f"I/O error while saving the drive: {exc.strerror}") from exc
    finally:
        buf.close()
        os.close(fd)
    ctx.log(f"Saved {size} bytes")
