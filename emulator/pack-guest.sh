#!/bin/sh
# This runs only during packaging, never during the macOS app session.
set -eu
umask 077
# Decoupled base image (docs/architecture.md): it must never carry OEM files. Its Debian package list and
# copyright files go into the app bundle's third-party notices (M7).
test -e /opt/sp16/sp16-base
test ! -e /opt/sp16/rootfs
dpkg-query -W -f='${Package} ${Version} source=${source:Package} ${source:Version}\n' > /output/third-party-packages.txt
for sp16_copyright in /usr/share/doc/*/copyright; do
  printf '==== %s\n' "${sp16_copyright#/usr/share/doc/}"
  cat "$sp16_copyright"
done > /output/third-party-copyright.txt
test ! -e /output/guest.cpio.gz
sp16_kernel=$(find /boot -name 'vmlinuz-*' -type f | head -1)
test -n "$sp16_kernel"
cp "$sp16_kernel" /output/kernel
cd /
# Exclude container pseudo-filesystems and large build-only packages.
find . -xdev \
 \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./output \
 -o -path ./tmp -o -path ./boot -o -path ./usr/share/doc -o -path ./usr/share/man \
 -o -path ./usr/lib/gcc -o -path ./usr/lib/arm-linux-gnueabi \
 -o -path ./usr/lib/llvm-14 -o -path ./usr/lib/llvm-15 \
 -o -path ./usr/arm-linux-gnueabi -o -path ./var/cache \) -prune \
 -o -print0 | cpio --null -o --format=newc 2>/output/pack.txt | gzip -1 > /output/guest.cpio.gz
chmod 600 /output/kernel /output/guest.cpio.gz
