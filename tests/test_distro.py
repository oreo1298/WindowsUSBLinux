import pytest

from rufux.core import distro

SAMPLES = {
    "ubuntu": ('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n', "debian"),
    "mint": ('NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n', "debian"),
    "debian": ('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n', "debian"),
    "fedora": ('NAME="Fedora Linux"\nID=fedora\n', "fedora"),
    "tumbleweed": ('NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse"),
    "cachyos": ('NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n', "arch"),
    "endeavouros": ('NAME="EndeavourOS"\nID="endeavouros"\nID_LIKE="arch"\n', "arch"),
    "void": ('NAME="Void"\nID="void"\n', None),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_family_detection(name):
    text, family = SAMPLES[name]
    assert distro.family_of(distro.parse_os_release(text)) == family


def test_install_hints():
    assert distro.install_hint("wimlib", "debian") == "sudo apt install wimtools"
    assert distro.install_hint("wimlib", "fedora") == "sudo dnf install wimlib-utils"
    assert distro.install_hint("wimlib", "arch") == "sudo pacman -S --needed wimlib"
    assert distro.install_hint("util-linux", "debian") == "sudo apt install fdisk"
    assert distro.install_hint("ntfs-3g", "suse") == "sudo zypper install ntfs-3g ntfsprogs"
    assert distro.install_hint("dosfstools", "fedora") == "sudo dnf install dosfstools"
    # Unknown distribution: a generic sentence rather than a wrong command
    assert distro.install_hint("qt", "") == "install PyQt6 or PySide6 with your package manager"
    assert distro.package_names("exfatprogs", "debian") == "exfatprogs"
