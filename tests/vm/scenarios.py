#!/usr/bin/env python3
"""Runs inside the virtme-ng VM started by run.sh (real kernel drivers): exercises the
privileged helper on eight virtual USB sticks and checks the results.

    python3 scenarios.py WORKDIR
"""
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VM = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
sys.path.insert(0, REPO)
from rufux import HELPER_PROTOCOL  # noqa: E402
from rufux.core import blockdevs  # noqa: E402
from rufux.core.unattend import WueOptions, build_unattend  # noqa: E402

UEFI_NTFS = os.path.join(VM, "uefi-ntfs.img")
MiB = 1024 * 1024
results = []


def sh(*cmd, check=False):
    p = subprocess.run(list(cmd), capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"{cmd}: {p.stderr}")
    return p


def run_job(job):
    job = {"protocol": HELPER_PROTOCOL, "action": "write", **job}
    t0 = time.time()
    p = subprocess.run(["/usr/bin/python3", "-I", f"{REPO}/bin/rufux-helper"], input=json.dumps(job) + "\n",
                       capture_output=True, text=True, timeout=3600)
    events = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
    done = events[-1] if events else {"ok": False, "msg": p.stderr[-500:]}
    return done, events, time.time() - t0


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"RESULT: {'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def mount(dev, fstype, mp, ro=True):
    os.makedirs(mp, exist_ok=True)
    return sh("mount", "-t", fstype, "-o", "ro" if ro else "rw", dev, mp).returncode == 0


def umount(mp):
    sh("umount", mp)


