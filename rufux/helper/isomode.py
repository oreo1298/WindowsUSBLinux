"""ISO ("file copy") mode: extract a bootable ISO onto a formatted partition.

Handles Windows specifics (split WIM on FAT32, answer files, BIOS boot via GRUB
chainloading bootmgr) and Linux specifics (volume label patching, persistence).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from ..core.image import FAT32_MAX_FILE
from ..core.isofs import Entry, ImageFS, IsoError
from ..core.util import human_size
from .context import Context, HelperError, syncfs
from .formatting import mounted

MiB = 1024 * 1024
SPLIT_SIZE_MIB = 3800
SYNC_EVERY = 64 * MiB
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KERNEL_TOKENS = ("options", "append", "linux", "linuxefi", "$linux", "search", "for", "kernel")
ARCHISO_UUID = re.compile(r"archisosearchuuid=\S+")


@dataclass
class IsoJob:
    fs: str
    label: str
    iso_label: str = ""
    split_wim: str = ""  # path inside the ISO of the WIM to split ('' = no split)
    bios_grub: bool = False
    unattend: str | None = None
    unattend_target: str = "root"  # root | oem
    bypass_appraiser: bool = False
    patch_labels: bool = True
    persistence: str | None = None  # casper | live
    extended_label: bool = True
    verify: bool = True
    skipped: list[str] = field(default_factory=list)


def populate(ctx: Context, job: IsoJob, img: ImageFS, part_dev: str, disk_dev: str,
             image_fd: int) -> None:
    """Copy the image onto the (already formatted) main partition."""
    entries = list(img.walk())
    exclude: set[str] = set()
    split_entry: Entry | None = None
    if job.split_wim:
        split_entry = img.lookup(job.split_wim)
        if split_entry is None or split_entry.is_dir:
            raise HelperError(f"'{job.split_wim}' not found in the image")
        exclude.add(split_entry.path.lower())
    fat = job.fs in ("fat16", "fat32")
    if fat:
        too_big = [e.path for e in entries if not e.is_dir and not e.is_symlink
                   and e.size > FAT32_MAX_FILE and e.path.lower() not in exclude]
        if too_big:
            raise HelperError(f"'{too_big[0]}' is larger than 4 GB, which FAT32 cannot store. "
                              "Use NTFS (or exFAT) instead.")
    files = [e for e in entries if not e.is_dir and e.path.lower() not in exclude]
    copy_total = sum(e.size for e in files)
    split_total = split_entry.size if split_entry else 0
    grand_total = copy_total + split_total
    ctx.log(f"Image contains {len(files)} files ({human_size(copy_total)} to copy"
            + (f", {human_size(split_total)} to split" if split_total else "") + ")")

    transforms = _config_transforms(ctx, job, img, entries)
    hashes: dict[str, str] = {}
    with mounted(ctx, part_dev, job.fs) as mnt:
        ctx.status("Copying ISO files...")
        _copy_tree(ctx, img, entries, mnt, exclude, transforms, hashes, grand_total,
                   native_symlinks=job.fs.startswith("ext"), skipped=job.skipped)
        if split_entry is not None:
            _split_wim(ctx, img, split_entry, image_fd, mnt, copy_total, grand_total)
        if job.unattend:
            _write_unattend(ctx, mnt, job)
        if job.bypass_appraiser:
            _bypass_appraiser(ctx, mnt, hashes)
        if job.persistence == "live" and not transforms:
            ctx.log("Warning: no boot configuration file could be patched for persistence")
        if job.extended_label:
            write_autorun(ctx, mnt, job.label)
        if job.bios_grub:
            install_grub_bios(ctx, disk_dev, mnt, job.fs)
        ctx.status("Flushing data to the drive...")
        syncfs(mnt)
    if job.skipped:
        ctx.log("Warning: the following files could not be copied: " + ", ".join(job.skipped[:20]))
    if job.verify:
        verify_copy(ctx, part_dev, job.fs, hashes, split_entry is not None)


# ---------------------------------------------------------------------------
# copying
# ---------------------------------------------------------------------------


def _safe_join(root: str, rel: str) -> str:
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") or "\x00" in p for p in parts):
        raise HelperError(f"Unsafe path in image: {rel!r}")
    return os.path.join(root, *parts)


def resolve_symlink(img: ImageFS, entry: Entry, depth: int = 0) -> Entry | None:
    target = entry.symlink or ""
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), target))
    if path in ("..", ".") or path.startswith("../"):
        return None
    found = img.lookup(path, case_sensitive=True) or img.lookup(path)
    if found is not None and found.is_symlink:
        if depth >= 8:
            return None
        return resolve_symlink(img, found, depth + 1)
    return found


def _copy_tree(ctx: Context, img: ImageFS, entries: list[Entry], dest: str, exclude: set[str],
               transforms: dict, hashes: dict[str, str], total: int, native_symlinks: bool,
               skipped: list[str]) -> None:
    done = 0
    unsynced = 0
    for e in entries:
        ctx.check()
        low = e.path.lower()
        if low in exclude:
            continue
        target = _safe_join(dest, e.path)
        if e.is_dir:
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                raise HelperError(f"Cannot create directory '{e.path}': {exc.strerror}") from exc
            continue
        src = e
        if e.is_symlink:
            if native_symlinks:
                with contextlib.suppress(FileExistsError):
                    os.symlink(e.symlink or "", target)
                continue
            resolved = resolve_symlink(img, e)
            if resolved is None or resolved.is_dir:
                ctx.log(f"Skipping symbolic link '{e.path}' -> '{e.symlink}'")
                continue
            src = resolved
        h = hashlib.sha256()
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        except OSError as exc:
            # e.g. a character that FAT/NTFS cannot store in a file name
            ctx.log(f"Warning: cannot create '{e.path}': {exc.strerror}")
            skipped.append(e.path)
            done += src.size
            continue
        try:
            if low in transforms:
                data = transforms[low](img.read(src))
                _write_all(fd, data)
                h.update(data)
                done += src.size
            else:
                for chunk in img.iter_chunks(src):
                    _write_all(fd, chunk)
                    h.update(chunk)
                    done += len(chunk)
                    unsynced += len(chunk)
                    if unsynced >= SYNC_EVERY:
                        os.fdatasync(fd)
                        unsynced = 0
                    ctx.progress("copy", done, total,
                                 f"Copying ISO files: {human_size(done)} of {human_size(total)}")
                    ctx.check()
        except OSError as exc:
            raise HelperError(f"Error writing '{e.path}': {exc.strerror}") from exc
        except IsoError as exc:
            raise HelperError(f"Error reading '{e.path}' from the image: {exc}") from exc
        finally:
            os.close(fd)
        hashes[e.path] = h.hexdigest()
        if e.mtime:
            with contextlib.suppress(OSError, OverflowError, ValueError):
                os.utime(target, (e.mtime, e.mtime))
        ctx.progress("copy", done, total, f"Copying ISO files: {human_size(done)} of {human_size(total)}")
    syncfs(dest)


def _write_all(fd: int, data) -> None:
    view = memoryview(data)
    while len(view):
        n = os.write(fd, view)
        view = view[n:]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

WIM_PROGRESS = re.compile(r"(\d+) MiB of (\d+) MiB \((\d+)%\)")


def _split_wim(ctx: Context, img: ImageFS, entry: Entry, image_fd: int, mnt: str,
               base_done: int, total: int) -> None:
    wimlib = ctx.tool("wimlib-imagex", package="wimlib")
    dest_dir = os.path.join(mnt, *entry.path.split("/")[:-1])
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "install.swm")
    ctx.status("Splitting the Windows image (install.wim) for FAT32...")
    with _wim_source(ctx, img, entry, image_fd) as wim_path:
        def on_line(line: str) -> None:
            m = WIM_PROGRESS.search(line)
            if m:
                done_mib, total_mib = int(m.group(1)), max(int(m.group(2)), 1)
                done = base_done + entry.size * done_mib // total_mib
                ctx.progress("split", done, total, f"Splitting install.wim: {m.group(3)}%")

        ctx.run_progress([wimlib, "split", wim_path, dest, str(SPLIT_SIZE_MIB)], on_line)
    syncfs(mnt)
    parts = sorted(f for f in os.listdir(dest_dir) if f.lower().startswith("install") and f.lower().endswith(".swm"))
    ctx.log(f"Created {len(parts)} split image files: {', '.join(parts)}")


@contextlib.contextmanager
def _wim_source(ctx: Context, img: ImageFS, entry: Entry, image_fd: int):
    """Yield a file system path to the WIM: loop-mount the ISO, else extract it."""
    loopdev = None
    mp = None
    try:
        losetup = ctx.tool("losetup", package="util-linux")
        proc = subprocess.run([losetup, "--find", "--show", "--read-only", f"/dev/fd/{image_fd}"],
                              capture_output=True, env=ctx.env, pass_fds=(image_fd,), timeout=60)
        if proc.returncode == 0:
            loopdev = proc.stdout.decode().strip()
            ctx.log(f"Attached the image to {loopdev}")
            mp = ctx.make_temp_dir("iso-")
            fstype = "udf" if img.kind == "udf" else "iso9660"
            m = ctx.run(["mount", "-t", fstype, "-o", "ro,nosuid,nodev,noexec", loopdev, mp], check=False)
            if m.returncode == 0:
                path = os.path.join(mp, *entry.path.split("/"))
                if os.path.isfile(path) and os.path.getsize(path) == entry.size:
                    yield path
                    return
                ctx.log("The mounted image does not expose the WIM as expected")
            else:
                ctx.log("Could not mount the image, falling back to extracting the WIM")
    finally:
        if mp and os.path.ismount(mp):
            ctx.run(["umount", mp], check=False, quiet=True)
        if loopdev:
            ctx.run(["losetup", "-d", loopdev], check=False, quiet=True)
    # Fallback: extract to a temporary file on disk (not tmpfs).
    tmp_dir = _pick_temp_dir(entry.size)
    tmp_path = os.path.join(tmp_dir, f"rufux-{os.getpid()}-install.wim")
    ctx.status("Extracting install.wim to a temporary file...")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            done = 0
            for chunk in img.iter_chunks(entry):
                _write_all(fd, chunk)
                done += len(chunk)
                ctx.progress("extract", done, entry.size, f"Extracting install.wim: {done * 100 // entry.size}%")
                ctx.check()
        finally:
            os.close(fd)
        yield tmp_path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _pick_temp_dir(size: int) -> str:
    for d in ("/var/tmp", "/tmp", "/root"):
        try:
            st = os.statvfs(d)
        except OSError:
            continue
        if st.f_bavail * st.f_frsize > size + 256 * MiB:
            return d
    raise HelperError("Not enough free disk space to extract install.wim temporarily "
                      f"({human_size(size)} needed). Use NTFS instead of FAT32.")


def _write_unattend(ctx: Context, mnt: str, job: IsoJob) -> None:
    data = (job.unattend or "").encode("utf-8")
    if job.unattend_target == "oem":
        # No windowsPE settings: Setup copies $OEM$\$$ into %WINDIR% (Rufus does the same).
        target = os.path.join(_mkdirs_ci(mnt, ["sources", "$OEM$", "$$", "Panther"]), "unattend.xml")
    else:
        target = os.path.join(mnt, "autounattend.xml")
    with open(target, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    ctx.log(f"Created '{os.path.relpath(target, mnt)}' (Windows User Experience options)")


def _find_ci(root: str, parts: list[str]) -> str | None:
    cur = root
    for p in parts:
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == p.lower()), None)
        if match is None:
            return None
        cur = os.path.join(cur, match)
    return cur


def _mkdirs_ci(root: str, parts: list[str]) -> str:
    cur = root
    for p in parts:
        existing = _find_ci(cur, [p])
        cur = existing or os.path.join(cur, p)
        os.makedirs(cur, exist_ok=True)
    return cur


def _bypass_appraiser(ctx: Context, mnt: str, hashes: dict[str, str]) -> None:
    """Rufus' trick for in-place upgrades: neutralise sources/appraiserres.dll."""
    sources = _find_ci(mnt, ["sources"])
    if not sources:
        return
    dll = _find_ci(sources, ["appraiserres.dll"])
    if dll is None:
        return
    backup = os.path.join(sources, "appraiserres.bak")
    os.replace(dll, backup)
    open(dll, "wb").close()
    rel_dll = os.path.relpath(dll, mnt).replace(os.sep, "/")
    rel_bak = os.path.relpath(backup, mnt).replace(os.sep, "/")
    if rel_dll in hashes:
        hashes[rel_bak] = hashes[rel_dll]
    hashes[rel_dll] = hashlib.sha256(b"").hexdigest()
    ctx.log("Renamed 'sources/appraiserres.dll' to 'appraiserres.bak' and created an empty placeholder")


