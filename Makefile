# Rufux - install into a prefix (works on any distribution; also used by the PKGBUILD).
#
#   make rufus-files                     # optional: fetch UEFI:NTFS and the setup.exe wrappers (verified)
#   sudo make install                    # into /usr/local
#   sudo make uninstall                  # remove it again (use the same PREFIX)
#   make DESTDIR=/tmp/stage PREFIX=/usr install
#
# Variables:
#   PREFIX      installation prefix (default /usr/local)
#   PYTHON      interpreter for the privileged helper (default /usr/bin/python3)
#   PYTHON_GUI  interpreter for the GUI, e.g. a virtualenv with PySide6 (default: PYTHON)
#   UEFI_NTFS   UEFI:NTFS image to bundle (default: ./uefi-ntfs.img if it exists)
#   SETUP_X64, SETUP_ARM64
#               Rufus' setup.exe wrappers to bundle (default: ./setup_x64.exe, ./setup_arm64.exe)
#   POLKITDIR   polkit actions directory; polkit only reads /usr/share/polkit-1/actions

PREFIX     ?= /usr/local
DESTDIR    ?=
PYTHON     ?= /usr/bin/python3
PYTHON_GUI ?= $(PYTHON)
LIBDIR     ?= $(PREFIX)/lib/rufux
BINDIR     ?= $(PREFIX)/bin
DATADIR    ?= $(PREFIX)/share
POLKITDIR  ?= /usr/share/polkit-1/actions
UEFI_NTFS  ?= $(wildcard uefi-ntfs.img)
SETUP_X64  ?= $(wildcard setup_x64.exe)
SETUP_ARM64 ?= $(wildcard setup_arm64.exe)
APP_ID     := io.github.oreo1298.Rufux
POLICY     := io.github.oreo1298.rufux.policy

# Files from the Rufus release that Rufux pins (see rufux/core/rufusfiles.py).
RUFUS_RES          := https://raw.githubusercontent.com/pbatard/rufus/v4.15/res
UEFI_NTFS_SHA256   := 72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e
SETUP_X64_SHA256   := 11df838dc69378187e1e1aaf32d34384157642d07096c6e49c1d0e7375634544
SETUP_ARM64_SHA256 := 14bd07f559513890a0f6565df3927392b4fe6b8e6fc3f5e832e9d69c8b7bb7eb

.PHONY: all rufus-files install uninstall test check clean

all:
	@echo "Nothing to build. Run 'sudo make install' (see the header of this Makefile) or 'make test'."

# $(call fetch,URL,SHA256): download URL to $@ and check its checksum
define fetch
	if command -v curl >/dev/null; then curl -fL --proto '=https' -o $@.part "$(1)"; \
	else wget -O $@.part "$(1)"; fi
	echo "$(2)  $@.part" | sha256sum -c -
	mv $@.part $@
endef

rufus-files: uefi-ntfs.img setup_x64.exe setup_arm64.exe

uefi-ntfs.img:
	$(call fetch,$(RUFUS_RES)/uefi/uefi-ntfs.img,$(UEFI_NTFS_SHA256))

setup_x64.exe:
	$(call fetch,$(RUFUS_RES)/setup/setup_x64.exe,$(SETUP_X64_SHA256))

setup_arm64.exe:
	$(call fetch,$(RUFUS_RES)/setup/setup_arm64.exe,$(SETUP_ARM64_SHA256))

install:
	install -d "$(DESTDIR)$(LIBDIR)/bin" "$(DESTDIR)$(BINDIR)"
	find rufux -type d -name __pycache__ -prune -o -type f \( -name '*.py' -o -name '*.svg' -o -name '*.ico' \) -print | \
		while read -r f; do install -Dm644 "$$f" "$(DESTDIR)$(LIBDIR)/$$f"; done
	sed '1s|^#!.*|#!$(PYTHON_GUI)|' bin/rufux > "$(DESTDIR)$(LIBDIR)/bin/rufux"
	sed '1s|^#!.*|#!$(PYTHON) -I|' bin/rufux-helper > "$(DESTDIR)$(LIBDIR)/bin/rufux-helper"
	chmod 755 "$(DESTDIR)$(LIBDIR)/bin/rufux" "$(DESTDIR)$(LIBDIR)/bin/rufux-helper"
	ln -sf "$(LIBDIR)/bin/rufux" "$(DESTDIR)$(BINDIR)/rufux"
	install -d "$(DESTDIR)$(POLKITDIR)"
	sed 's|@HELPER@|$(LIBDIR)/bin/rufux-helper|' data/$(POLICY).in > "$(DESTDIR)$(POLKITDIR)/$(POLICY)"
	chmod 644 "$(DESTDIR)$(POLKITDIR)/$(POLICY)"
	install -Dm644 data/$(APP_ID).desktop "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	install -Dm644 data/$(APP_ID).metainfo.xml "$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml"
	install -Dm644 rufux/data/rufux.svg "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg"
	install -Dm644 LICENSE "$(DESTDIR)$(DATADIR)/licenses/rufux/LICENSE"
	if [ -n "$(UEFI_NTFS)" ]; then install -Dm644 "$(UEFI_NTFS)" "$(DESTDIR)$(DATADIR)/rufux/uefi-ntfs.img"; fi
	if [ -n "$(SETUP_X64)" ]; then install -Dm644 "$(SETUP_X64)" "$(DESTDIR)$(DATADIR)/rufux/setup_x64.exe"; fi
	if [ -n "$(SETUP_ARM64)" ]; then install -Dm644 "$(SETUP_ARM64)" "$(DESTDIR)$(DATADIR)/rufux/setup_arm64.exe"; fi
	for py in $(sort $(PYTHON) $(PYTHON_GUI)); do "$$py" -m compileall -q -d "$(LIBDIR)" "$(DESTDIR)$(LIBDIR)/rufux"; done

uninstall:
	rm -rf "$(DESTDIR)$(LIBDIR)" "$(DESTDIR)$(DATADIR)/rufux"
	rm -f "$(DESTDIR)$(BINDIR)/rufux" \
		"$(DESTDIR)$(POLKITDIR)/$(POLICY)" \
		"$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop" \
		"$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml" \
		"$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg" \
		"$(DESTDIR)$(DATADIR)/licenses/rufux/LICENSE"
	-rmdir "$(DESTDIR)$(DATADIR)/licenses/rufux" 2>/dev/null

test check:
	$(PYTHON) -m pytest -q tests

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache src pkg *.pkg.tar.* *.part
