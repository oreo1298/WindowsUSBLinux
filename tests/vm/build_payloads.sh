#!/bin/bash
# Build the test images used by run.sh into the directory given as $1:
#   marker.efi  - GRUB EFI program printing RUFUX-UEFI-OK on the serial port
#   win.iso     - Windows-like UDF ISO (bootmgr stand-in, EFI loader, real install.wim)
#   hybrid.iso  - ISOHybrid GRUB image (BIOS + UEFI) printing RUFUX-DD-OK
#   live.iso    - Linux-like ISO whose boot config searches for its own volume label
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${1:?usage: build_payloads.sh WORKDIR}
mkdir -p "$WORK" && cd "$WORK"
MKISOFS=$(command -v mkisofs || command -v genisoimage) || { echo "need mkisofs (cdrtools) or genisoimage"; exit 1; }

cat > marker.cfg <<'CFG'
serial --unit=0 --speed=115200
terminal_output serial console
echo RUFUX-UEFI-OK
sleep 2
halt
CFG
grub-mkstandalone -O x86_64-efi -o marker.efi --modules="serial terminal echo sleep halt" \
    "boot/grub/grub.cfg=marker.cfg"
nasm -f bin -o bootmgr "$HERE/bootmgr.asm"

rm -rf win wimsrc && mkdir -p win/efi/boot win/efi/microsoft/boot win/sources wimsrc/Windows
cp bootmgr win/bootmgr
cp marker.efi win/bootmgr.efi
cp marker.efi win/efi/boot/bootx64.efi
head -c 20000 /dev/urandom > win/efi/microsoft/boot/bcd
head -c 400000 /dev/urandom > win/sources/boot.wim
head -c 30000 /dev/urandom > win/sources/appraiserres.dll
head -c 70000 /dev/urandom > win/setup.exe
head -c 40000000 /dev/urandom > wimsrc/Windows/data.bin
wimlib-imagex capture wimsrc win/sources/install.wim "Windows 11 Pro" --compress=none >/dev/null
"$MKISOFS" -quiet -udf -iso-level 3 -V CCCOMA_X64FRE_EN-US_DV9 -o win.iso win

rm -rf hyb && mkdir -p hyb/boot/grub
printf 'serial --unit=0 --speed=115200\nterminal_output serial console\nset timeout=0\necho RUFUX-DD-OK\nsleep 2\nhalt\n' \
    > hyb/boot/grub/grub.cfg
grub-mkrescue -o hybrid.iso hyb -- -volid RUFUX_HYBRID >/dev/null 2>&1

rm -rf live && mkdir -p live/EFI/BOOT live/boot/grub live/loader/entries live/casper
printf 'insmod part_msdos\ninsmod part_gpt\ninsmod fat\nserial --unit=0 --speed=115200\nterminal_output serial console\nsearch --no-floppy --file --set=root /boot/grub/rufux-live.cfg\nconfigfile ($root)/boot/grub/rufux-live.cfg\n' \
    > live-embedded.cfg
grub-mkstandalone -O x86_64-efi -o live/EFI/BOOT/BOOTX64.EFI \
    --modules="part_msdos part_gpt fat serial terminal echo sleep halt search search_label search_fs_file configfile test" \
    "boot/grub/grub.cfg=live-embedded.cfg"
printf 'search --no-floppy --label RUFUX_LIVE_2026_TEST --set=found\nif [ -n "$found" ]; then echo RUFUX-LABEL-OK; else echo RUFUX-LABEL-MISSING; fi\nsleep 2\nhalt\n' \
    > live/boot/grub/rufux-live.cfg
printf 'title Live\nlinux /casper/vmlinuz\noptions boot=casper archisosearchuuid=2026-09-28-00-00-00-00 quiet\n' \
    > live/loader/entries/live.conf
head -c 100000 /dev/urandom > live/casper/vmlinuz
xorriso -as mkisofs -R -J -V RUFUX_LIVE_2026_TEST -o live.iso live >/dev/null 2>&1

if [ ! -f uefi-ntfs.img ]; then
    if [ -f /usr/share/rufux/uefi-ntfs.img ]; then
        cp /usr/share/rufux/uefi-ntfs.img .
    else
        python3 -c "import sys; sys.path.insert(0, '$HERE/../..'); from rufux.core import uefintfs; import shutil; shutil.copy(uefintfs.download(), 'uefi-ntfs.img')"
    fi
fi
rm -rf win wimsrc hyb live
ls -la "$WORK"/*.iso "$WORK"/uefi-ntfs.img