def install_grub_bios(ctx: Context, disk_dev: str, mnt: str, fs: str) -> None:
    grub_install = ctx.tool("grub-install", "grub2-install", package="grub")
    if not any(os.path.isdir(d) for d in ("/usr/lib/grub/i386-pc", "/usr/lib/grub2/i386-pc")):
        raise HelperError("GRUB for BIOS (i386-pc) is not installed (sudo pacman -S grub)")
    ctx.status("Installing GRUB for legacy BIOS boot...")
    fsmod = {"ntfs": "ntfs", "exfat": "exfat", "fat32": "fat", "fat16": "fat"}.get(fs, "fat")
    boot_dir = os.path.join(mnt, "boot")
    os.makedirs(boot_dir, exist_ok=True)
    ctx.run([grub_install, "--target=i386-pc", f"--boot-directory={boot_dir}", "--no-floppy",
             "--force", f"--modules=part_msdos {fsmod} ntldr search search_fs_file", disk_dev],
            timeout=600)
    grub_dir = _find_ci(boot_dir, ["grub"]) or os.path.join(boot_dir, "grub")
    with open(os.path.join(grub_dir, "grub.cfg"), "w") as f:
        f.write(
            "# Created by Rufux: chainload the Windows boot manager on BIOS systems\n"
            "set timeout=0\n"
            "insmod part_msdos\n"
            f"insmod {fsmod}\n"
            "insmod ntldr\n"
            "search --no-floppy --file --set=root /bootmgr\n"
            "ntldr /bootmgr\n"
            "boot\n")


