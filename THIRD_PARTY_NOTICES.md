# Third-party notices

This repository distributes source code only. It contains no third-party binaries, no firmware and no key material.
The build tool downloads or installs the components below **on your Mac, at build time**, and builds them there.
If you redistribute a built app, you also redistribute these components and take on their licence obligations
(for the GPL components: provide or offer their complete corresponding source).

Every decoupled build writes the exact inventory of what it used into the app bundle:
`Contents/Resources/THIRD-PARTY-NOTICES.md`, `Contents/Resources/ThirdParty/packages.txt` (every Debian package with
its version and source package) and `Contents/Resources/ThirdParty/copyright.txt` (each package's copyright and
licence file).

## QEMU

- Version 10.2.1, downloaded as the official source archive `https://download.qemu.org/qemu-10.2.1.tar.xz`; the
  build checks its SHA-256 (pinned in `emulator/native.py`) and compiles `qemu-system-aarch64` with
  Hypervisor.framework support. The configure options are in `emulator/native.py` (`build_qemu`).
- Licence: GNU General Public License version 2. Some parts are under GPL-2.0-or-later, LGPL-2.1-or-later, BSD or MIT
  licences; see `COPYING`, `COPYING.LIB` and `LICENSE` in the source archive.
- Source: the same archive, or <https://www.qemu.org/download/>.

## Linux guest base image (Debian 12 "bookworm", arm64)

Built by Docker from `emulator/Dockerfile.native-base` on your Mac, starting from the `debian:bookworm-slim` image,
and packed into the guest kernel and initramfs that QEMU boots. It contains Debian packages and this project's own
code (init, bridge, importer, hardware shim). The packages installed directly, and their main licences:

| Package | Purpose | Licence |
|---|---|---|
| `linux-image-arm64` | guest kernel and modules | GPL-2.0 |
| `qemu-user-static` (`qemu-arm-static` only is kept) | runs the 32-bit ARM firmware userland | GPL-2.0 (parts LGPL/BSD) |
| `xvfb`, `x11-utils`, `libxtst6` | virtual display for the firmware's LCD | MIT/X11 |
| `python3` | guest bridge and importer | PSF License |
| `kmod`, `iproute2`, `dmsetup` | modules, networking, device mapper | GPL-2.0, LGPL-2.1 |
| `cpio`, `gzip`, `dosfstools`, `xorriso` | packing, FAT images, ISO building | GPL-3.0 |
| `squashfs-tools` | firmware image | GPL-2.0 |
| `gcc-arm-linux-gnueabi`, `libc6-dev-armel-cross` | build the hardware shim and developer preload libraries | GPL-3.0 with GCC Runtime Library Exception; LGPL-2.1 |

Their dependencies are listed in the generated `packages.txt`. Sources: the Debian source packages of those exact
versions, from <https://snapshot.debian.org/> or with `apt-get source`.

The hardware shim (`libsp16-ui.so`) is this project's code; it links only `libdl` and `libgcc` (GCC Runtime Library
Exception) from the cross toolchain and binds to the firmware's own C library at run time.

## macOS app, Python and Swift

The app is written in Swift against Apple system frameworks (AppKit, AVFoundation, CoreAudio) and has no third-party
Swift packages. The Python tools use the Python standard library only. The optional OCR used by the hardware suite
calls a separately installed `tesseract` (Apache-2.0); it is not bundled.

## Firmware

Firmware updates, the GPL source archive and the key are not part of this repository or of any build. You obtain
them yourself from their official pages and they remain subject to their owners' terms.
