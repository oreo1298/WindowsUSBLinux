# Rufux — a Rufus-style bootable USB creator for Linux

Rufux brings the [Rufus](https://rufus.ie) workflow to Linux: pick a drive and an image, check
the options, and press **START**. It writes Linux ISOs, builds Windows 10/11 installation drives,
and formats USB sticks and SD cards. It runs on any modern Linux distribution.

![Rufux main window](docs/screenshot.png)

> Rufux is an independent project inspired by Rufus. It is not affiliated with Rufus or its author.

## Features

- **Rufus layout and defaults.** Device, Boot selection, Partition scheme, Target system, Volume
  label, File system and Cluster size (using Rufus' cluster-size tables), advanced options, a
  status bar and a log window.
- **Linux images** (Ubuntu, Fedora, Debian, Linux Mint, openSUSE, Arch, …)
  - **DD image mode**, the default for ISOHybrid images: a raw copy, followed by a read-back
    verification.
  - **ISO image mode** (file copy): creates a normal FAT32/NTFS drive, patches boot-loader
    volume labels the way Rufus does, and supports a **persistent partition** for Ubuntu
    (casper) and Debian (live-boot) images.
  - Compressed images: `.img.xz`, `.gz`, `.bz2`, `.zst`, `.zip` and raw `.img`.
- **Windows 10/11 installation drives**
  - GPT + UEFI, or MBR + "BIOS or UEFI" (legacy BIOS boot uses GRUB to chainload `bootmgr`).
  - **FAT32 with automatic `install.wim` splitting** (via wimlib) when the image has a file over
    4 GB. This uses Windows' own signed boot files, so Secure Boot keeps working.
  - Or **NTFS/exFAT + UEFI:NTFS**, the same approach Rufus uses (a small FAT partition at the
    end of the drive holds Pete Batard's UEFI:NTFS driver).
  - The **Windows User Experience** dialog (same options as Rufus): remove the 4GB+ RAM, Secure Boot
    and TPM 2.0 requirements, remove the online Microsoft account requirement, create a local
    account, skip the privacy questions, copy your regional settings, and disable BitLocker
    automatic encryption.
  - **In-place upgrades**: run `setup.exe` from the drive inside Windows to upgrade while keeping
    your files and apps, the same as with a Rufus drive. This also works on PCs that don't meet
    the Windows 11 requirements (see [Upgrading Windows in place](#upgrading-windows-in-place)).
- **Non-bootable formatting**: FAT, FAT32 (including "Large FAT32"), NTFS, exFAT, UDF,
  ext2/3/4, on MBR or GPT, with an optional quick format and extended label (`autorun.inf` + icon).
- **Bad blocks and counterfeit-drive check** (1–4 passes). Every block is tagged with its own
  index, so fake-capacity drives that wrap around are detected.
- **Save drive to image** (the 💾 button) and **checksums** (MD5/SHA1/SHA256/SHA512, with a
  "compare" box).
- **Safety first**
  - Only USB drives and SD cards are listed. Internal disks and anything that hosts `/`, `/boot`,
    `/home`, swap and so on are never shown. USB hard disks are hidden unless you tick
    *List USB Hard Drives*.
  - Before writing, the helper re-checks that the device path still refers to the same drive
    (size, serial, model) you selected.
  - It refuses to overwrite the drive that holds the image, unmounts the drive's partitions,
    and stops the desktop from auto-mounting it while writing.

## Installation

Rufux needs:

- **Python 3.10** or newer, with **PyQt6** or **PySide6** (either one works)
- **polkit** (`pkexec`), which every desktop has, to ask for your password when writing
- **util-linux** (`lsblk`, `sfdisk`, `wipefs`), **dosfstools** and **e2fsprogs**
- **wimlib**, for Windows drives: it splits an `install.wim` over 4 GB so the drive can stay
  FAT32, and stores the Windows setup options inside `boot.wim`

Recommended extras: **ntfs-3g** (NTFS) and **exfatprogs** (exFAT).

Follow the section for your distribution: install the dependencies, download the source, then
install. Every method except the Arch package installs under `/usr/local`.

### Debian, Ubuntu, Linux Mint, Pop!_OS, Zorin OS, elementary OS

```bash
sudo apt install git make python3-pyqt6 pkexec dosfstools fdisk e2fsprogs wimtools
sudo apt install ntfs-3g exfatprogs    # recommended
git clone https://github.com/oreo1298/WindowsUSBLinux.git
cd WindowsUSBLinux
make rufus-files                       # optional, see below
sudo make install
```

On older releases (Ubuntu 22.04, Debian 11) the `pkexec` package is called `policykit-1`. If
`python3-pyqt6` doesn't exist on your release, see [Without a packaged Qt binding](#without-a-packaged-qt-binding).

### Fedora

```bash
sudo dnf install git make python3-pyqt6 polkit dosfstools util-linux e2fsprogs wimlib-utils
sudo dnf install ntfs-3g ntfsprogs exfatprogs    # recommended
git clone https://github.com/oreo1298/WindowsUSBLinux.git
cd WindowsUSBLinux
make rufus-files                                 # optional, see below
sudo make install
```

### openSUSE (Tumbleweed and Leap)

```bash
sudo zypper install git make python3-PyQt6 polkit dosfstools util-linux e2fsprogs wimtools
sudo zypper install ntfs-3g ntfsprogs exfatprogs    # recommended
git clone https://github.com/oreo1298/WindowsUSBLinux.git
cd WindowsUSBLinux
make rufus-files                                    # optional, see below
sudo make install
```

### Arch Linux, CachyOS, EndeavourOS, Manjaro and other Arch-based distributions

The repository includes a PKGBUILD, so Rufux installs as a normal pacman package, with the
files it uses from the Rufus project included (see the notes below):

```bash
sudo pacman -S --needed git base-devel
sudo pacman -S --needed ntfs-3g exfatprogs    # recommended
git clone https://github.com/oreo1298/WindowsUSBLinux.git
cd WindowsUSBLinux
makepkg -si
```

### Other distributions

Install the requirements listed above (plus `git` and `make`) with your package manager, then:

```bash
git clone https://github.com/oreo1298/WindowsUSBLinux.git
cd WindowsUSBLinux
sudo make install
```

### Without a packaged Qt binding

If your distribution has neither PyQt6 nor PySide6, install PySide6 from PyPI into a private
virtual environment and point the installed launcher at it (run this in the repository folder,
instead of the plain `sudo make install`):

```bash
python3 -m venv ~/.local/share/rufux-venv
~/.local/share/rufux-venv/bin/pip install PySide6-Essentials
sudo make PYTHON_GUI=$HOME/.local/share/rufux-venv/bin/python install
```

### Notes for every distribution

- **`make rufus-files`** downloads three small files from the Rufus v4.15 release (with `curl` or
  `wget`) and checks their SHA-256, so `make install` can bundle them: the UEFI:NTFS boot image
  (needed for NTFS/exFAT boot drives) and Rufus' signed `setup.exe` wrappers for x64 and ARM64
  (used on Windows 11 24H2+ drives, see [Upgrading Windows in place](#upgrading-windows-in-place)).
  If you skip it, Rufux offers to download a file the first time it's needed. The Arch package
  always includes them.
- **Legacy BIOS boot for Windows drives** (MBR → "BIOS or UEFI") needs GRUB's BIOS modules:

  | Distribution family | Packages |
  |---------------------|----------|
  | Debian / Ubuntu     | `grub-pc-bin grub2-common` |
  | Fedora              | `grub2-pc-modules grub2-tools` |
  | openSUSE            | `grub2-i386-pc` |
  | Arch-based          | `grub` |

  Installing these does **not** change your own boot loader. Rufux only runs `grub-install`
  against the USB drive.
- **UDF formatting** needs `udftools` (same name everywhere).
- Package names for Debian/Ubuntu were checked on Ubuntu 24.04. The Fedora and openSUSE names
  are the usual ones for those distributions; if one isn't found, search for it with
  `dnf search` or `zypper search`.
- When a tool is missing, Rufux tells you which package to install, using your distribution's
  package manager.

### Starting Rufux

Start **Rufux** from your application menu, or run `rufux` (optionally `rufux some-image.iso`).
The version is shown in the title bar.

### Updating Rufux

Open a terminal in the folder you cloned, download the latest version, and install it again the
same way you installed it. Your settings are kept.

| Installed with | Update with |
|----------------|-------------|
| `makepkg -si` (Arch-based) | `git pull && makepkg -sif` |
| `sudo make install` | `git pull && make rufus-files && sudo make install` |
| `sudo make install` with the PySide6 virtual environment | `git pull && make rufus-files && sudo make PYTHON_GUI=$HOME/.local/share/rufux-venv/bin/python install` |

On Arch, the `-f` matters: without it, makepkg reinstalls the package it built last time instead
of building the new version whenever the version number hasn't changed. `make rufus-files` only
downloads what is missing, and you can leave it out (Rufux then offers to download those files
when it needs them). Drives you made with an older version keep working, but re-create them to
get fixes that change what is written to the drive.

### Running from source without installing

With the requirements installed, you can also run it straight from the repository:

```bash
./bin/rufux
```

Polkit then asks for your password to run `bin/rufux-helper`. Without the installed polkit
policy, the prompt shows the helper's full path instead of a friendly message.

## Uninstalling

Use the command that matches how you installed Rufux:

| Installed with | Remove with |
|----------------|-------------|
| `sudo make install` | `sudo make uninstall` (run it from the repository folder) |
| `makepkg -si` (Arch-based) | `sudo pacman -Rns rufux` |
| `sudo make install` with the PySide6 virtual environment | `sudo make uninstall`, then `rm -rf ~/.local/share/rufux-venv` |

Then remove your personal settings, the download cache (only present if Rufux downloaded the
UEFI:NTFS image or a setup wrapper), and the repository folder:

```bash
rm -rf ~/.config/rufux ~/.cache/rufux
rm -rf ~/WindowsUSBLinux    # or wherever you cloned it
```

Packages you installed yourself (such as `wimlib`, `ntfs-3g`, `exfatprogs` or GRUB) stay
installed; remove them with your package manager if nothing else needs them. **Don't remove
GRUB if your computer boots with it.**

## Using it

1. **Device**: plug in the USB stick. It appears within a couple of seconds.
2. **Boot selection**: click **SELECT** (or drag and drop an image onto the window). Rufux
   analyzes the image and fills in the rest, the same way Rufus does. The **▾** next to SELECT
   has links to the official download pages of common distributions and Windows.
3. **Image option** (shown for ISOHybrid Linux images): *DD Image mode* is recommended. Choose
   *ISO Image mode* if you want a persistent partition or a drive that keeps a normal file system.
4. Check the partition scheme, target system, file system and label. The defaults are sensible.
5. Press **START**. For Windows images, the *Windows User Experience* dialog appears first. After
   that you get the usual `ALL DATA ON DEVICE … WILL BE DESTROYED` confirmation, then a polkit
   password prompt.
6. When the bar shows **READY**, the drive is done (and verified, unless you turned that off under
   *Show advanced format options*).

### Windows notes

- The default is **GPT / UEFI (non CSM) / FAT32**. If `install.wim` is larger than 4 GB, it is
  split into `install.swm` + `install2.swm`… (Windows Setup supports this natively). If
  `wimlib` is missing, or the image uses a solid-compressed `install.esd` larger than 4 GB,
  pick **NTFS** instead.
- **NTFS/exFAT** drives boot through UEFI:NTFS. Its loader and file system drivers are signed, so
  they also start with Secure Boot enabled. If a particular PC still refuses the drive, use FAT32
  or temporarily disable Secure Boot.
- For old BIOS-only PCs, choose **MBR** and **BIOS or UEFI** (needs `grub`).
- The customization options are stored where Rufus stores them. The requirement bypass has to act
  while Windows Setup starts, so the answer file goes inside `sources/boot.wim` (as
  `Autounattend.xml` in the Setup image). Windows only reads it when a PC boots from the drive, so
  it never gets in the way of an in-place upgrade. If you only chose options that apply after
  installation, they go to `sources/$OEM$/$$/Panther/unattend.xml` instead. Nothing is written
  to the root of the drive.
- With the requirement bypass, `sources/appraiserres.dll` is emptied and, for Windows 11 24H2
  and later, `setup.exe` is replaced by Rufus' wrapper (the original becomes `setup.dll`). Both
  are there for in-place upgrades, when you run `setup.exe` from within Windows.

### Upgrading Windows in place

A Windows drive made with Rufux can also upgrade the Windows that is already installed on a PC
and keep your files, apps and settings. For example, it can take Windows 10 to Windows 11, or
Windows 11 to a newer version, the same way a drive made with Rufus can:

1. Create the drive as usual: select the Windows ISO and press **START**. If the PC doesn't
   meet the Windows 11 requirements (for example, it has no TPM 2.0 or Secure Boot, or an
   unsupported CPU), tick **Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0** in the
   *Windows User Experience* dialog. Any file system works.
2. On the PC you want to upgrade, start Windows as usual and plug in the drive. **Don't boot
   from the drive**: that starts a clean install.
3. Open the drive in File Explorer and double-click **setup.exe**. Click **Yes** when Windows asks
   whether to allow it to make changes.
4. Follow the prompts. On the **Ready to install** screen, check that it says **Keep personal
   files and apps**. If it doesn't, click **Change what to keep** and choose that option.
5. Leave the drive plugged in until the upgrade has finished. The PC restarts several times.

Good to know:

- The other options in the dialog (local account, privacy questions, regional settings,
  BitLocker) only apply to clean installs from the drive. An upgrade keeps your existing
  accounts and settings.
- On Windows 11 24H2 and later, the requirement bypass uses the same small `setup.exe` wrapper
  as Rufus. It is signed by the Rufus author. It sets the registry values that Windows Setup
  checks on the running PC, then starts the original setup (`setup.dll`).
- Microsoft doesn't support Windows 11 on PCs that don't meet its requirements, and such PCs
  may not get every update.
- If `setup.exe` shows the clean-install screens (*Install now*, or asks where to install
  Windows), see [Troubleshooting](#troubleshooting).

### Linux notes

- For ISOHybrid images (most Linux ISOs, such as Ubuntu, Fedora, Debian or Arch), **DD mode** is
  the right choice. It is a byte-for-byte copy, so both BIOS and UEFI boot work exactly as designed.
- **ISO mode** copies the files onto FAT32 and patches the boot configuration to use the USB
  volume label. It boots on UEFI systems only.

## How it works

- The GUI (Python + Qt 6, through PyQt6 or PySide6) runs as your normal user. It enumerates drives with
  `lsblk` and reads images with a built-in ISO 9660 / Joliet / Rock Ridge / UDF reader, the way
  Rufus uses libcdio.
- Writing needs root. A small helper (`<prefix>/lib/rufux/bin/rufux-helper`) is started through
  **pkexec**. Polkit caches the authorization for a few minutes. The helper reads one JSON job,
  reports progress as JSON lines, and can be cancelled at any time.
- The helper opens your image with *your* permissions, not root's. It re-validates the target
  drive, then uses standard tools: `wipefs`, `sfdisk`, `mkfs.*`, `mount`, `wimlib-imagex` and
  `grub-install`.
- Partition layouts follow Rufus. The main partition starts at 1 MiB (*Main Data Partition*),
  followed by optional persistence and, at the end of the disk, a *UEFI:NTFS* partition
  (a Microsoft basic data partition with the "no drive letter" attribute on GPT, type `0xEF` on MBR).

## Differences from Rufus

Not implemented: Windows To Go, FreeDOS, installing Syslinux/GRUB for *Linux* ISO mode (use DD
mode), and the built-in Windows ISO downloader (Fido). The **▾** menu next to SELECT links to the
official download pages instead.

Done differently: Rufus writes the requirement-bypass registry values directly into the registry
inside `boot.wim`. Rufux uses the method Rufus falls back to when it can't do that (for example
in its Microsoft Store version): the same values set by the answer file inside `boot.wim`. When
a PC boots from the drive, a command prompt window flashes briefly as Windows Setup starts, and
the first setup screens can look slightly different.

Additions: verification of written data in both modes, compressed-image writing with progress,
a checksum compare box, and a *Safely remove the drive when finished* option.

## Troubleshooting

- **"Authorization failed or was cancelled"**: a polkit authentication agent must be running.
  KDE Plasma and GNOME include one. On minimal window managers, start e.g. `polkit-kde-agent`
  or `lxqt-policykit`.
- **My drive is not listed**: large or rotational USB disks count as *USB Hard Drives*. Tick that
  box under *Show advanced drive properties*. Internal disks are never listed.
- **"Could not unmount … the drive is in use"**: close any file manager window or terminal that
  has the drive open, then retry.
- **Running `setup.exe` from Windows shows the clean-install screens instead of the upgrade**:
  the drive was probably made by Rufux 1.0.1 or older with the requirement bypass ticked. Those
  versions put `autounattend.xml` at the root of the drive, where Windows Setup finds it and
  starts a clean install. Re-create the drive with the current version, or delete
  `autounattend.xml` from the root of the drive before running `setup.exe`. Deleting it also
  drops the requirement bypass for clean installs from that drive.
- **"Removing the Windows 11 requirements … needs wimlib"**: install it (`wimlib` on Arch,
  `wimtools` on Debian/Ubuntu/Mint/openSUSE, `wimlib-utils` on Fedora), then press START again.
- The log window (📄 button) shows every command that was run. *Save* it when reporting a problem.

## Development

The tests use pytest (`python3-pytest` on Debian/Ubuntu and Fedora, `python-pytest` on Arch):

```bash
make test         # unit tests; the end-to-end tests also need root and loop devices
sudo make test    # runs everything, including real partition/format/copy tests on loop devices
```

`tests/vm/run.sh` is an optional full-system test. It starts your own system in a VM (with
[virtme-ng](https://github.com/arighi/virtme-ng)) with 8 virtual USB sticks attached. It runs
the helper against them with the real kernel file system drivers, then boots each resulting stick
in QEMU with UEFI (OVMF) and legacy BIOS to check that the expected boot loader really starts.
This covers Windows FAT32/NTFS/exFAT, GPT/MBR, UEFI:NTFS and BIOS boot through GRUB, the
in-place upgrade layout (answer file inside `boot.wim`, `setup.exe` wrapper), hybrid DD images,
and Linux ISO mode with label patching:

```bash
sudo tests/vm/run.sh    # uses the running kernel; set KERNEL=/path/to/vmlinuz to pick another
```

Code layout:

- `rufux/core/`: Qt-free logic. The image reader (`isofs.py`), image analysis (`image.py`),
  drive enumeration (`blockdevs.py`), file system rules (`fsdefs.py`), UI decisions (`plan.py`),
  the Windows answer file generator (`unattend.py`) and the pinned Rufus files (`rufusfiles.py`).
- `rufux/helper/`: the privileged helper. DD writing, ISO mode, partitioning, formatting, bad
  blocks, and saving a drive.
- `rufux/ui/`: the Qt interface.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
Two components come from the [Rufus repository](https://github.com/pbatard/rufus) and are downloaded
when the package is built, pinned to Rufus v4.15 and checked by SHA-256. Both are © Pete Batard:

- the UEFI:NTFS boot image (GPLv2+);
- the signed Windows 11 `setup.exe` wrapper (GPLv3+; its source is `res/setup/setup.c` in the
  Rufus repository).