# ---------------------------------------------------------------------------
# Linux: label patching and persistence
# ---------------------------------------------------------------------------


def patch_config_text(text: str, iso_label: str, usb_label: str, persistence: str | None) -> str:
    """Apply Rufus-style fixes to a boot loader config file."""
    out_lines = []
    esc_iso = iso_label.replace(" ", "\\x20")
    esc_usb = usb_label.replace(" ", "\\x20")
    for line in text.splitlines(keepends=True):
        words = line.split(None, 1)
        token = words[0].lower() if words else ""
        if token in KERNEL_TOKENS:
            if iso_label and usb_label and iso_label != usb_label:
                if esc_iso != iso_label:
                    line = line.replace(esc_iso, esc_usb)
                line = line.replace(iso_label, usb_label)
            if usb_label and "archisosearchuuid=" in line:
                # archiso looks for the ISO's UUID, which a FAT/NTFS copy cannot have:
                # search by volume label instead.
                line = ARCHISO_UUID.sub(lambda m: "archisolabel=" + esc_usb, line)
            if persistence and token.lower() in ("linux", "linuxefi", "append", "kernel", "$linux", "options"):
                line = _add_persistence(line, persistence)
        out_lines.append(line)
    return "".join(out_lines)


def _add_persistence(line: str, kind: str) -> str:
    if kind == "casper":
        if "persistent" in line.split():
            return line
        for old, new in (("file=/cdrom/preseed", "persistent file=/cdrom/preseed"),
                         ("boot=casper", "boot=casper persistent"),
                         ("/casper/vmlinuz", "/casper/vmlinuz persistent")):
            if old in line:
                line = line.replace(old, new, 1)
                return line.replace(" maybe-ubiquity", "")
        return line
    if kind == "live":
        if "persistence" in line.split():
            return line
        if "boot=live" in line:
            return line.replace("boot=live", "boot=live persistence", 1)
    return line


