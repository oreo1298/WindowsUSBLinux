# Rufux - install into a prefix (works on any distribution; also used by the PKGBUILD).
#
#   make uefi-ntfs.img                   # optional: fetch the UEFI:NTFS boot image (verified)
#   sudo make install                    # into /usr/local
#   sudo make uninstall                  # remove it again (use the same PREFIX)
#   make DESTDIR=/tmp/stage PREFIX=/usr install
#
# Variables:
#   PREFIX      installation prefix (default /usr/local)
#   PYTHON      interpreter for the privileged helper (default /usr/bin/python3)
#   PYTHON_GUI  interpreter for the GUI, e.g. a virtualenv with PySide6 (default: PYTHON)
#   UEFI_NTFS   UEFI:NTFS image to bundle (default: ./uefi-ntfs.img if it exists)
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
APP_ID     := io.github.oreo1298.Rufux
POLICY     := io.github.oreo1298.rufux.policy

UEFI_NTFS_URL    := https://raw.githubusercontent.com/pbatard/rufus/v4.15/res/uefi/uefi-ntfs.img
UEFI_NTFS_SHA256 := 72683fa1250eeea772d3399277b434d4e55ba8dd0dc926e52d817e701fc2eb9e

.PHONY: all install uninstall test check clean

all:
	@echo "Nothing to build. Run 'sudo make install' (see the header of this Makefile) or 'make test'."

uefi-ntfs.img:
	if command -v curl >/dev/null; then curl -fL --proto '=https' -o $@.part "$(UEFI_NTFS_URL)"; \
	else wget -O $@.part "$(UEFI_NTFS_URL)"; fi
	echo "$(UEFI_NTFS_SHA256)  $@.part" | sha256sum -c -
	mv $@.part $@

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
	rm -rf .pytest_cache src pkg *.pkg.tar.* uefi-ntfs.img.part
