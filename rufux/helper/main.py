"""Entry point of the privileged helper (see rufux.helper)."""

from __future__ import annotations

import json
import os
import pwd
import signal
import stat
import sys
import threading
import traceback

from .. import HELPER_PROTOCOL, __version__
from ..core import blockdevs, rufusfiles
from ..core.fsdefs import FILESYSTEMS
from ..core.isofs import ImageFS, IsoError
from ..core.util import human_size
from . import badblocks, dd, isomode
from .context import Cancelled, Context, Emitter, HelperError
from .device import TargetDevice
from .formatting import mkfs, mounted
from .layout import LayoutError, describe, plan_layout
from .save import save_drive

MAX_JOB = 4 * 1024 * 1024
MAX_UEFI_NTFS = 8 * 1024 * 1024


def _user_ids() -> tuple[int | None, int | None]:
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid:
        return None, None
    try:
        u = int(uid)
    except ValueError:
        return None, None
    gid_env = os.environ.get("SUDO_GID")
    if gid_env and gid_env.isdigit():
        return u, int(gid_env)
    try:
        return u, pwd.getpwuid(u).pw_gid
    except KeyError:
        return u, u


def _allow_loop() -> bool:
    # Only honoured when root runs the helper directly (pkexec clears the environment).
    return os.environ.get("RUFUX_ALLOW_LOOP") == "1"


def _check_str(job: dict, key: str, allowed: tuple | None = None, default=None) -> str:
    val = job.get(key, default)
    if not isinstance(val, str):
        raise HelperError(f"Invalid job: '{key}' must be a string")
    if allowed is not None and val not in allowed:
        raise HelperError(f"Invalid job: bad value for '{key}': {val!r}")
    return val


def _open_image(ctx: Context, path: str) -> int:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise HelperError("Invalid image path")
    fd = ctx.open_user_file(path)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise HelperError(f"'{path}' is not a regular file")
    return fd


def _load_uefi_ntfs(ctx: Context, path: str) -> bytes:
    fd = ctx.open_user_file(path)
    try:
        data = os.read(fd, MAX_UEFI_NTFS + 1)
    finally:
        os.close(fd)
    if not data or len(data) > MAX_UEFI_NTFS or data[510:512] != b"\x55\xaa" or b"FAT" not in data[0x36:0x5A]:
        raise HelperError(f"'{path}' is not a valid UEFI:NTFS image")
    return data


def _load_setup_wrapper(ctx: Context, path: str) -> tuple[str, bytes]:
    """Read Rufus' setup.exe wrapper; only the exact pinned files are accepted."""
    fd = ctx.open_user_file(path)
    try:
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(rufusfiles.MAX_SIZE + 1)
    finally:
        os.close(fd)
    arch = rufusfiles.identify_setup_wrapper(data)
    if arch is None:
        raise HelperError(f"'{path}' is not the setup wrapper from Rufus {rufusfiles.RUFUS_VERSION} "
                          "(checksum mismatch)")
    return arch, data


