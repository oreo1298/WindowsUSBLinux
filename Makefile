# Rufux - install into a prefix (used by the PKGBUILD, also works standalone).
#
#   make install                     # into /usr/local
#   sudo make PREFIX=/usr install
#   make DESTDIR=/tmp/stage PREFIX=/usr install
#
# Optional: UEFI_NTFS=/path/to/uefi-ntfs.img bundles the UEFI:NTFS boot image.

PREFIX   ?= /usr/local
DESTDIR  ?=
PYTHON   ?= python3
LIBDIR   ?= $(PREFIX)/lib/rufux
BINDIR   ?= $(PREFIX)/bin
DATADIR  ?= $(PREFIX)/share
APP_ID   := io.github.oreo1298.Rufux
POLICY   := io.github.oreo1298.rufux.policy
UEFI_NTFS ?=

.PHONY: all install uninstall test check clean

all:
	@echo "Nothing to build. Use 'make install' (see the Makefile header) or 'make test'."

install:
	install -d "$(DESTDIR)$(LIBDIR)/bin" "$(DESTDIR)$(BINDIR)"
	find rufux -type d -name __pycache__ -prune -o -type f \( -name '*.py' -o -name '*.svg' -o -name '*.ico' \) -print | \
		while read -r f; do install -Dm644 "$$f" "$(DESTDIR)$(LIBDIR)/$$f"; done
	install -m755 bin/rufux "$(DESTDIR)$(LIBDIR)/bin/rufux"
	sed '1s|^#!.*|#!/usr/bin/python3 -I|' bin/rufux-helper > "$(DESTDIR)$(LIBDIR)/bin/rufux-helper"
	chmod 755 "$(DESTDIR)$(LIBDIR)/bin/rufux-helper"
	ln -sf "$(LIBDIR)/bin/rufux" "$(DESTDIR)$(BINDIR)/rufux"
	install -d "$(DESTDIR)$(DATADIR)/polkit-1/actions"
	sed 's|@HELPER@|$(LIBDIR)/bin/rufux-helper|' data/$(POLICY).in > "$(DESTDIR)$(DATADIR)/polkit-1/actions/$(POLICY)"
	chmod 644 "$(DESTDIR)$(DATADIR)/polkit-1/actions/$(POLICY)"
	install -Dm644 data/$(APP_ID).desktop "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	install -Dm644 data/$(APP_ID).metainfo.xml "$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml"
	install -Dm644 rufux/data/rufux.svg "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg"
	install -Dm644 LICENSE "$(DESTDIR)$(DATADIR)/licenses/rufux/LICENSE"
	if [ -n "$(UEFI_NTFS)" ]; then install -Dm644 "$(UEFI_NTFS)" "$(DESTDIR)$(DATADIR)/rufux/uefi-ntfs.img"; fi
	$(PYTHON) -m compileall -q -d "$(LIBDIR)" "$(DESTDIR)$(LIBDIR)/rufux"

uninstall:
	rm -rf "$(DESTDIR)$(LIBDIR)" "$(DESTDIR)$(DATADIR)/rufux"
	rm -f "$(DESTDIR)$(BINDIR)/rufux" \
		"$(DESTDIR)$(DATADIR)/polkit-1/actions/$(POLICY)" \
		"$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop" \
		"$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml" \
		"$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg" \
		"$(DESTDIR)$(DATADIR)/licenses/rufux/LICENSE"

test check:
	$(PYTHON) -m pytest -q tests

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache src pkg *.pkg.tar.*
