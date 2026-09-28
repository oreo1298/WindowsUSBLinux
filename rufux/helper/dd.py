"""Raw ("DD mode") image writing with optional decompression and verification."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import os
import queue
import subprocess
import threading
import zipfile

from ..core.util import human_size
from . import rawio
from .context import Cancelled, Context, HelperError

CHUNK = 4 * 1024 * 1024
NBUF = 4


class _Source:
    """Decompressing reader on top of an already-open file descriptor."""

    def __init__(self, ctx: Context, fd: int, compression: str | None):
        self.fd = fd
        self.compression = compression
        self.raw = os.fdopen(fd, "rb", buffering=0, closefd=True)
        self.file_size = os.fstat(fd).st_size
        self.proc: subprocess.Popen | None = None
        self.zip: zipfile.ZipFile | None = None
        self.uncompressed_size: int | None = None
        if compression is None:
            self.stream = self.raw
            self.uncompressed_size = self.file_size
        elif compression == "gz":
            self.stream = gzip.GzipFile(fileobj=self.raw, mode="rb")
        elif compression in ("xz", "lzma"):
            self.stream = lzma.LZMAFile(self.raw, "rb")
        elif compression == "bz2":
            self.stream = bz2.BZ2File(self.raw, "rb")
        elif compression == "zip":
            self.zip = zipfile.ZipFile(self.raw)
            members = [m for m in self.zip.infolist() if not m.is_dir()]
            if not members:
                raise HelperError("The zip archive is empty")
            member = max(members, key=lambda m: m.file_size)
            ctx.log(f"Using '{member.filename}' from the zip archive")
            self.uncompressed_size = member.file_size
            self.stream = self.zip.open(member)
        elif compression == "zst":
            stream = None
            try:
                from compression import zstd  # type: ignore[import-not-found]

                stream = zstd.ZstdFile(self.raw, "rb")
            except ImportError:
                pass
            if stream is None:
                exe = ctx.tool("zstd", package="zstd")
                self.proc = subprocess.Popen([exe, "-dc", "-q", "--no-progress"], stdin=fd,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stream = self.proc.stdout
            self.stream = stream
        else:
            raise HelperError(f"Unsupported compression '{compression}'")

    def position(self) -> int:
        """Bytes of the (compressed) input consumed so far."""
        try:
            return os.lseek(self.fd, 0, os.SEEK_CUR)
        except OSError:
            return 0

    def readinto(self, view: memoryview) -> int:
        total = 0
        while total < len(view):
            if hasattr(self.stream, "readinto"):
                n = self.stream.readinto(view[total:])
            else:
                data = self.stream.read(len(view) - total)
                n = len(data)
                view[total:total + n] = data
            if not n:
                break
            total += n
        return total

    def close(self) -> None:
        err = b""
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            _, err = self.proc.communicate()
        for obj in (self.stream, self.zip, self.raw):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        if self.proc is not None and self.proc.returncode not in (0, -9) and err:
            raise HelperError("zstd: " + err.decode("utf-8", "replace").strip())


def write_image(ctx: Context, dev_path: str, dev_size: int, image_fd: int, compression: str | None,
                verify: bool = True, sector_size: int = 512) -> int:
    """Write an image to a device. Returns the number of bytes written."""
    src = _Source(ctx, image_fd, compression)
    total_hint = src.uncompressed_size
    if total_hint is not None and total_hint > dev_size:
        src.close()
        raise HelperError(f"The image ({human_size(total_hint)}) is larger than the drive "
                          f"({human_size(dev_size)})")
    progress_total = src.uncompressed_size or src.file_size
    use_position = src.uncompressed_size is None
    try:
        fd, direct = rawio.open_device(dev_path, write=True)
    except OSError as exc:
        src.close()
        raise HelperError(f"Cannot open {dev_path} for writing: {exc.strerror}") from exc
    ctx.log(f"Writing to {dev_path} ({'direct I/O' if direct else 'buffered I/O'})")
    hasher = hashlib.sha256()
    free: queue.Queue = queue.Queue()
    full: queue.Queue = queue.Queue()
    buffers = [rawio.aligned_buffer(CHUNK) for _ in range(NBUF)]
    for b in buffers:
        free.put(b)
    stop = threading.Event()
    reader_error: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                buf = free.get()
                if buf is None:
                    return
                n = src.readinto(memoryview(buf))
                full.put((buf, n))
                if n < len(buf):
                    break
        except BaseException as exc:  # noqa: BLE001 - forwarded to the writer
            reader_error.append(exc)
        full.put(None)

    t = threading.Thread(target=reader, name="image-reader", daemon=True)
    t.start()
    written = 0
    try:
        while True:
            try:
                item = full.get(timeout=0.5)
            except queue.Empty:
                ctx.check()
                continue
            if item is None:
                if reader_error:
                    raise HelperError(f"Error while reading the image: {reader_error[0]}")
                break
            buf, n = item
            if n == 0:
                free.put(buf)
                break
            hasher.update(memoryview(buf)[:n])
            wlen = n
            if direct and n % sector_size:
                wlen = (n + sector_size - 1) // sector_size * sector_size
                memoryview(buf)[n:wlen] = bytes(wlen - n)
            if written + n > dev_size:
                raise HelperError("The image is larger than the drive")
            try:
                rawio.pwrite_all(fd, memoryview(buf)[:wlen], written)
            except OSError as exc:
                raise HelperError(f"Write error at offset {written}: {exc.strerror}") from exc
            written += n
            free.put(buf)
            done = src.position() if use_position else written
            ctx.progress("write", done, progress_total,
                         f"Writing image: {human_size(written)}" +
                         ("" if use_position else f" of {human_size(progress_total)}"))
            ctx.check()
            if n < len(buf):
                break
        ctx.status("Flushing data to the drive...")
        os.fsync(fd)
        ctx.progress("write", progress_total, progress_total, "Writing image: done", force=True)
    except (Cancelled, HelperError, OSError):
        stop.set()
        free.put(None)
        raise
    finally:
        stop.set()
        free.put(None)
        t.join(timeout=5)
        os.close(fd)
        try:
            src.close()
        finally:
            for b in buffers:
                b.close()
    digest = hasher.hexdigest()
    ctx.log(f"Wrote {written} bytes (SHA-256 {digest})")
    if verify:
        verify_device(ctx, dev_path, written, digest)
    return written


def verify_device(ctx: Context, dev_path: str, length: int, expected_sha256: str) -> None:
    ctx.status("Verifying written data...")
    fd, direct = rawio.open_device(dev_path, write=False)
    rawio.flush_buffers(fd)
    hasher = hashlib.sha256()
    buf = rawio.aligned_buffer(CHUNK)
    try:
        done = 0
        while done < length:
            want = min(CHUNK, length - done)
            rlen = (want + 4095) // 4096 * 4096 if direct else want
            n = rawio.pread_into(fd, memoryview(buf)[:rlen], done)
            if n < want:
                raise HelperError(f"Verification failed: could not read back offset {done + n}")
            hasher.update(memoryview(buf)[:want])
            done += want
            ctx.progress("verify", done, length, f"Verifying: {human_size(done)} of {human_size(length)}")
            ctx.check()
    finally:
        buf.close()
        os.close(fd)
    if hasher.hexdigest() != expected_sha256:
        raise HelperError("Verification FAILED: the data read back from the drive differs from the "
                          "image. The drive may be faulty or counterfeit.")
    ctx.log("Verification successful")
