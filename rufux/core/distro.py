"""Linux distribution detection, used to show the right "install X" command."""

from __future__ import annotations

import functools

INSTALL_CMD = {
    "arch": "sudo pacman -S --needed",
    "debian": "sudo apt install",
    "fedora": "sudo dnf install",
    "suse": "sudo zypper install",
}

# Logical package -> package name(s) per family.  Anything not listed has the
# same name everywhere (dosfstools, e2fsprogs, exfatprogs, udftools, zstd...).
PACKAGES: dict[str, dict[str, str]] = {
    "qt": {"arch": "pyside6", "debian": "python3-pyqt6", "fedora": "python3-pyqt6",
           "suse": "python3-PyQt6"},
    "polkit": {"debian": "pkexec"},
    "util-linux": {"debian": "fdisk"},  # sfdisk is packaged separately on Debian/Ubuntu
    "ntfs-3g": {"fedora": "ntfs-3g ntfsprogs", "suse": "ntfs-3g ntfsprogs"},
    "wimlib": {"debian": "wimtools", "fedora": "wimlib-utils", "suse": "wimtools"},
    "grub": {"debian": "grub-pc-bin grub2-common", "fedora": "grub2-pc-modules grub2-tools",
             "suse": "grub2-i386-pc"},
}

GENERIC_NAMES = {
    "qt": "PyQt6 or PySide6",
    "polkit": "polkit (pkexec)",
    "util-linux": "util-linux (sfdisk)",
    "grub": "GRUB for BIOS (i386-pc)",
}

_FAMILY_IDS = {
    "arch": {"arch", "archarm", "cachyos", "endeavouros", "manjaro", "garuda", "artix", "steamos"},
    "debian": {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "raspbian", "kali",
               "neon", "deepin", "mx"},
    "fedora": {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine", "bazzite"},
    "suse": {"suse", "opensuse", "sles", "sled"},
}


def parse_os_release(text: str) -> dict[str, str]:
    info = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition("=")
            info[key.strip()] = val.strip().strip('"').strip("'")
    return info


def family_of(info: dict[str, str]) -> str | None:
    ids = [info.get("ID", "")] + info.get("ID_LIKE", "").split()
    for raw in ids:
        i = raw.lower()
        for fam, members in _FAMILY_IDS.items():
            if i in members or (fam == "suse" and i.startswith("opensuse")):
                return fam
    return None


@functools.lru_cache(maxsize=1)
def family() -> str | None:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as f:
                return family_of(parse_os_release(f.read()))
        except OSError:
            continue
    return None


def package_names(logical: str, fam: str | None = None) -> str:
    fam = family() if fam is None else fam
    if fam is None:
        return GENERIC_NAMES.get(logical, logical)
    return PACKAGES.get(logical, {}).get(fam, logical)


def install_hint(logical: str, fam: str | None = None) -> str:
    """E.g. 'sudo apt install wimtools' (or a generic sentence on unknown distros)."""
    fam = family() if fam is None else fam
    if fam in INSTALL_CMD:
        return f"{INSTALL_CMD[fam]} {package_names(logical, fam)}"
    return f"install {GENERIC_NAMES.get(logical, logical)} with your package manager"
