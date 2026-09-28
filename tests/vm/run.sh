#!/bin/bash
# Full-system test (optional, needs root, QEMU, OVMF and virtme-ng):
#
#  1. boots the host's own userland in a VM (virtme-ng) with a real kernel, so the
#     helper runs with the genuine vfat/ntfs3/exfat/udf drivers against 8 virtual
#     USB sticks (usb-storage on xHCI), exactly like on real hardware;
#  2. boots every produced stick in QEMU with UEFI (OVMF) and/or legacy BIOS and
#     checks that the expected boot loader actually runs.
#
#   sudo KERNEL=/boot/vmlinuz-linux tests/vm/run.sh
#
# Needed on Arch: qemu-full (or qemu-base + qemu-hw-usb-*), edk2-ovmf, virtme-ng (AUR),
# busybox, grub, nasm, libisoburn, cdrtools, mtools, wimlib, ntfs-3g, exfatprogs, udftools.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
KERNEL=${KERNEL:-/boot/vmlinuz-linux}
# Must be visible inside the VM: virtme-ng shows the host /tmp but a fresh tmpfs on /var/tmp.
WORK=${WORK:-$(mktemp -d /tmp/rufux-vmtest.XXXXXX)}
[ "$(id -u)" -eq 0 ] || { echo "Please run as root"; exit 1; }
[ -f "$KERNEL" ] || { echo "Kernel $KERNEL not found (set KERNEL=...)"; exit 1; }
echo "Work directory: $WORK"
"$HERE/build_payloads.sh" "$WORK"

QOPTS="-device qemu-xhci,id=xhci,p2=8,p3=8"
i=0
for sz in 1000 1016 1032 1048 1064 1080 1096 1112; do
    i=$((i + 1))
    rm -f "$WORK/usb$sz.img"
    truncate -s "${sz}M" "$WORK/usb$sz.img"
    QOPTS+=" -drive file=$WORK/usb$sz.img,format=raw,if=none,id=u$i"
    QOPTS+=" -device usb-storage,bus=xhci.0,drive=u$i,removable=on"
done
KVM=()
[ -w /dev/kvm ] || KVM=(--disable-kvm)
vng -r "$KERNEL" "${KVM[@]}" --cpus 4 --memory 4G --verbose --qemu-opts="$QOPTS" \
    --exec "python3 $HERE/scenarios.py $WORK" > "$WORK/vm.log" 2>&1 || true
grep -E "^(RESULT|SUMMARY)" "$WORK/vm.log" || { echo "VM run failed, see $WORK/vm.log"; exit 1; }
grep -q "^SUMMARY: \([0-9]*\)/\1 passed" "$WORK/vm.log" || fail=1

fail=${fail:-0}
check() { python3 "$HERE/boot_check.py" "$WORK/$1" "$2" "$3" || fail=1; }
check usb1000.img uefi RUFUX-UEFI-OK    # Windows, GPT, FAT32 (split install.wim)
check usb1016.img uefi RUFUX-UEFI-OK    # Windows, GPT, NTFS through UEFI:NTFS
check usb1032.img bios RUFUX-BIOS-OK    # Windows, MBR, FAT32: GRUB -> bootmgr
check usb1032.img uefi RUFUX-UEFI-OK
check usb1048.img bios RUFUX-BIOS-OK    # Windows, MBR, NTFS: GRUB -> bootmgr
check usb1048.img uefi RUFUX-UEFI-OK    #   ... and UEFI:NTFS
check usb1064.img uefi RUFUX-UEFI-OK    # Windows, GPT, exFAT through UEFI:NTFS
check usb1080.img bios RUFUX-DD-OK      # ISOHybrid image written in DD mode
check usb1080.img uefi RUFUX-DD-OK
check usb1096.img uefi RUFUX-LABEL-OK   # Linux ISO mode: patched volume label found at boot
if [ "$fail" -eq 0 ]; then echo "ALL PASSED"; else echo "SOME TESTS FAILED (logs in $WORK)"; fi
exit "$fail"
