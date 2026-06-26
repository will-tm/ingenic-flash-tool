# Host / cross build for the compact Ingenic flasher.
# Cross-compile by overriding CC and PKG_CONFIG (Buildroot sets these for you).
CC      ?= cc
PKG_CONFIG ?= pkg-config
CFLAGS  ?= -Os -Wall -Wextra
CFLAGS  += $(shell $(PKG_CONFIG) --cflags libusb-1.0)
LDLIBS  += $(shell $(PKG_CONFIG) --libs libusb-1.0)

ingenic_flash: ingenic_flash.c firmware_blob.h
	$(CC) $(CFLAGS) -o $@ ingenic_flash.c $(LDLIBS)

# Regenerate the embedded firmware blob from the Python tree.
firmware_blob.h:
	../.venv/bin/python gen_blob.py

clean:
	rm -f ingenic_flash

.PHONY: clean
