# Maintainer: oreo1298
#
# Local build from a clone of this repository (Arch Linux / CachyOS):
#
#   git clone https://github.com/oreo1298/WindowsUSBLinux.git
#   cd WindowsUSBLinux
#   makepkg -si
#
# This PKGBUILD packages the working tree it sits in, so a plain `git pull`
# followed by `makepkg -si` installs the latest version.

pkgname=rufux
pkgver=1.0.0
pkgrel=1
_rufusver=v4.15
pkgdesc="Rufus-style bootable USB creator for Linux (Windows 10/11 and Linux images)"
arch=('any')
url="https://github.com/oreo1298/WindowsUSBLinux"
license=('GPL-3.0-or-later')
depends=('python' 'pyside6' 'qt6-svg' 'polkit' 'util-linux' 'dosfstools' 'e2fsprogs')
optdepends=('ntfs-3g: NTFS file system (Windows images with files over 4 GB)'
            'exfatprogs: exFAT file system'
            'wimlib: keep Windows drives on FAT32 by splitting install.wim'
            'grub: legacy BIOS boot for Windows installation drives'
            'udftools: UDF file system'
            'udisks2: power off the drive when finished')
checkdepends=('python-pytest')
# UEFI:NTFS boot image by Pete Batard (GPLv2+), from the Rufus release it ships with.
source=("uefi-ntfs-${_rufusver}.img::https://raw.githubusercontent.com/pbatard/rufus/${_rufusver}/res/uefi/uefi-ntfs.img")
noextract=("uefi-ntfs-${_rufusver}.img")
sha256sums=('72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e')

check() {
  cd "$startdir"
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
}

package() {
  cd "$startdir"
  make DESTDIR="$pkgdir" PREFIX=/usr UEFI_NTFS="$srcdir/uefi-ntfs-${_rufusver}.img" install
}