def blkid(dev):
    out = sh("blkid", "-p", "-o", "export", dev).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def main():
    for mod in ("xhci_pci", "ehci_pci", "usb-storage", "uas", "sd_mod", "udf", "isofs", "ntfs3", "exfat", "ext4"):
        sh("modprobe", mod)
    drives = []
    for _ in range(60):
        drives = blockdevs.list_drives(show_usb_hdd=True)
        if len(drives) >= 8:
            break
        time.sleep(1)
    print("DRIVES:", [(d.name, d.size // MiB, d.tran, d.removable, d.serial, d.model) for d in drives], flush=True)
    by_size = {d.size // MiB: d for d in drives}

    def dev(size_mib):
        d = by_size[size_mib]
        return d, {"path": d.path, "size": d.size, "serial": d.serial, "model": d.model}

    xml, target = build_unattend(WueOptions(bypass_requirements=True, no_online_account=True,
                                            local_account="tester", no_data_collection=True), "x64")
    win = os.path.join(VM, "win.iso")
    cases = [
        ("A-win-fat32-gpt", 1000, {"mode": "iso", "image": win, "filesystem": "fat32", "scheme": "gpt",
                                   "label": "CCCOMA_X64FRE_EN-US_DV9",
                                   "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "split_wim": "sources/install.wim",
                                           "unattend": xml, "unattend_target": target, "bypass_appraiser": True,
                                           "patch_labels": False}}),
        ("B-win-ntfs-gpt", 1016, {"mode": "iso", "image": win, "filesystem": "ntfs", "scheme": "gpt",
                                  "label": "CCCOMA_X64FRE_EN-US_DV9", "uefi_ntfs": UEFI_NTFS,
                                  "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "patch_labels": False}}),
        ("C-win-fat32-mbr-bios", 1032, {"mode": "iso", "image": win, "filesystem": "fat32", "scheme": "mbr",
                                        "label": "WIN11",
                                        "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "bios_grub": True,
                                                "patch_labels": False}}),
        ("D-win-ntfs-mbr-bios", 1048, {"mode": "iso", "image": win, "filesystem": "ntfs", "scheme": "mbr",
                                       "label": "WIN11", "uefi_ntfs": UEFI_NTFS,
                                       "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "bios_grub": True,
                                               "patch_labels": False}}),
        ("E-win-exfat-gpt", 1064, {"mode": "iso", "image": win, "filesystem": "exfat", "scheme": "gpt",
                                   "label": "WIN11", "uefi_ntfs": UEFI_NTFS,
                                   "iso": {"iso_label": "CCCOMA_X64FRE_EN-US_DV9", "patch_labels": False}}),
        ("F-hybrid-dd", 1080, {"mode": "dd", "image": os.path.join(VM, "hybrid.iso")}),
        ("G-live-iso-fat32-persist", 1096, {"mode": "iso", "image": os.path.join(VM, "live.iso"),
                                            "filesystem": "fat32", "scheme": "mbr",
                                            "label": "RUFUX_LIVE_2026_TEST", "persistence_size": 256 * MiB,
                                            "iso": {"iso_label": "RUFUX_LIVE_2026_TEST", "patch_labels": True,
                                                    "persistence": "casper"}}),
    ]
    for name, size, job in cases:
        d, spec = dev(size)
        job = dict(job, device=spec, verify=True)
        done, events, secs = run_job(job)
        ok = bool(done.get("ok"))
        record(name, ok, f"({secs:.0f}s) {done.get('msg', '')}")
        if not ok:
            for e in events[-25:]:
                if e.get("t") in ("log", "status"):
                    print("   ", e.get("msg"))
            continue
        check_case(name, d)

    # Non-bootable formats, one after another on the same drive
    d, spec = dev(1112)
    for fs, label in (("fat32", "Data Stick"), ("exfat", "exFAT Data"), ("ntfs", "NTFS Data"),
                      ("ext4", "ext4data"), ("udf", "UDFDATA")):
        done, events, secs = run_job({"mode": "format", "device": spec, "filesystem": fs, "scheme": "gpt",
                                      "label": label, "extended_label": True, "verify": True})
        ok = bool(done.get("ok"))
        part = d.path + "1"
        info = blkid(part)
        want_type = {"fat32": "vfat", "exfat": "exfat", "ntfs": "ntfs", "ext4": "ext4", "udf": "udf"}[fs]
        want_label = {"fat32": "DATA STICK"}.get(fs, label)
        label_ok = info.get("LABEL", "").replace("\\x20", " ").replace("\\ ", " ") == want_label
        mp = "/tmp/mnt-fmt"
        rw_ok = False
        if ok and mount(part, {"vfat": "vfat"}.get(want_type, want_type), mp, ro=False):
            try:
                with open(os.path.join(mp, "probe.txt"), "w") as f:
                    f.write("hello")
                rw_ok = open(os.path.join(mp, "probe.txt")).read() == "hello"
                autorun = os.path.exists(os.path.join(mp, "autorun.inf")) or fs == "udf"
            finally:
                umount(mp)
        else:
            autorun = False
        record(f"H-format-{fs}", ok and info.get("TYPE") == want_type and label_ok and rw_ok and autorun,
               f"type={info.get('TYPE')} label={info.get('LABEL')} rw={rw_ok} autorun={autorun} {done.get('msg')}")

    # Save drive to image (reads the last formatted drive back)
    out = "/tmp/saved-drive.img"
    done, events, secs = run_job({"action": "save", "device": spec, "output": out})
    ok = bool(done.get("ok")) and os.path.getsize(out) == d.size
    record("I-save-drive", ok, f"({secs:.0f}s) {done.get('msg')}")

    passed = sum(1 for r in results if r[1])
    print(f"SUMMARY: {passed}/{len(results)} passed", flush=True)


def check_case(name, d):
    p1 = d.path + "1"
    mp = "/tmp/mnt-check"
    if name.startswith("A"):
        ok = mount(p1, "vfat", mp)
        try:
            names = {n.lower() for n in os.listdir(os.path.join(mp, "sources"))}
            unattend = os.path.exists(os.path.join(mp, "autounattend.xml"))
            appr = os.path.getsize(os.path.join(mp, "sources", "appraiserres.dll")) == 0
            good = ok and "install.swm" in names and "install.wim" not in names and unattend and appr
            record("A-contents", good, f"sources={sorted(names)} unattend={unattend} appraiser_empty={appr}")
        finally:
            umount(mp)
    if name.startswith(("B", "D", "E")):
        fstype = "exfat" if name.startswith("E") else "ntfs3"
        ok = mount(p1, fstype, mp)
        try:
            wim = os.path.exists(os.path.join(mp, "sources", "install.wim"))
        finally:
            umount(mp)
        with open(d.path + "2", "rb") as f:
            blob = f.read(MiB)
        same = blob == open(UEFI_NTFS, "rb").read()
        record(f"{name[0]}-contents", ok and wim and same, f"install.wim={wim} uefi-ntfs-partition-matches={same}")
    if name.startswith(("C", "D")):
        with open(d.path, "rb") as f:
            mbr = f.read(512)
        fstype = "ntfs3" if name.startswith("D") else "vfat"
        ok = mount(p1, fstype, mp)
        try:
            cfg = open(os.path.join(mp, "boot", "grub", "grub.cfg")).read()
        finally:
            umount(mp)
        record(f"{name[0]}-grub", ok and b"GRUB" in mbr and "ntldr /bootmgr" in cfg and mbr[446] == 0x80,
               f"grub_in_mbr={b'GRUB' in mbr} active={mbr[446] == 0x80}")
    if name.startswith("G"):
        ok = mount(p1, "vfat", mp)
        try:
            cfg = open(os.path.join(mp, "boot", "grub", "rufux-live.cfg")).read()
            entry = open(os.path.join(mp, "loader", "entries", "live.conf")).read()
        finally:
            umount(mp)
        info2 = blkid(d.path + "2")
        good = (ok and "--label RUFUX_LIVE_ " in cfg and "archisolabel=RUFUX_LIVE_" in entry
                and "persistent" in entry and info2.get("LABEL") == "casper-rw" and info2.get("TYPE") == "ext3")
        record("G-contents", good, f"search_line={cfg.splitlines()[0]!r} entry_options={entry.splitlines()[-1]!r} "
                                   f"persist={info2.get('TYPE')}/{info2.get('LABEL')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("SUMMARY: crashed", exc, flush=True)