def do_write(ctx: Context, job: dict) -> str:
    mode = _check_str(job, "mode", ("dd", "iso", "format"))
    target = TargetDevice(ctx, job.get("device") or {}, allow_loop=_allow_loop())
    ctx.log(f"Target: {target.drive.display_name()} - {target.path}, {target.size} bytes, "
            f"{target.sector_size}-byte sectors")
    image_fd = None
    image_path = ""
    if mode in ("dd", "iso"):
        image_path = _check_str(job, "image")
        image_fd = _open_image(ctx, image_path)
        ctx.add_cleanup("close image", lambda: os.close(image_fd))
        if blockdevs.image_on_drive(image_path, target.drive):
            raise HelperError("The selected image is stored on the drive you are about to overwrite. "
                              "Copy it somewhere else first.")
        ctx.log(f"Image: {image_path} ({human_size(os.fstat(image_fd).st_size)})")

    # Resolve everything that can fail before touching the drive.
    fs = scheme = ""
    uefi_blob = None
    wrapper = None
    img = None
    if mode in ("iso", "format"):
        fs = _check_str(job, "filesystem", tuple(FILESYSTEMS))
        scheme = _check_str(job, "scheme", ("mbr", "gpt"))
        ctx.tool(*FILESYSTEMS[fs].tools, package=FILESYSTEMS[fs].package)
        ctx.tool("sfdisk", package="util-linux")
        if job.get("uefi_ntfs"):
            uefi_blob = _load_uefi_ntfs(ctx, _check_str(job, "uefi_ntfs"))
    if mode == "iso":
        assert image_fd is not None
        try:
            img = ImageFS(os.fdopen(image_fd, "rb", closefd=False))
        except IsoError as exc:
            raise HelperError(f"Cannot read the ISO image: {exc}") from exc
        ctx.add_cleanup("close iso", img.close)
        opts = job.get("iso") or {}
        if opts.get("split_wim"):
            ctx.tool("wimlib-imagex", package="wimlib")
        if opts.get("bios_grub"):
            ctx.tool("grub-install", "grub2-install", package="grub")
        if opts.get("unattend") is not None:
            _check_str(opts, "unattend")
            if _check_str(opts, "unattend_target", ("bootwim", "oem")) == "bootwim":
                ctx.tool("wimlib-imagex", package="wimlib")
                boot_wim = _check_str(opts, "boot_wim")
                entry = img.lookup(boot_wim)
                if entry is None or entry.is_dir:
                    raise HelperError(f"'{boot_wim}' was not found in the image")
        if opts.get("setup_wrapper"):
            wrapper = _load_setup_wrapper(ctx, _check_str(opts, "setup_wrapper"))

    target.unmount_all()
    target.ensure_unused()
    target.inhibit_automount()

    passes = int(job.get("badblocks") or 0)
    if passes:
        badblocks.run(ctx, target.path, target.size, passes)

    if mode == "dd":
        ctx.status("Writing image (DD mode)...")
        assert image_fd is not None
        # write_image() takes ownership of (and closes) the descriptor it is given.
        dd.write_image(ctx, target.path, target.size, os.dup(image_fd), job.get("compression") or None,
                       verify=bool(job.get("verify", True)), sector_size=target.sector_size)
        target.reread()
        result = "Image written successfully"
    else:
        result = _format_and_populate(ctx, job, target, mode, fs, scheme, uefi_blob, img, image_fd, wrapper)

    if job.get("eject"):
        ctx.status("Ejecting the drive...")
        target.release_automount()
        target.power_off()
        result += " - the drive can now be removed"
    return result


def _format_and_populate(ctx: Context, job: dict, target: TargetDevice, mode: str, fs: str, scheme: str,
                         uefi_blob: bytes | None, img: ImageFS | None, image_fd: int | None,
                         wrapper: tuple[str, bytes] | None = None) -> str:
    opts = job.get("iso") or {}
    persistence = opts.get("persistence") if mode == "iso" else None
    persistence_size = int(job.get("persistence_size") or 0) if persistence else 0
    try:
        specs = plan_layout(target.size, target.sector_size, scheme, fs, bootable=(mode == "iso"),
                            uefi_ntfs_size=len(uefi_blob) if uefi_blob else 0,
                            persistence_size=persistence_size)
    except LayoutError as exc:
        raise HelperError(str(exc)) from exc
    ctx.log(f"Partition scheme: {scheme.upper()}")
    for line in describe(specs):
        ctx.log("  " + line)
    target.wipe()
    paths = target.partition(specs, scheme)
    roles = {s.role: p for s, p in zip(specs, paths)}
    if uefi_blob:
        ctx.status("Writing the UEFI:NTFS partition...")
        target.write_blob(roles["uefi-ntfs"], uefi_blob)
    if persistence and "persistence" in roles:
        isomode.setup_persistence(ctx, roles["persistence"], persistence)
    if not job.get("quick", True):
        ctx.status("Zeroing the partition (full format)...")
        target.zero_region(roles["main"], specs[0].size, "zero", "Formatting")
    label = mkfs(ctx, fs, roles["main"], job.get("label") or "", int(job.get("cluster") or 0),
                 target.sector_size)
    if mode == "iso":
        assert img is not None and image_fd is not None
        ij = isomode.IsoJob(
            fs=fs, label=label,
            iso_label=str(opts.get("iso_label") or ""),
            split_wim=str(opts.get("split_wim") or ""),
            bios_grub=bool(opts.get("bios_grub")),
            unattend=opts.get("unattend") if isinstance(opts.get("unattend"), str) else None,
            unattend_target="oem" if opts.get("unattend_target") == "oem" else "bootwim",
            boot_wim=str(opts.get("boot_wim") or "sources/boot.wim"),
            bypass_appraiser=bool(opts.get("bypass_appraiser")),
            setup_wrapper=wrapper,
            patch_labels=bool(opts.get("patch_labels", True)),
            persistence=persistence if persistence in ("casper", "live") else None,
            extended_label=bool(job.get("extended_label", True)),
            verify=bool(job.get("verify", True)),
        )
        isomode.populate(ctx, ij, img, roles["main"], target.path, image_fd)
        result = "Bootable drive created successfully"
        if ij.skipped:
            result += f" ({len(ij.skipped)} file(s) could not be copied, see the log)"
    else:
        if job.get("extended_label", True) and fs not in ("udf",):
            with mounted(ctx, roles["main"], fs) as mnt:
                isomode.write_autorun(ctx, mnt, label)
        result = "Drive formatted successfully"
    target.settle()
    return result


