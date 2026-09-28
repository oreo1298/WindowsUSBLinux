"""Raw block device I/O: O_DIRECT with aligned buffers, ioctls, zero fill."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import mmap
import os
import time

BLKRRPART = 0x125F
BLKFLSBUF = 0x1261

ALIGN = 4096


def aligned_buffer(size: int) -> mmap.mmap:
    """Anonymous mmap: page aligned, as O_DIRECT requires."""
    size = (size + ALIGN - 1) // ALIGN * ALIGN
    return mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)


def open_device(path: str, write: bool, direct: bool = True, exclusive: bool = True) -> tuple[int, bool]:
    """Open a block device; returns (fd, is_direct)."""
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC
    if exclusive:
        flags |= os.O_EXCL
    if direct:
        try:
            return os.open(path, flags | os.O_DIRECT), True
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
    return os.open(path, flags), False


def flush_buffers(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, BLKFLSBUF, 0)


def reread_partitions(path: str, retries: int = 10) -> bool:
    """Ask the kernel to re-read the partition table (BLKRRPART)."""
    for _ in range(retries):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            time.sleep(0.3)
            continue
        try:
            fcntl.ioctl(fd, BLKRRPART, 0)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EAGAIN):
                return False
        finally:
            os.close(fd)
        time.sleep(0.5)
    return False


def pwrite_all(fd: int, data, offset: int) -> None:
    view = memoryview(data)
    while len(view):
        n = os.pwrite(fd, view, offset)
        if n <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[n:]
        offset += n


def pread_into(fd: int, buf, offset: int) -> int:
    """Fill `buf` from `offset`; returns bytes read (short only at EOF)."""
    view = memoryview(buf)
    total = 0
    while total < len(view):
        n = os.preadv(fd, [view[total:]], offset + total)
        if n == 0:
            break
        total += n
    return total


def zero_range(fd: int, offset: int, length: int, direct: bool,
               progress=None, check=None, chunk: int = 8 * 1024 * 1024) -> None:
    """Write zeros over [offset, offset+length)."""
    buf = aligned_buffer(min(chunk, max(length, ALIGN)))
    try:
        done = 0
        while done < length:
            n = min(len(buf), length - done)
            if direct and n % 512:
                n = (n + 511) // 512 * 512
            pwrite_all(fd, memoryview(buf)[:n], offset + done)
            done += n
            if progress:
                progress(min(done, length), length)
            if check:
                check()
        os.fsync(fd)
    finally:
        buf.close()