def _config_transforms(ctx: Context, job: IsoJob, img: ImageFS, entries: list[Entry]) -> dict:
    """Map lower-case paths of config files to text-patching callables."""
    need_label = job.patch_labels and job.iso_label and job.label and job.iso_label != job.label
    if not need_label and not job.persistence:
        return {}
    out = {}
    for e in entries:
        low = e.path.lower()
        if e.is_dir or e.is_symlink or e.size > 1024 * 1024 or not low.endswith((".cfg", ".conf")):
            continue

        def fix(data: bytes, path=e.path) -> bytes:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return data
            new = patch_config_text(text, job.iso_label if need_label else "", job.label, job.persistence)
            if new != text:
                ctx.log(f"Patched '{path}'")
            return new.encode("utf-8")

        out[low] = fix
    if need_label:
        ctx.log(f"Boot configuration: replacing ISO label '{job.iso_label}' with '{job.label}'")
    return out


def setup_persistence(ctx: Context, dev: str, kind: str) -> None:
    from .formatting import mkfs

    label = "casper-rw" if kind == "casper" else "persistence"
    ctx.status("Creating the persistent partition...")
    mkfs(ctx, "ext3", dev, label)
    if kind == "live":
        with mounted(ctx, dev, "ext3") as mnt:
            with open(os.path.join(mnt, "persistence.conf"), "w") as f:
                f.write("/ union\n")
            ctx.log("Created persistence.conf")


