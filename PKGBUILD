# Maintainer: oreo1298
#
# Arch Linux and Arch-based distributions (CachyOS, EndeavourOS, Manjaro, ...).
# Local build from a clone of this repository:
#
#   git clone https://github.com/oreo1298/WindowsUSBLinux.git
#   cd WindowsUSBLinux
#   makepkg -si
#
# This PKGBUILD packages the working tree it sits in, so `git pull` followed by
# `makepkg -sif` installs the latest version (-f rebuilds instead of reinstalling
# the package that was built last time).

pkgname=rufux
pkgver=1.1.0
pkgrel=1
_rufusver=v4.15
pkgdesc="Rufus-style bootable USB creator for Linux (Windows 10/11 and Linux images)"
arch=('any')
url="https://github.com/oreo1298/WindowsUSBLinux"
license=('GPL-3.0-or-later')
depends=('python' 'pyside6' 'qt6-svg' 'polkit' 'util-linux' 'dosfstools' 'e2fsprogs' 'wimlib')
optdepends=('ntfs-3g: NTFS file system (Windows images with files over 4 GB)'
            'exfatprogs: exFAT file system'
            'grub: legacy BIOS boot for Windows installation drives'
            'udftools: UDF file system'
            'udisks2: power off the drive when finished')
checkdepends=('python-pytest')
# From the Rufus release, by Pete Batard: the UEFI:NTFS boot image (GPLv2+) and the signed
# setup.exe wrappers used for Windows 11 in-place upgrades (GPLv3+).
_rufusres="https://raw.githubusercontent.com/pbatard/rufus/${_rufusver}/res"
source=("uefi-ntfs-${_rufusver}.img::${_rufusres}/uefi/uefi-ntfs.img"
        "setup_x64-${_rufusver}.exe::${_rufusres}/setup/setup_x64.exe"
        "setup_arm64-${_rufusver}.exe::${_rufusres}/setup/setup_arm64.exe")
noextract=("uefi-ntfs-${_rufusver}.img" "setup_x64-${_rufusver}.exe" "setup_arm64-${_rufusver}.exe")
sha256sums=('72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e'
            '11df838dc69378187e1e1aaf32d34384157642d07096c6e49c1d0e7375634544'
            '14bd07f559513890a0f6565df3927392b4fe6b8e6fc3f5e832e9d69c8b7bb7eb')

check() {
  cd "$startdir"
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
}

package() {
  cd "$startdir"
  make DESTDIR="$pkgdir" PREFIX=/usr \
    UEFI_NTFS="$srcdir/uefi-ntfs-${_rufusver}.img" \
    SETUP_X64="$srcdir/setup_x64-${_rufusver}.exe" \
    SETUP_ARM64="$srcdir/setup_arm64-${_rufusver}.exe" \
    install
}
