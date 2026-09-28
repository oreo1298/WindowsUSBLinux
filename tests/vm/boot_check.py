#!/usr/bin/env python3
"""Boot a disk image in QEMU (UEFI via OVMF, or legacy BIOS via SeaBIOS) as a USB
stick and wait for a marker string on the serial port.

    boot_check.py IMAGE uefi|bios MARKER [--timeout SECONDS]
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

OVMF_CODE = ["/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd",
             "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_CODE.fd"]
OVMF_VARS = ["/usr/share/edk2/x64/OVMF_VARS.4m.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd",
             "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd", "/usr/share/OVMF/OVMF_VARS.fd"]


def first(paths):
    for p in paths:
        for m in glob.glob(p):
            return m
    return None


def boot(image: str, firmware: str, marker: str, timeout: float = 240) -> tuple[bool, float, str]:
    tmp = tempfile.mkdtemp(prefix="rufux-boot-")
    serial = os.path.join(tmp, "serial.txt")
    cmd = ["qemu-system-x86_64", "-m", "1024", "-display", "none", "-no-reboot", "-net", "none",
           "-serial", f"file:{serial}", "-monitor", "none"]
    if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.W_OK):
        cmd += ["-enable-kvm"]
    if firmware == "uefi":
        code, vars_ = first(OVMF_CODE), first(OVMF_VARS)
        if not code or not vars_:
            raise SystemExit("OVMF firmware not found (install edk2-ovmf)")
        vars_copy = os.path.join(tmp, "vars.fd")
        shutil.copy(vars_, vars_copy)
        cmd += ["-machine", "q35", "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive", f"if=pflash,format=raw,file={vars_copy}"]
    cmd += ["-drive", f"file={image},format=raw,if=none,id=usbdisk,snapshot=on",
            "-device", "qemu-xhci", "-device", "usb-storage,drive=usbdisk,bootindex=0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    start = time.time()
    found = False
    text = ""
    try:
        while time.time() - start < timeout:
            time.sleep(1)
            if os.path.exists(serial):
                with open(serial, "rb") as f:
                    text = f.read().decode("latin-1")
                if marker in text:
                    found = True
                    break
            if proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    return found, time.time() - start, text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("firmware", choices=("uefi", "bios"))
    ap.add_argument("marker")
    ap.add_argument("--timeout", type=float, default=240)
    a = ap.parse_args()
    ok, secs, text = boot(a.image, a.firmware, a.marker, a.timeout)
    tail = " | ".join(line.strip() for line in text.replace("\x1b", "").splitlines()[-4:] if line.strip())
    print(f"{'PASS' if ok else 'FAIL'} boot {a.firmware:4s} {os.path.basename(a.image):16s} "
          f"expect {a.marker} ({secs:.0f}s)" + ("" if ok else f" :: {tail[-240:]}"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