def do_save(ctx: Context, job: dict) -> str:
    target = TargetDevice(ctx, job.get("device") or {}, allow_loop=_allow_loop())
    out = _check_str(job, "output")
    if not os.path.isabs(out):
        raise HelperError("Invalid output path")
    if blockdevs.image_on_drive(os.path.dirname(out), target.drive):
        raise HelperError("Cannot save the drive onto itself")
    out_fd = ctx.open_user_file(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        target.unmount_all()
        ctx.status(f"Saving {target.path} to {out}...")
        save_drive(ctx, target.path, target.size, out_fd)
    finally:
        os.close(out_fd)
    return f"Drive saved to {out}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print(__version__)
        return 0
    emitter = Emitter()
    if os.geteuid() != 0:
        emitter.emit(t="done", ok=False, msg="The helper must run as root (via pkexec)")
        return 2
    os.umask(0o022)
    uid, gid = _user_ids()
    ctx = Context(emitter, user_uid=uid, user_gid=gid)
    emitter.emit(t="hello", version=__version__, protocol=HELPER_PROTOCOL)

    raw = sys.stdin.buffer.readline(MAX_JOB)
    try:
        job = json.loads(raw.decode("utf-8"))
        if not isinstance(job, dict):
            raise ValueError("job must be an object")
    except (ValueError, UnicodeDecodeError) as exc:
        emitter.emit(t="done", ok=False, msg=f"Invalid job: {exc}")
        return 2

    def watch_stdin() -> None:
        for line in sys.stdin.buffer:
            if line.strip() == b"cancel":
                ctx.log("Cancellation requested")
                ctx.cancel()

    threading.Thread(target=watch_stdin, name="stdin", daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: ctx.cancel())

    ok, cancelled, msg = False, False, ""
    try:
        if job.get("protocol") != HELPER_PROTOCOL:
            raise HelperError("The helper and the application versions do not match")
        action = job.get("action")
        if action == "write":
            msg = do_write(ctx, job)
        elif action == "save":
            msg = do_save(ctx, job)
        else:
            raise HelperError(f"Unknown action {action!r}")
        ok = True
    except Cancelled:
        cancelled, msg = True, "Operation cancelled by the user. The drive is probably unusable now."
    except HelperError as exc:
        msg = str(exc)
    except Exception as exc:  # noqa: BLE001 - report anything unexpected
        for line in traceback.format_exc().splitlines():
            ctx.log(line)
        msg = f"Unexpected error: {exc}"
    finally:
        ctx.run_cleanups()
    if not ok:
        ctx.log(("CANCELLED: " if cancelled else "ERROR: ") + msg)
    emitter.emit(t="done", ok=ok, cancelled=cancelled, msg=msg)
    return 0 if ok else (3 if cancelled else 1)


if __name__ == "__main__":
    sys.exit(main())
