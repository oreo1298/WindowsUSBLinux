"""Destructive bad block and counterfeit ("fake capacity") drive check.

Like Rufus' check, every pass writes a pattern over the whole drive and reads
it back.  Each 4 KiB block also carries its own index, so drives that
silently wrap around (fake capacity) are detected as well.
"""

from __future__ import annotations

import array
import os
import random
import struct

from ..core.util import human_size
from . import rawio
from .context import Context, HelperError

BLOCK = 4096
CHUNK = 1024 * 1024
PATTERNS = [0xAA, 0x55, 0xFF, 0x00]
MAX_REPORTED = 20


def _fill(buf, pattern: int, offset: int, key: int) -> None:
    n = len(buf)
    mv = memoryview(buf).cast("Q")
    first = offset // BLOCK
    count = n // BLOCK
    mv[0::BLOCK // 8] = array.array("Q", ((first + i) ^ key for i in range(count)))
    del mv


def run(ctx: Context, dev_path: str, size: int, passes: int) -> None:
    passes = max(1, min(passes, len(PATTERNS)))
    size = size // BLOCK * BLOCK
    fd, direct = rawio.open_device(dev_path, write=True)
    wbuf = rawio.aligned_buffer(CHUNK)
    rbuf = rawio.aligned_buffer(CHUNK)
    key = random.getrandbits(63)
    bad_blocks: set[int] = set()
    aliased = 0
    io_errors = 0
    total_work = size * 2 * passes
    work = 0
    try:
        for p in range(passes):
            pattern = PATTERNS[p]
            pkey = key ^ (p * 0x9E3779B97F4A7C15 & 0x7FFFFFFFFFFFFFFF)
            ctx.status(f"Bad blocks check: pass {p + 1} of {passes} (writing 0x{pattern:02X})")
            wbuf[:] = bytes([pattern]) * CHUNK
            for off in range(0, size, CHUNK):
                n = min(CHUNK, size - off)
                _fill(memoryview(wbuf)[:n], pattern, off, pkey)
                try:
                    rawio.pwrite_all(fd, memoryview(wbuf)[:n], off)
                except OSError:
                    io_errors += 1
                    bad_blocks.update(range(off // BLOCK, (off + n) // BLOCK))
                work += n
                ctx.progress("badblocks", work, total_work,
                             f"Bad blocks: pass {p + 1}/{passes}, writing {off * 100 // size}%")
                ctx.check()
            os.fsync(fd)
            rawio.flush_buffers(fd)
            ctx.status(f"Bad blocks check: pass {p + 1} of {passes} (reading back)")
            for off in range(0, size, CHUNK):
                n = min(CHUNK, size - off)
                _fill(memoryview(wbuf)[:n], pattern, off, pkey)
                try:
                    got = rawio.pread_into(fd, memoryview(rbuf)[:n], off)
                except OSError:
                    got = -1
                if got != n:
                    io_errors += 1
                    bad_blocks.update(range(off // BLOCK, (off + n) // BLOCK))
                elif rbuf[:n] != wbuf[:n]:
                    for b in range(0, n, BLOCK):
                        if rbuf[b:b + BLOCK] != wbuf[b:b + BLOCK]:
                            idx = (off + b) // BLOCK
                            bad_blocks.add(idx)
                            stored = struct.unpack_from("<Q", rbuf, b)[0] ^ pkey
                            if stored != idx and stored < size // BLOCK and \
                                    rbuf[b + 8:b + BLOCK] == wbuf[b + 8:b + BLOCK]:
                                aliased += 1
                work += n
                ctx.progress("badblocks", work, total_work,
                             f"Bad blocks: pass {p + 1}/{passes}, reading {off * 100 // size}%")
                ctx.check()
    finally:
        wbuf.close()
        rbuf.close()
        os.close(fd)
    if bad_blocks:
        first = sorted(bad_blocks)[:MAX_REPORTED]
        ctx.log(f"Bad blocks found: {len(bad_blocks)} x 4 KiB ({human_size(len(bad_blocks) * BLOCK)}); "
                f"read/write errors: {io_errors}; blocks holding data of another block: {aliased}")
        ctx.log("First bad block offsets: " + ", ".join(str(b * BLOCK) for b in first))
        if aliased:
            raise HelperError(
                f"This drive appears to be COUNTERFEIT: {aliased} blocks returned data that was written "
                "elsewhere, which means its real capacity is smaller than advertised. Do not use it.")
        raise HelperError(f"Check failed: {len(bad_blocks)} bad blocks "
                          f"({human_size(len(bad_blocks) * BLOCK)}) were found on this drive.")
    ctx.log(f"Bad blocks check: {passes} pass(es) completed, no bad blocks found")
