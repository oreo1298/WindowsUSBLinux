"""Block device discovery with system-disk protection.

Only USB drives and SD cards are offered (like Rufus); internal disks and any
disk that hosts a running system mount point or active swap are never listed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field

from .util import human_size

LSBLK_COLUMNS = [
    "NAME", "KNAME", "PATH", "PKNAME", "TYPE", "SIZE", "MODEL", "VENDOR", "SERIAL", "TRAN",
    "RM", "HOTPLUG", "RO", "ROTA", "LOG-SEC", "PHY-SEC", "PTTYPE", "FSTYPE", "LABEL", "UUID",
    "PARTLABEL", "MOUNTPOINTS", "MAJ:MIN",
]

# Mount points that may legitimately be unmounted before writing.
REMOVABLE_MOUNT_PREFIXES = ("/run/media/", "/media/", "/mnt/", "/tmp/", "/run/rufux/")

USB_HDD_MIN_SIZE = 128 * 1000 ** 3  # non-removable USB disks bigger than this are "USB HDDs"


class BlockDevError(Exception):
    pass


@dataclass
class Node:
    name: str
    path: str
    type: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    partlabel: str = ""
    mountpoints: list[str] = field(default_factory=list)
    majmin: str = ""
    children: list["Node"] = field(default_factory=list)

    def descendants(self):
        for c in self.children:
            yield c
            yield from c.descendants()


@dataclass
class Drive:
    name: str
    path: str
    size: int
    model: str = ""
    vendor: str = ""
    serial: str = ""
    tran: str = ""
    removable: bool = False
    hotplug: bool = False
    read_only: bool = False
    rotational: bool = False
    log_sec: int = 512
    phy_sec: int = 512
    pttype: str = ""
    majmin: str = ""
    node: Node | None = None
    is_system: bool = False
    system_reason: str = ""
    busy_reason: str = ""
    is_sd_card: bool = False
    is_usb_hdd: bool = False
    is_loop: bool = False

    @property
    def partitions(self) -> list[Node]:
        return [c for c in (self.node.children if self.node else []) if c.type == "part"]

    @property
    def mountpoints(self) -> list[str]:
        out: list[str] = []
        if self.node:
            out.extend(self.node.mountpoints)
            for d in self.node.descendants():
                out.extend(d.mountpoints)
        return out

    @property
    def label(self) -> str:
        if self.node is not None:
            if self.node.label:
                return self.node.label
            for p in self.partitions:
                if p.label:
                    return p.label
        return ""

    @property
    def vendor_model(self) -> str:
        parts = [p for p in (self.vendor.strip(), self.model.strip()) if p]
        text = " ".join(parts)
        if self.vendor and self.model and self.model.lower().startswith(self.vendor.strip().lower()):
            text = self.model.strip()
        return text

    def display_name(self) -> str:
        name = self.label or self.vendor_model or ("SD card" if self.is_sd_card else "NO_LABEL")
        extra = " [read-only]" if self.read_only else ""
        return f"{name} ({self.name}) [{human_size(self.size, marketing=True)}]{extra}"

    def identity(self) -> dict:
        """Fields the helper re-checks before writing (guards against re-plugs)."""
        return {"path": self.path, "size": self.size, "serial": self.serial, "model": self.model,
                "majmin": self.majmin}

    def key(self) -> str:
        return f"{self.path}|{self.serial}|{self.size}|{self.model}"


def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip() in ("1", "true", "True")


def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _node(d: dict) -> Node:
    mps = d.get("mountpoints")
    if mps is None:
        mp = d.get("mountpoint")
        mps = [mp] if mp else []
    mountpoints = [m for m in mps if m]
    return Node(
        name=_s(d.get("name")),
        path=_s(d.get("path")) or "/dev/" + _s(d.get("kname") or d.get("name")),
        type=_s(d.get("type")),
        size=_i(d.get("size")),
        fstype=_s(d.get("fstype")),
        label=_s(d.get("label")),
        uuid=_s(d.get("uuid")),
        partlabel=_s(d.get("partlabel")),
        mountpoints=mountpoints,
        majmin=_s(d.get("maj:min")),
        children=[_node(c) for c in d.get("children") or []],
    )


def run_lsblk(extra: list[str] | None = None) -> list[dict]:
    cmd = ["lsblk", "--json", "--bytes", "-o", ",".join(LSBLK_COLUMNS)] + (extra or [])
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15).stdout
    except FileNotFoundError as exc:
        raise BlockDevError("lsblk not found (util-linux is required)") from exc
    except subprocess.CalledProcessError as exc:
        if "MOUNTPOINTS" in cmd[4] and "unknown column" in (exc.stderr or ""):
            cols = [c if c != "MOUNTPOINTS" else "MOUNTPOINT" for c in LSBLK_COLUMNS]
            cmd[4] = ",".join(cols)
            out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15).stdout
        else:
            raise BlockDevError(f"lsblk failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BlockDevError("lsblk timed out") from exc
    return json.loads(out).get("blockdevices", [])


def _active_swaps() -> set[str]:
    out: set[str] = set()
    try:
        with open("/proc/swaps") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if parts:
                    out.add(os.path.realpath(parts[0]))
    except OSError:
        pass
    return out


def _sysfs_read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def classify(drive: Drive, swaps: set[str] | None = None) -> None:
    """Fill in is_system / busy_reason for a drive."""
    swaps = _active_swaps() if swaps is None else swaps
    nodes = [drive.node] if drive.node else []
    if drive.node:
        nodes += list(drive.node.descendants())
    for n in nodes:
        if n is None:
            continue
        for mp in n.mountpoints:
            if mp == "[SWAP]":
                drive.is_system, drive.system_reason = True, f"{n.path} is active swap"
                return
            if not mp.startswith(REMOVABLE_MOUNT_PREFIXES):
                drive.is_system, drive.system_reason = True, f"{n.path} is mounted on {mp}"
                return
        if os.path.realpath(n.path) in swaps:
            drive.is_system, drive.system_reason = True, f"{n.path} is active swap"
            return
        if n.type in ("crypt", "lvm", "dm", "mpath") or n.type.startswith("raid"):
            drive.busy_reason = f"{n.path} is in use ({n.type}); close/deactivate it first"


def list_drives(show_usb_hdd: bool = False, allow_loop: bool = False,
                include_hidden: bool = False) -> list[Drive]:
    devices = run_lsblk()
    swaps = _active_swaps()
    drives: list[Drive] = []
    for d in devices:
        dtype = _s(d.get("type"))
        name = _s(d.get("name"))
        is_loop = dtype == "loop"
        if dtype != "disk" and not (allow_loop and is_loop):
            continue
        node = _node(d)
        drive = Drive(
            name=name,
            path=node.path,
            size=_i(d.get("size")),
            model=_s(d.get("model")),
            vendor=_s(d.get("vendor")),
            serial=_s(d.get("serial")),
            tran=_s(d.get("tran")).lower(),
            removable=_b(d.get("rm")),
            hotplug=_b(d.get("hotplug")),
            read_only=_b(d.get("ro")),
            rotational=_b(d.get("rota")),
            log_sec=_i(d.get("log-sec"), 512),
            phy_sec=_i(d.get("phy-sec"), 512),
            pttype=_s(d.get("pttype")),
            majmin=_s(d.get("maj:min")),
            node=node,
            is_loop=is_loop,
        )
        if drive.size <= 0:
            continue
        if name.startswith("mmcblk"):
            dev_type = _sysfs_read(f"/sys/block/{name}/device/type")
            drive.is_sd_card = dev_type == "SD"
        classify(drive, swaps)
        if not include_hidden and not is_eligible(drive, show_usb_hdd, allow_loop):
            continue
        drives.append(drive)
    return drives


def is_eligible(drive: Drive, show_usb_hdd: bool = False, allow_loop: bool = False) -> bool:
    if drive.is_system:
        return False
    if drive.is_loop:
        return allow_loop
    if drive.tran == "usb":
        drive.is_usb_hdd = (not drive.removable) and (drive.rotational or drive.size > USB_HDD_MIN_SIZE)
        return show_usb_hdd or not drive.is_usb_hdd
    if drive.is_sd_card:
        return True
    if drive.name.startswith("mmcblk"):
        return False  # eMMC: internal storage
    if drive.tran in ("ieee1394", "firewire") and drive.removable:
        return True
    return False


def find_drive(path: str, **kw) -> Drive | None:
    for d in list_drives(include_hidden=True, **kw):
        if d.path == path or os.path.realpath(d.path) == os.path.realpath(path):
            return d
    return None


def disk_of_path(path: str) -> str | None:
    """Return the whole-disk kernel name holding the file at `path`, if any."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    dev = st.st_dev
    link = f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}"
    try:
        real = os.path.realpath(link)
    except OSError:
        return None
    if not os.path.exists(real):
        return None
    if os.path.exists(os.path.join(real, "partition")):
        return os.path.basename(os.path.dirname(real))
    return os.path.basename(real)


def image_on_drive(image_path: str, drive: Drive) -> bool:
    """True if the image file lives on the drive we are about to overwrite."""
    disk = disk_of_path(image_path)
    if disk is not None and disk == drive.name:
        return True
    real = os.path.realpath(image_path)
    for mp in drive.mountpoints:
        if mp and mp != "/" and (real == mp or real.startswith(mp.rstrip("/") + "/")):
            return True
    return False


def is_whole_disk(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISBLK(st.st_mode):
        return False
    sys_path = f"/sys/dev/block/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    return os.path.exists(sys_path) and not os.path.exists(os.path.join(sys_path, "partition"))