# ---------------------------------------------------------------------------
# extended label (autorun.inf + icon), as Rufus does
# ---------------------------------------------------------------------------


def write_autorun(ctx: Context, mnt: str, label: str) -> None:
    if _find_ci(mnt, ["autorun.inf"]):
        return  # keep the image's own autorun.inf (e.g. Windows setup)
    icon_src = os.path.join(PACKAGE_DIR, "data", "autorun.ico")
    have_icon = os.path.isfile(icon_src) and not _find_ci(mnt, ["autorun.ico"])
    text = "; Created by Rufux\r\n[autorun]\r\n"
    if have_icon:
        text += "icon  = autorun.ico\r\n"
    text += f"label = {label}\r\n"
    try:
        with open(os.path.join(mnt, "autorun.inf"), "wb") as f:
            f.write(b"\xff\xfe" + text.encode("utf-16-le"))
        if have_icon:
            shutil.copyfile(icon_src, os.path.join(mnt, "autorun.ico"))
        ctx.log("Created autorun.inf (extended label)")
    except OSError as exc:
        ctx.log(f"Could not create autorun.inf: {exc}")


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def verify_copy(ctx: Context, part_dev: str, fs: str, hashes: dict[str, str], split: bool) -> None:
    from . import rawio

    ctx.status("Verifying copied files...")
    with contextlib.suppress(OSError):
        fd = os.open(part_dev, os.O_RDONLY | os.O_CLOEXEC)
        rawio.flush_buffers(fd)
        os.close(fd)
    with mounted(ctx, part_dev, fs, readonly=True) as mnt:
        total = len(hashes)
        bad = []
        for i, (rel, digest) in enumerate(sorted(hashes.items()), 1):
            ctx.check()
            path = _safe_join(mnt, rel)
            h = hashlib.sha256()
            try:
                with open(path, "rb", buffering=0) as f:
                    while True:
                        chunk = f.read(4 * MiB)
                        if not chunk:
                            break
                        h.update(chunk)
            except OSError as exc:
                bad.append(f"{rel} ({exc.strerror})")
                continue
            if h.hexdigest() != digest:
                bad.append(rel)
            ctx.progress("verify", i, total, f"Verifying: {i} of {total} files")
        if bad:
            raise HelperError("Verification FAILED for: " + ", ".join(bad[:10])
                              + ". The drive may be faulty or counterfeit.")
        if split:
            wimlib = ctx.tool("wimlib-imagex", package="wimlib")
            sources = _find_ci(mnt, ["sources"]) or os.path.join(mnt, "sources")
            ctx.status("Verifying the split Windows image...")

            def on_line(line: str) -> None:
                m = WIM_PROGRESS.search(line)
                if m:
                    ctx.progress("verify-wim", int(m.group(1)), max(int(m.group(2)), 1),
                                 f"Verifying install.swm: {m.group(3)}%")

            ctx.run_progress([wimlib, "verify", os.path.join(sources, "install.swm"),
                              f"--ref={os.path.join(sources, 'install*.swm')}"], on_line)
    ctx.log(f"Verification successful ({len(hashes)} files)")
