"""Read-only access to ISO 9660 (with Joliet / Rock Ridge) and UDF images.

This is a small, dependency-free reader.  The GUI uses it to analyse an image
and the privileged helper uses it to extract files onto the target drive, the
same way Rufus uses libcdio instead of mounting the image.

Supported:
  * ISO 9660 levels 1-3, multi-extent files, Joliet (UCS-2) names,
    Rock Ridge names, symlinks, modes, deep-directory relocation (CL/RE/PL)
    and SUSP continuation areas.
  * UDF 1.02 - 2.60 including type 1, sparable and metadata partitions,
    short/long allocation descriptors, allocation extent continuation,
    embedded (in-ICB) data, extended file entries and symlinks.
  * El Torito boot catalog parsing.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator

SECTOR = 2048
MAX_DEPTH = 64
MAX_ENTRIES = 2_000_000


class IsoError(Exception):
    """Raised when an image cannot be parsed."""


@dataclass
class Extent:
    offset: int | None  # absolute byte offset in the image, None => zeros
    length: int


@dataclass
class Entry:
    path: str  # '/'-separated, relative to the root ('' for the root itself)
    name: str
    is_dir: bool
    size: int = 0
    extents: list[Extent] = field(default_factory=list)
    inline: bytes | None = None
    symlink: str | None = None
    mtime: float | None = None
    mode: int | None = None
    ref: object = None  # file-system specific directory reference

    @property
    def is_symlink(self) -> bool:
        return self.symlink is not None


@dataclass
class BootEntry:
    platform: int  # 0 = x86 BIOS, 0xEF = UEFI
    bootable: bool
    media_type: int
    load_rba: int
    sector_count: int

    @property
    def is_efi(self) -> bool:
        return self.platform == 0xEF


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


class _Source:
    """Thread-safe positional reads on top of a file descriptor."""

    def __init__(self, path_or_file: str | os.PathLike | BinaryIO):
        if isinstance(path_or_file, (str, os.PathLike)):
            self._file = open(path_or_file, "rb")
            self._owned = True
        else:
            self._file = path_or_file
            self._owned = False
        self.fd = self._file.fileno()
        st = os.fstat(self.fd)
        size = st.st_size
        if size == 0:
            try:
                size = os.lseek(self.fd, 0, os.SEEK_END)
            except OSError:
                size = 0
        self.size = size

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        chunks = []
        remaining = length
        while remaining > 0:
            data = os.pread(self.fd, remaining, offset)
            if not data:
                break
            chunks.append(data)
            offset += len(data)
            remaining -= len(data)
        out = b"".join(chunks)
        if len(out) < length:
            raise IsoError(f"Unexpected end of image at offset {offset}")
        return out

    def close(self) -> None:
        if self._owned:
            self._file.close()


# ---------------------------------------------------------------------------
# ISO 9660
# ---------------------------------------------------------------------------


def _iso_datetime(b: bytes) -> float | None:
    """Decode a 7-byte ISO 9660 directory record timestamp."""
    if len(b) < 7 or b[1] == 0:
        return None
    try:
        tz = struct.unpack("b", b[6:7])[0] * 15 * 60
        tm = (1900 + b[0], b[1], b[2], b[3], b[4], b[5], 0, 0, 0)
        return time.mktime(tm) - time.timezone - tz  # interpret as UTC
    except (OverflowError, ValueError):
        return None


@dataclass
class _IsoRecord:
    lba: int
    size: int
    flags: int
    name_raw: bytes
    system_use: bytes
    mtime: float | None


class Iso9660FS:
    """ISO 9660 file system, optionally using Rock Ridge or Joliet names."""

    kind = "iso9660"

    def __init__(self, src: _Source, use_joliet: bool | None = None):
        self.src = src
        self.pvd: bytes | None = None
        self.joliet_svd: bytes | None = None
        self.boot_catalog_lba: int | None = None
        sector = 16
        while sector < 16 + 128:
            data = src.read(sector * SECTOR, SECTOR)
            if data[1:6] != b"CD001":
                break
            vtype = data[0]
            if vtype == 0 and data[7:30] == b"EL TORITO SPECIFICATION":
                self.boot_catalog_lba = _u32(data, 71)
            elif vtype == 1 and self.pvd is None:
                self.pvd = data
            elif vtype == 2 and data[88:91] in (b"%/@", b"%/C", b"%/E"):
                if self.joliet_svd is None:
                    self.joliet_svd = data
            elif vtype == 255:
                break
            sector += 1
        if self.pvd is None:
            raise IsoError("No ISO 9660 primary volume descriptor")
        self.block_size = _u16(self.pvd, 128) or SECTOR
        if self.block_size not in (512, 1024, 2048):
            raise IsoError(f"Unsupported ISO 9660 block size {self.block_size}")
        self.volume_space = _u32(self.pvd, 80) * self.block_size
        self.volume_id = self.pvd[40:72].decode("ascii", "replace").rstrip(" \x00")
        self.joliet_volume_id = ""
        if self.joliet_svd is not None:
            self.joliet_volume_id = (
                self.joliet_svd[40:72].decode("utf-16-be", "replace").rstrip(" \x00")
            )
        self._pvd_root = self._parse_record(self.pvd[156:156 + 34])
        # Rock Ridge detection: SUSP "SP" entry in the root's "." record.
        self.susp_skip = 0
        self.rock_ridge = False
        try:
            recs = self._read_records(self._pvd_root.lba, self._pvd_root.size, limit=1)
            if recs:
                su = recs[0].system_use
                if len(su) >= 7 and su[0:2] == b"SP" and su[4:6] == b"\xbe\xef":
                    self.susp_skip = su[6]
                    self.rock_ridge = self._detect_rr(su)
        except IsoError:
            pass
        if use_joliet is None:
            use_joliet = (not self.rock_ridge) and self.joliet_svd is not None
        self.use_joliet = bool(use_joliet and self.joliet_svd is not None)
        if self.use_joliet:
            assert self.joliet_svd is not None
            self._root = self._parse_record(self.joliet_svd[156:156 + 34])
        else:
            self._root = self._pvd_root

    # -- low level -----------------------------------------------------

    @staticmethod
    def _parse_record(rec: bytes) -> _IsoRecord:
        rec_len = rec[0]
        ext_attr = rec[1]
        lba = _u32(rec, 2) + ext_attr
        size = _u32(rec, 10)
        flags = rec[25]
        name_len = rec[32]
        name_raw = rec[33:33 + name_len]
        su_start = 33 + name_len + (1 if name_len % 2 == 0 else 0)
        system_use = rec[su_start:rec_len] if rec_len > su_start else b""
        return _IsoRecord(lba, size, flags, name_raw, system_use, _iso_datetime(rec[18:25]))

    def _read_records(self, lba: int, size: int, limit: int | None = None) -> list[_IsoRecord]:
        bs = self.block_size
        if size > 64 * 1024 * 1024:
            raise IsoError("Directory too large")
        data = self.src.read(lba * bs, max(size, 0))
        out: list[_IsoRecord] = []
        pos = 0
        while pos < len(data):
            rec_len = data[pos]
            if rec_len == 0:
                pos = (pos // bs + 1) * bs
                continue
            if rec_len < 34 or pos + rec_len > len(data):
                break
            out.append(self._parse_record(data[pos:pos + rec_len]))
            if limit is not None and len(out) >= limit:
                break
            pos += rec_len
        return out

    def _susp_entries(self, su: bytes, skip: int) -> Iterator[tuple[bytes, bytes]]:
        """Yield (signature, entry bytes) from a system use area, following CE."""
        areas = [su[skip:]]
        seen_ce = 0
        while areas:
            area = areas.pop(0)
            pos = 0
            while pos + 4 <= len(area):
                sig = area[pos:pos + 2]
                length = area[pos + 2]
                if length < 4 or pos + length > len(area):
                    break
                entry = area[pos:pos + length]
                if sig == b"ST":
                    break
                if sig == b"CE" and length >= 28 and seen_ce < 64:
                    seen_ce += 1
                    ce_lba = _u32(entry, 4)
                    ce_off = _u32(entry, 12)
                    ce_len = _u32(entry, 20)
                    try:
                        areas.append(self.src.read(ce_lba * self.block_size + ce_off, ce_len))
                    except IsoError:
                        pass
                else:
                    yield sig, entry
                pos += length

    def _detect_rr(self, root_su: bytes) -> bool:
        return any(sig in (b"ER", b"RR", b"PX", b"NM")
                   for sig, _ in self._susp_entries(root_su, 0))

    def _decode_plain(self, raw: bytes) -> str:
        if self.use_joliet:
            name = raw.decode("utf-16-be", "replace")
        else:
            name = raw.decode("latin-1")
        if ";" in name:
            name = name.split(";", 1)[0]
        if not self.use_joliet:
            if name.endswith(".") and len(name) > 1:
                name = name[:-1]
            name = name.lower()
        return name

    # -- directory listing -----------------------------------------------

    def root(self) -> Entry:
        return Entry("", "", True, self._root.size, [Extent(self._root.lba * self.block_size, self._root.size)],
                     ref=(self._root.lba, self._root.size))

    def listdir(self, dir_entry: Entry) -> list[Entry]:
        lba, size = dir_entry.ref  # type: ignore[misc]
        records = self._read_records(lba, size)
        entries: list[Entry] = []
        pending: _IsoRecord | None = None
        pending_extents: list[Extent] = []
        pending_size = 0
        rr = self.rock_ridge and not self.use_joliet
        for rec in records:
            if rec.name_raw in (b"\x00", b"\x01"):
                continue
            if pending is not None and pending.name_raw != rec.name_raw:
                # Malformed multi-extent chain; flush what we have.
                entries.append(self._make_entry(dir_entry, pending, pending_extents, pending_size, rr))
                pending, pending_extents, pending_size = None, [], 0
            if pending is None:
                pending = rec
            pending_extents.append(Extent(rec.lba * self.block_size, rec.size))
            pending_size += rec.size
            if rec.flags & 0x80:  # multi-extent: more records follow
                continue
            ent = self._make_entry(dir_entry, pending, pending_extents, pending_size, rr)
            if ent is not None:
                entries.append(ent)
            pending, pending_extents, pending_size = None, [], 0
        if pending is not None:
            ent = self._make_entry(dir_entry, pending, pending_extents, pending_size, rr)
            if ent is not None:
                entries.append(ent)
        entries = [e for e in entries if e is not None]
        if rr and dir_entry.path == "":
            # Hide the Rock Ridge relocation directory once its (relocated)
            # contents have been filtered out, as the kernel effectively does.
            entries = [e for e in entries
                       if not (e.is_dir and e.name in ("rr_moved", ".rr_moved") and not self.listdir(e))]
        return entries

    def _make_entry(self, parent: Entry, rec: _IsoRecord, extents: list[Extent], size: int,
                    rr: bool) -> Entry | None:
        is_dir = bool(rec.flags & 0x02)
        name = None
        symlink = None
        mode = None
        child_link = None
        if rr and rec.system_use:
            name_parts: list[str] = []
            sl_parts: list[str] = []
            sl_absolute = False
            sl_pending = ""
            for sig, e in self._susp_entries(rec.system_use, self.susp_skip):
                if sig == b"NM" and len(e) >= 5:
                    flags = e[4]
                    if flags & 0x02:
                        name_parts = ["."]
                    elif flags & 0x04:
                        name_parts = [".."]
                    else:
                        name_parts.append(e[5:].decode("utf-8", "surrogateescape"))
                elif sig == b"PX" and len(e) >= 12:
                    mode = _u32(e, 4)
                elif sig == b"SL" and len(e) >= 5:
                    pos = 5
                    while pos + 2 <= len(e):
                        cflags, clen = e[pos], e[pos + 1]
                        content = e[pos + 2:pos + 2 + clen].decode("utf-8", "surrogateescape")
                        pos += 2 + clen
                        if cflags & 0x08:
                            sl_absolute = True
                            continue
                        if cflags & 0x02:
                            content = "."
                        elif cflags & 0x04:
                            content = ".."
                        sl_pending += content
                        if not cflags & 0x01:
                            sl_parts.append(sl_pending)
                            sl_pending = ""
                elif sig == b"CL" and len(e) >= 12:
                    child_link = _u32(e, 4)
                elif sig == b"RE":
                    return None  # relocated directory; reached through its CL entry
            if name_parts:
                name = "".join(name_parts)
            if mode is not None and (mode & 0o170000) == 0o120000:
                if sl_pending:
                    sl_parts.append(sl_pending)
                symlink = ("/" if sl_absolute else "") + "/".join(sl_parts)
        if name is None:
            name = self._decode_plain(rec.name_raw)
        if not name or name in (".", "..") or "/" in name or "\x00" in name:
            return None
        path = f"{parent.path}/{name}" if parent.path else name
        if child_link is not None:
            # Deep directory relocated elsewhere: read its "." record for the size.
            recs = self._read_records(child_link, self.block_size, limit=1)
            dsize = recs[0].size if recs else self.block_size
            return Entry(path, name, True, dsize, [Extent(child_link * self.block_size, dsize)],
                         mtime=rec.mtime, mode=mode, ref=(child_link, dsize))
        if symlink is not None:
            return Entry(path, name, False, 0, [], symlink=symlink, mtime=rec.mtime, mode=mode)
        if is_dir:
            return Entry(path, name, True, rec.size, extents, mtime=rec.mtime, mode=mode,
                         ref=(rec.lba, rec.size))
        return Entry(path, name, False, size, extents, mtime=rec.mtime, mode=mode)

    # -- El Torito ------------------------------------------------------

    def boot_entries(self) -> list[BootEntry]:
        if self.boot_catalog_lba is None:
            return []
        try:
            cat = self.src.read(self.boot_catalog_lba * self.block_size, SECTOR)
        except IsoError:
            return []
        if cat[0] != 0x01 or cat[30:32] != b"\x55\xaa":
            return []
        out: list[BootEntry] = []
        platform = cat[1]
        e = cat[32:64]
        out.append(BootEntry(platform, e[0] == 0x88, e[1], _u32(e, 8), _u16(e, 6)))
        pos = 64
        while pos + 32 <= len(cat):
            hdr = cat[pos]
            if hdr not in (0x90, 0x91):
                break
            platform = cat[pos + 1]
            count = _u16(cat, pos + 2)
            pos += 32
            for _ in range(count):
                if pos + 32 > len(cat):
                    break
                e = cat[pos:pos + 32]
                if e[0] in (0x88, 0x00):
                    out.append(BootEntry(platform, e[0] == 0x88, e[1], _u32(e, 8), _u16(e, 6)))
                pos += 32
                # skip section entry extensions
                while pos + 32 <= len(cat) and cat[pos] == 0x44:
                    pos += 32
            if hdr == 0x91:
                break
        return out

    @property
    def label(self) -> str:
        if self.use_joliet and self.joliet_volume_id:
            return self.joliet_volume_id
        return self.volume_id


# ---------------------------------------------------------------------------
# UDF
# ---------------------------------------------------------------------------

TAG_PVD, TAG_AVDP, TAG_VDP, TAG_IUVD, TAG_PD, TAG_LVD, TAG_USD, TAG_TD = 1, 2, 3, 4, 5, 6, 7, 8
TAG_FSD, TAG_FID, TAG_AED, TAG_IE, TAG_TE, TAG_FE, TAG_EFE = 256, 257, 258, 259, 260, 261, 266


def _cs0(raw: bytes) -> str:
    """Decode an OSTA Compressed Unicode string."""
    if not raw:
        return ""
    comp = raw[0]
    body = raw[1:]
    if comp == 8:
        return body.decode("latin-1")
    if comp == 16:
        if len(body) % 2:
            body = body[:-1]
        return body.decode("utf-16-be", "replace")
    if comp in (254, 255):
        return ""
    raise IsoError(f"Unknown OSTA CS0 compression id {comp}")


def _dstring(field_bytes: bytes) -> str:
    if not field_bytes:
        return ""
    length = field_bytes[-1]
    if length == 0 or length > len(field_bytes) - 1:
        return ""
    try:
        return _cs0(field_bytes[:length]).rstrip("\x00 ")
    except IsoError:
        return ""


def _udf_time(b: bytes) -> float | None:
    if len(b) < 12:
        return None
    type_tz, year = struct.unpack_from("<Hh", b, 0)
    month, day, hour, minute, second = b[4], b[5], b[6], b[7], b[8]
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    tz = type_tz & 0x0FFF
    if tz & 0x0800:
        tz -= 0x1000
    if tz == -2047:
        tz = 0
    try:
        return time.mktime((year, month, day, hour, minute, second, 0, 0, 0)) - time.timezone - tz * 60
    except (OverflowError, ValueError):
        return None


def _check_tag(data: bytes, expected: int | tuple[int, ...] | None = None) -> int:
    if len(data) < 16:
        raise IsoError("Short UDF descriptor")
    tag_id = _u16(data, 0)
    checksum = (sum(data[0:4]) + sum(data[5:16])) & 0xFF
    if checksum != data[4]:
        raise IsoError(f"Bad UDF tag checksum (tag {tag_id})")
    if expected is not None:
        exp = expected if isinstance(expected, tuple) else (expected,)
        if tag_id not in exp:
            raise IsoError(f"Unexpected UDF tag {tag_id}, wanted {exp}")
    return tag_id


@dataclass
class _Partition:
    kind: str  # "phys" or "meta"
    number: int  # partition number (from the partition descriptor)
    meta_file_loc: int = 0
    meta_extents: list[tuple[int, int]] = field(default_factory=list)  # (phys lbn, n blocks)


class UdfFS:
    kind = "udf"

    def __init__(self, src: _Source):
        self.src = src
        if not self._has_vrs():
            raise IsoError("No UDF volume recognition sequence")
        avdp = None
        candidates = [256 * SECTOR]
        if src.size >= 512 * SECTOR:
            last = src.size // SECTOR - 1
            candidates += [last * SECTOR, (last - 256) * SECTOR]
        for off in candidates:
            try:
                data = src.read(off, SECTOR)
                _check_tag(data, TAG_AVDP)
                avdp = data
                break
            except IsoError:
                continue
        if avdp is None:
            raise IsoError("No UDF anchor volume descriptor pointer")
        self.pd_starts: dict[int, int] = {}
        self.lvd: bytes | None = None
        self.pvd_label = ""
        main_len, main_loc = _u32(avdp, 16), _u32(avdp, 20)
        res_len, res_loc = _u32(avdp, 24), _u32(avdp, 28)
        try:
            self._read_vds(main_loc, main_len)
        except IsoError:
            self._read_vds(res_loc, res_len)
        if self.lvd is None or not self.pd_starts:
            self._read_vds(res_loc, res_len)
        if self.lvd is None or not self.pd_starts:
            raise IsoError("Incomplete UDF volume descriptor sequence")
        lvd = self.lvd
        self.block_size = _u32(lvd, 212)
        if self.block_size != SECTOR:
            raise IsoError(f"Unsupported UDF block size {self.block_size}")
        self.lv_label = _dstring(lvd[84:84 + 128])
        self.domain = lvd[216 + 1:216 + 24].rstrip(b"\x00").decode("ascii", "replace")
        self.udf_revision = _u16(lvd, 216 + 24)
        n_maps = _u32(lvd, 268)
        pos = 440
        self.partitions: list[_Partition] = []
        for _ in range(n_maps):
            if pos + 2 > len(lvd):
                break
            mtype, mlen = lvd[pos], lvd[pos + 1]
            m = lvd[pos:pos + mlen]
            if mtype == 1 and mlen >= 6:
                self.partitions.append(_Partition("phys", _u16(m, 4)))
            elif mtype == 2 and mlen >= 64:
                ident = m[5:28].rstrip(b"\x00")
                pnum = _u16(m, 38)
                if ident.startswith(b"*UDF Metadata Partition"):
                    self.partitions.append(_Partition("meta", pnum, meta_file_loc=_u32(m, 40)))
                elif ident.startswith(b"*UDF Sparable Partition"):
                    # Sparing only matters for defective media; treat as physical.
                    self.partitions.append(_Partition("phys", pnum))
                else:
                    raise IsoError(f"Unsupported UDF partition map {ident!r}")
            else:
                raise IsoError(f"Unsupported UDF partition map type {mtype}")
            pos += max(mlen, 2)
        if not self.partitions:
            raise IsoError("No UDF partition maps")
        # Resolve metadata partitions.
        for idx, part in enumerate(self.partitions):
            if part.kind == "meta":
                phys_ref = self._phys_ref_for(part.number)
                fe = self._read_block(phys_ref, part.meta_file_loc)
                info = self._parse_fe(fe, phys_ref)
                exts: list[tuple[int, int]] = []
                for ext in info["ads"]:
                    etype, elen, eloc, epart = ext
                    if etype in (0, 1):
                        exts.append((eloc, (elen + SECTOR - 1) // SECTOR))
                part.meta_extents = exts
        # File set descriptor
        fsd_len = _u32(lvd, 248) & 0x3FFFFFFF
        fsd_loc = _u32(lvd, 252)
        fsd_part = _u16(lvd, 256)
        fsd = self._read_block(fsd_part, fsd_loc)
        _check_tag(fsd, TAG_FSD)
        del fsd_len
        root_len = _u32(fsd, 400) & 0x3FFFFFFF
        del root_len
        self.root_icb = (_u16(fsd, 408), _u32(fsd, 404))  # (partref, lbn)
        self.fs_label = _dstring(fsd[304:304 + 32]) if len(fsd) >= 336 else ""

    # -- volume structures ---------------------------------------------

    def _has_vrs(self) -> bool:
        for sector in range(16, 16 + 64):
            try:
                data = self.src.read(sector * SECTOR, 8)
            except IsoError:
                return False
            ident = data[1:6]
            if ident in (b"NSR02", b"NSR03"):
                return True
            if ident not in (b"CD001", b"BEA01", b"TEA01", b"CDW02", b"BOOT2", b"NSR02", b"NSR03"):
                if data[0:1] != b"\x00" or ident == b"\x00" * 5:
                    return False
        return False

    def _read_vds(self, loc: int, length: int, depth: int = 0) -> None:
        if depth > 8:
            return
        for i in range(max(length // SECTOR, 0)):
            data = self.src.read((loc + i) * SECTOR, SECTOR)
            try:
                tag = _check_tag(data)
            except IsoError:
                continue
            if tag == TAG_PD:
                self.pd_starts[_u16(data, 22)] = _u32(data, 188)
            elif tag == TAG_LVD and self.lvd is None:
                self.lvd = data
            elif tag == TAG_PVD and not self.pvd_label:
                self.pvd_label = _dstring(data[24:24 + 32])
            elif tag == TAG_VDP:
                self._read_vds(_u32(data, 24), _u32(data, 20), depth + 1)
                return
            elif tag == TAG_TD:
                return

    def _phys_ref_for(self, number: int) -> int:
        for idx, p in enumerate(self.partitions):
            if p.kind == "phys" and p.number == number:
                return idx
        raise IsoError(f"No physical partition map for partition {number}")

    def _map(self, partref: int, lbn: int, length: int) -> list[Extent]:
        """Map a partition-relative extent to absolute image extents."""
        if partref >= len(self.partitions):
            raise IsoError(f"Bad UDF partition reference {partref}")
        part = self.partitions[partref]
        if part.kind == "phys":
            start = self.pd_starts.get(part.number)
            if start is None:
                raise IsoError(f"Unknown UDF partition {part.number}")
            return [Extent((start + lbn) * SECTOR, length)]
        # metadata partition: walk the metadata file's extents
        phys_start = self.pd_starts.get(part.number)
        if phys_start is None:
            raise IsoError(f"Unknown UDF partition {part.number}")
        out: list[Extent] = []
        remaining = length
        cur = lbn
        while remaining > 0:
            base = 0
            for ploc, nblocks in part.meta_extents:
                if base <= cur < base + nblocks:
                    avail_blocks = base + nblocks - cur
                    take = min(remaining, avail_blocks * SECTOR)
                    out.append(Extent((phys_start + ploc + (cur - base)) * SECTOR, take))
                    remaining -= take
                    cur += (take + SECTOR - 1) // SECTOR
                    break
                base += nblocks
            else:
                raise IsoError("Metadata partition block out of range")
        return out

    def _read_block(self, partref: int, lbn: int) -> bytes:
        ext = self._map(partref, lbn, SECTOR)[0]
        assert ext.offset is not None
        return self.src.read(ext.offset, SECTOR)

    def _read_extents(self, extents: list[Extent], limit: int = 64 * 1024 * 1024) -> bytes:
        total = sum(e.length for e in extents)
        if total > limit:
            raise IsoError("UDF structure too large")
        parts = []
        for e in extents:
            if e.offset is None:
                parts.append(b"\x00" * e.length)
            else:
                parts.append(self.src.read(e.offset, e.length))
        return b"".join(parts)

    # -- ICBs ---------------------------------------------------------

    def _parse_fe(self, data: bytes, partref: int) -> dict:
        tag = _check_tag(data, (TAG_FE, TAG_EFE))
        file_type = data[16 + 11]
        icb_flags = _u16(data, 16 + 18)
        ad_type = icb_flags & 0x7
        info_len = _u64(data, 56)
        if tag == TAG_FE:
            mtime = _udf_time(data[84:96])
            l_ea, l_ad = _u32(data, 168), _u32(data, 172)
            ad_start = 176 + l_ea
        else:
            mtime = _udf_time(data[92:104])
            l_ea, l_ad = _u32(data, 208), _u32(data, 212)
            ad_start = 216 + l_ea
        perms = _u32(data, 44)
        if ad_start + l_ad > len(data):
            raise IsoError("Bad UDF file entry allocation descriptor length")
        ad_area = data[ad_start:ad_start + l_ad]
        result = {"type": file_type, "size": info_len, "mtime": mtime, "inline": None, "ads": [],
                  "perms": perms}
        if ad_type == 3:
            result["inline"] = ad_area[:info_len]
            return result
        result["ads"] = self._parse_ads(ad_area, ad_type, partref)
        return result

    def _parse_ads(self, area: bytes, ad_type: int, partref: int, depth: int = 0) -> list[tuple]:
        """Return list of (type, length, lbn, partref), following continuations."""
        out: list[tuple] = []
        if depth > 1024:
            raise IsoError("Too many UDF allocation extent continuations")
        if ad_type == 0:
            size = 8
        elif ad_type == 1:
            size = 16
        elif ad_type == 2:
            size = 20
        else:
            raise IsoError(f"Unsupported UDF allocation descriptor type {ad_type}")
        pos = 0
        while pos + size <= len(area):
            raw_len = _u32(area, pos)
            etype = raw_len >> 30
            elen = raw_len & 0x3FFFFFFF
            if ad_type == 0:
                loc, pref = _u32(area, pos + 4), partref
            elif ad_type == 1:
                loc, pref = _u32(area, pos + 4), _u16(area, pos + 8)
            else:  # ext_ad
                loc, pref = _u32(area, pos + 12), _u16(area, pos + 16)
            pos += size
            if elen == 0 and etype == 0:
                break
            if etype == 3:
                aed = self._read_block(pref, loc)
                _check_tag(aed, TAG_AED)
                l_ad = _u32(aed, 20)
                out.extend(self._parse_ads(aed[24:24 + l_ad], ad_type, pref, depth + 1))
                break
            out.append((etype, elen, loc, pref))
        return out

    def _file_extents(self, info: dict) -> list[Extent]:
        extents: list[Extent] = []
        remaining = info["size"]
        for etype, elen, loc, pref in info["ads"]:
            if remaining <= 0:
                break
            take = min(elen, remaining)
            if etype == 0:
                extents.extend(self._map(pref, loc, take))
            else:  # allocated-but-unrecorded or unallocated => zeros
                extents.append(Extent(None, take))
            remaining -= take
        if remaining > 0:
            # Truncated allocation: treat the rest as zeros rather than failing hard.
            extents.append(Extent(None, remaining))
        return extents

    def _load_icb(self, partref: int, lbn: int) -> dict:
        data = self._read_block(partref, lbn)
        tag = _u16(data, 0)
        hops = 0
        while tag == TAG_IE and hops < 16:  # indirect entry
            _check_tag(data, TAG_IE)
            partref, lbn = _u16(data, 16 + 20 + 8), _u32(data, 16 + 20 + 4)
            data = self._read_block(partref, lbn)
            tag = _u16(data, 0)
            hops += 1
        info = self._parse_fe(data, partref)
        info["partref"] = partref
        return info

    # -- public API -----------------------------------------------------

    def root(self) -> Entry:
        partref, lbn = self.root_icb
        info = self._load_icb(partref, lbn)
        return Entry("", "", True, info["size"], ref=info, mtime=info["mtime"])

    def listdir(self, dir_entry: Entry) -> list[Entry]:
        info = dir_entry.ref
        assert isinstance(info, dict)
        if info["inline"] is not None:
            data = info["inline"]
        else:
            data = self._read_extents(self._file_extents(info))
        out: list[Entry] = []
        pos = 0
        while pos + 38 <= len(data):
            tag_id = _u16(data, pos)
            if tag_id != TAG_FID:
                # Skip padding up to the next block boundary.
                nxt = (pos // SECTOR + 1) * SECTOR
                if nxt <= pos or nxt >= len(data):
                    break
                pos = nxt
                continue
            chars = data[pos + 18]
            l_fi = data[pos + 19]
            icb_lbn = _u32(data, pos + 24)
            icb_part = _u16(data, pos + 28)
            l_iu = _u16(data, pos + 36)
            name_raw = data[pos + 38 + l_iu:pos + 38 + l_iu + l_fi]
            pos += (38 + l_iu + l_fi + 3) & ~3
            if chars & 0x08 or chars & 0x04:  # parent / deleted
                continue
            name = _cs0(name_raw)
            if not name or name in (".", "..") or "/" in name or "\x00" in name:
                continue
            child = self._load_icb(icb_part, icb_lbn)
            path = f"{dir_entry.path}/{name}" if dir_entry.path else name
            ftype = child["type"]
            if ftype == 4 or chars & 0x02:
                out.append(Entry(path, name, True, child["size"], ref=child, mtime=child["mtime"]))
            elif ftype == 12:
                target = self._symlink_target(child)
                out.append(Entry(path, name, False, 0, symlink=target, mtime=child["mtime"]))
            elif ftype in (0, 5, 249):
                if child["inline"] is not None:
                    out.append(Entry(path, name, False, child["size"], inline=child["inline"],
                                     mtime=child["mtime"]))
                else:
                    out.append(Entry(path, name, False, child["size"], self._file_extents(child),
                                     mtime=child["mtime"]))
            # Other types (devices, FIFOs, stream directories...) are ignored.
        return out

    def _symlink_target(self, info: dict) -> str:
        if info["inline"] is not None:
            data = info["inline"]
        else:
            data = self._read_extents(self._file_extents(info), limit=1024 * 1024)
        parts: list[str] = []
        absolute = False
        pos = 0
        while pos + 4 <= len(data):
            ctype, l_ci = data[pos], data[pos + 1]
            ident = data[pos + 4:pos + 4 + l_ci]
            pos += 4 + l_ci
            if ctype in (1, 2):
                absolute = True
                parts = []
            elif ctype == 3:
                parts.append("..")
            elif ctype == 4:
                parts.append(".")
            elif ctype == 5:
                parts.append(_cs0(ident))
        return ("/" if absolute else "") + "/".join(parts)

    @property
    def label(self) -> str:
        return self.lv_label or self.pvd_label or self.fs_label


# ---------------------------------------------------------------------------
# Unified image access
# ---------------------------------------------------------------------------


class ImageFS:
    """Opens an optical-disc image and exposes its most complete file system."""

    def __init__(self, path_or_file: str | os.PathLike | BinaryIO, prefer: str | None = None):
        self.src = _Source(path_or_file)
        self.iso: Iso9660FS | None = None
        self.udf: UdfFS | None = None
        self.errors: list[str] = []
        try:
            self.iso = Iso9660FS(self.src)
        except (IsoError, struct.error) as exc:
            self.errors.append(f"ISO 9660: {exc}")
        try:
            self.udf = UdfFS(self.src)
        except (IsoError, struct.error) as exc:
            if self.iso is None or "recognition" not in str(exc):
                self.errors.append(f"UDF: {exc}")
        if self.iso is None and self.udf is None:
            self.src.close()
            raise IsoError("Not an ISO 9660 or UDF image" +
                           (f" ({'; '.join(self.errors)})" if self.errors else ""))
        self.fs = self._choose(prefer)
        self._cache: dict[str, list[Entry]] = {}

    def _choose(self, prefer: str | None):
        if prefer == "iso9660" and self.iso is not None:
            return self.iso
        if prefer == "udf" and self.udf is not None:
            return self.udf
        if self.udf is not None and self.iso is not None:
            try:
                n_udf = len(self.udf.listdir(self.udf.root()))
            except (IsoError, struct.error) as exc:
                self.errors.append(f"UDF: {exc}")
                return self.iso
            try:
                n_iso = len(self.iso.listdir(self.iso.root()))
            except (IsoError, struct.error):
                return self.udf
            return self.udf if n_udf >= n_iso else self.iso
        return self.udf or self.iso

    @property
    def kind(self) -> str:
        if self.fs is self.udf:
            return "udf"
        assert self.iso is not None
        if self.iso.rock_ridge and not self.iso.use_joliet:
            return "iso9660+rockridge"
        if self.iso.use_joliet:
            return "iso9660+joliet"
        return "iso9660"

    @property
    def label(self) -> str:
        # Prefer the ISO 9660 volume id (what blkid & boot loaders match),
        # falling back to the UDF logical volume id.
        if self.iso is not None and self.iso.volume_id:
            if self.fs is self.udf and self.udf is not None and self.udf.label:
                return self.udf.label
            return self.iso.volume_id
        if self.udf is not None:
            return self.udf.label
        return ""

    def boot_entries(self) -> list[BootEntry]:
        return self.iso.boot_entries() if self.iso is not None else []

    def root(self) -> Entry:
        return self.fs.root()

    def listdir(self, entry: Entry | str = "") -> list[Entry]:
        if isinstance(entry, str):
            found = self.lookup(entry) if entry else self.root()
            if found is None or not found.is_dir:
                raise IsoError(f"No such directory: {entry}")
            entry = found
        key = entry.path
        cached = self._cache.get(key)
        if cached is None:
            cached = self.fs.listdir(entry)
            self._cache[key] = cached
        return cached

    def walk(self, max_entries: int = MAX_ENTRIES) -> Iterator[Entry]:
        """Depth-first pre-order walk (directories before their contents)."""
        count = 0
        seen: set = set()
        stack: list[tuple[Entry, int]] = [(self.root(), 0)]
        while stack:
            d, depth = stack.pop()
            if depth > MAX_DEPTH:
                raise IsoError("Directory tree too deep")
            key = self._dir_key(d)
            if key in seen:
                continue
            seen.add(key)
            children = self.listdir(d)
            dirs = []
            for c in children:
                count += 1
                if count > max_entries:
                    raise IsoError("Too many files in image")
                yield c
                if c.is_dir:
                    dirs.append(c)
            for c in reversed(dirs):
                stack.append((c, depth + 1))

    @staticmethod
    def _dir_key(d: Entry):
        if isinstance(d.ref, tuple):
            return ("iso", d.ref)
        if isinstance(d.ref, dict):
            ads = d.ref.get("ads") or []
            if ads:
                return ("udf", ads[0][2], ads[0][3])
            return ("udf-inline", d.path)
        return ("?", d.path)

    def lookup(self, path: str, case_sensitive: bool = False) -> Entry | None:
        parts = [p for p in path.strip("/").split("/") if p]
        cur = self.root()
        for i, part in enumerate(parts):
            if not cur.is_dir:
                return None
            match = None
            for c in self.listdir(cur):
                if c.name == part or (not case_sensitive and c.name.lower() == part.lower()):
                    match = c
                    if c.name == part:
                        break
            if match is None:
                return None
            cur = match
        return cur

    def iter_chunks(self, entry: Entry, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        if entry.inline is not None:
            yield entry.inline[:entry.size]
            return
        remaining = entry.size
        for ext in entry.extents:
            left = min(ext.length, remaining)
            off = ext.offset
            while left > 0:
                n = min(chunk_size, left)
                if off is None:
                    yield b"\x00" * n
                else:
                    yield self.src.read(off, n)
                    off += n
                left -= n
                remaining -= n
            if remaining <= 0:
                break

    def read(self, entry: Entry | str, max_bytes: int | None = None) -> bytes:
        if isinstance(entry, str):
            found = self.lookup(entry)
            if found is None or found.is_dir:
                raise IsoError(f"No such file: {entry}")
            entry = found
        out = bytearray()
        for chunk in self.iter_chunks(entry, 1024 * 1024):
            out += chunk
            if max_bytes is not None and len(out) >= max_bytes:
                return bytes(out[:max_bytes])
        return bytes(out)

    def read_range(self, entry: Entry, offset: int, length: int) -> bytes:
        """Read `length` bytes at `offset` inside a file."""
        if entry.inline is not None:
            return entry.inline[offset:offset + length]
        out = bytearray()
        pos = 0
        end = min(offset + length, entry.size)
        for ext in entry.extents:
            ext_end = pos + ext.length
            if ext_end > offset and pos < end:
                s = max(offset, pos)
                e = min(end, ext_end)
                if ext.offset is None:
                    out += b"\x00" * (e - s)
                else:
                    out += self.src.read(ext.offset + (s - pos), e - s)
            pos = ext_end
            if pos >= end:
                break
        return bytes(out)

    def close(self) -> None:
        self.src.close()

    def __enter__(self) -> "ImageFS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
