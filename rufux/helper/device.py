"""Target drive handling: validation, unmounting, wiping and partitioning."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import subprocess
import time

from ..core import blockdevs
from . import rawio
from .context import Context, HelperError
from .layout import MiB, PartSpec, sfdisk_script

DEV_RE = re.compile(r"^/dev/[A-Za-z0-9._-]+$")
UDEV_RULES_DIR = "/run/udev/rules.d"


class TargetDevice:
    def __init__(self, ctx: Context, spec: dict, allow_loop: bool = False):
        self.ctx = ctx
        path = str(spec.get("path", ""))
        if not DEV_RE.match(path):
            raise HelperError(f"Invalid device path '{path}'")
        if not blockdevs.is_whole_disk(path):
            raise HelperError(f"{path} is not a whole-disk block device")
        drive = blockdevs.find_drive(path, allow_loop=allow_loop)
        if drive is None:
            raise HelperError(f"Device {path} was not found (was it unplugged?)")
        if int(spec.get("size", -1)) != drive.size:
            raise HelperError(f"The size of {path} changed. The drive may have been replaced; "
                              "please refresh the device list and try again.")
        for key in ("serial", "model"):
            want = spec.get(key)
            if want is not None and want != getattr(drive, key):
                raise HelperError(f"{path} is not the drive that was selected ({key} mismatch). "
                                  "Please refresh the device list and try again.")
        if drive.is_system:
            raise HelperError(f"Refusing to touch {path}: {drive.system_reason}")
        if not blockdevs.is_eligible(drive, show_usb_hdd=True, allow_loop=allow_loop):
            raise HelperError(f"Refusing to touch {path}: it is not a USB drive or SD card")
        if drive.busy_reason:
            raise HelperError(drive.busy_reason)
        if drive.read_only:
            raise HelperError(f"{path} is write protected")
        self.drive = drive
        self.path = path
        self.name = drive.name
        self.size = drive.size
        self.sector_size = drive.log_sec or 512
        self._udev_rule: str | None = None

    # -- mounts ------------------------------------------------------------

    def _current_mounts(self) -> list[str]:
        """Mount points of the disk or its partitions, from /proc/self/mountinfo."""
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        majmins = {f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"}
        sys_dir = f"/sys/class/block/{self.name}"
        with contextlib.suppress(OSError):
            for entry in os.listdir(sys_dir):
                dev_file = os.path.join(sys_dir, entry, "dev")
                if entry.startswith(self.name) and os.path.exists(dev_file):
                    with open(dev_file) as f:
                        majmins.add(f.read().strip())
        paths = {self.path} | {f"/dev/{p}" for p in self._partition_names()}
        out = []
        with contextlib.suppress(OSError), open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                sep = parts.index("-") if "-" in parts else -1
                source = parts[sep + 2] if sep > 0 and len(parts) > sep + 2 else ""
                if parts[2] in majmins or source in paths:
                    out.append(_unescape(parts[4]))
        return out

    def unmount_all(self) -> None:
        mps = set(self.drive.mountpoints) | set(self._current_mounts())
        for mp in sorted(mps, key=len, reverse=True):
            self.ctx.log(f"Unmounting {mp}")
            self.umount(mp)

    def umount(self, mp: str) -> None:
        for attempt in range(10):
            proc = self.ctx.run(["umount", mp], check=False, quiet=attempt > 0)
            if proc.returncode == 0 or not os.path.ismount(mp):
                return
            time.sleep(0.5)
        raise HelperError(f"Could not unmount {mp}: the drive is in use. "
                          "Close any window or program using it and try again.")

    def ensure_unused(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise HelperError(f"{self.path} is busy (still mounted or in use by another program)")
            raise HelperError(f"Cannot open {self.path}: {exc.strerror}")
        os.close(fd)

    # -- automount inhibition ----------------------------------------------

    def inhibit_automount(self) -> None:
        """Hide the drive from udisks while we work (prevents desktop automounting)."""
        sep = "p" if self.name[-1].isdigit() else ""
        rule = (f'SUBSYSTEM=="block", KERNEL=="{self.name}", ENV{{UDISKS_IGNORE}}="1", ENV{{UDISKS_AUTO}}="0"\n'
                f'SUBSYSTEM=="block", KERNEL=="{self.name}{sep}[0-9]*", ENV{{UDISKS_IGNORE}}="1", '
                f'ENV{{UDISKS_AUTO}}="0"\n')
        path = os.path.join(UDEV_RULES_DIR, f"90-rufux-{self.name}.rules")
        try:
            os.makedirs(UDEV_RULES_DIR, exist_ok=True)
            with open(path, "w") as f:
                f.write("# Temporary rule created by Rufux while writing this drive\n" + rule)
        except OSError as exc:
            self.ctx.log(f"Note: could not install udev inhibit rule: {exc}")
            return
        self._udev_rule = path
        self._udevadm("control", "--reload")
        self.ctx.add_cleanup("udev inhibit", self.release_automount)

    def release_automount(self) -> None:
        if not self._udev_rule:
            return
        with contextlib.suppress(OSError):
            os.unlink(self._udev_rule)
        self._udev_rule = None
        self._udevadm("control", "--reload")
        self._udevadm("trigger", "--action=change", f"--sysname-match={self.name}*")

    def _udevadm(self, *args: str) -> None:
        try:
            subprocess.run(["udevadm", *args], env=self.ctx.env, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def settle(self) -> None:
        self._udevadm("settle", "--timeout=15")

    # -- partition table ---------------------------------------------------

    def _partition_names(self) -> list[str]:
        sys_dir = f"/sys/class/block/{self.name}"
        names = []
        with contextlib.suppress(OSError):
            for entry in os.listdir(sys_dir):
                if entry.startswith(self.name) and os.path.exists(os.path.join(sys_dir, entry, "partition")):
                    names.append(entry)
        return sorted(names)

    def reread(self) -> None:
        self.settle()
        if not rawio.reread_partitions(self.path):
            self.ctx.run(["partx", "-u", self.path], check=False, quiet=True)
            self.ctx.run(["blockdev", "--rereadpt", self.path], check=False, quiet=True)
        self.settle()

    def wipe(self) -> None:
        self.ctx.status("Clearing the partition table...")
        wipefs = self.ctx.tool("wipefs", package="util-linux")
        for part in self._partition_names():
            self.ctx.run([wipefs, "--all", "--force", f"/dev/{part}"], check=False)
        self.ctx.run([wipefs, "--all", "--force", self.path], check=False)
        fd, direct = rawio.open_device(self.path, write=True)
        try:
            rawio.zero_range(fd, 0, min(MiB, self.size), direct)
            if self.size > 2 * MiB:
                rawio.zero_range(fd, (self.size - MiB) // 4096 * 4096, MiB, direct)
        finally:
            os.close(fd)
        self.reread()

    def partition(self, specs: list[PartSpec], scheme: str) -> list[str]:
        script = sfdisk_script(specs, scheme, self.sector_size)
        self.ctx.status("Creating partitions...")
        for line in script.splitlines():
            self.ctx.log("  " + line)
        sfdisk = self.ctx.tool("sfdisk", package="util-linux")
        self.ctx.run([sfdisk, "--wipe", "always", "--wipe-partitions", "always", "--no-reread",
                      "--no-tell-kernel", self.path], input=script)
        self.reread()
        paths = self.partition_paths(len(specs))
        # Clear the start of each new partition so no stale file system is detected.
        for spec, part in zip(specs, paths):
            fd, direct = rawio.open_device(part, write=True)
            try:
                rawio.zero_range(fd, 0, min(spec.size, MiB), direct)
            finally:
                os.close(fd)
        return paths

    def partition_paths(self, count: int, timeout: float = 20.0) -> list[str]:
        sep = "p" if self.name[-1].isdigit() else ""
        paths = [f"/dev/{self.name}{sep}{i}" for i in range(1, count + 1)]
        deadline = time.monotonic() + timeout
        tried_partx = False
        while True:
            if all(_is_blk(p) for p in paths):
                return paths
            if time.monotonic() > deadline:
                missing = [p for p in paths if not _is_blk(p)]
                raise HelperError("The kernel did not create the new partition device(s): "
                                  + ", ".join(missing))
            if not tried_partx and time.monotonic() > deadline - timeout / 2:
                tried_partx = True
                self.ctx.run(["partx", "-a", self.path], check=False, quiet=True)
            self.settle()
            time.sleep(0.3)

    # -- raw helpers ---------------------------------------------------------

    def zero_region(self, dev_path: str, length: int, phase: str, label: str) -> None:
        fd, direct = rawio.open_device(dev_path, write=True)
        try:
            rawio.zero_range(
                fd, 0, length, direct,
                progress=lambda d, t: self.ctx.progress(phase, d, t, f"{label}: {d * 100 // max(t, 1)}%"),
                check=self.ctx.check)
        finally:
            os.close(fd)

    def write_blob(self, dev_path: str, data: bytes) -> None:
        fd, direct = rawio.open_device(dev_path, write=True)
        try:
            buf = rawio.aligned_buffer(len(data) + 4096)
            try:
                n = (len(data) + 4095) // 4096 * 4096 if direct else len(data)
                buf[:len(data)] = data
                rawio.pwrite_all(fd, memoryview(buf)[:n], 0)
            finally:
                buf.close()
            os.fsync(fd)
        finally:
            os.close(fd)

    def power_off(self) -> None:
        udisksctl = None
        with contextlib.suppress(HelperError):
            udisksctl = self.ctx.tool("udisksctl")
        if udisksctl:
            proc = self.ctx.run([udisksctl, "power-off", "--no-user-interaction", "-b", self.path],
                                check=False)
            if proc.returncode == 0:
                return
        delete = f"/sys/block/{self.name}/device/delete"
        if os.path.exists(delete):
            with contextlib.suppress(OSError), open(delete, "w") as f:
                f.write("1\n")


def _is_blk(path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
