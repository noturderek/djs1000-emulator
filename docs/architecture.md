# Architecture

The emulator behaves like the hardware: a fixed machine that boots whatever firmware update it is given. Everything
below the firmware's application is emulated; the application and its userland come from the user's update.

## The hardware boundary

The firmware talks to its hardware through a small set of interfaces. The emulator implements exactly those:

| Interface | Emulated by |
|---|---|
| Panel MCU (buttons, encoders, selector, pads, analog pots, lamps) over SPI device nodes | `hardware_stub.c` (device nodes, ioctls) and `panel.py` / `panel_map.py` / `protocol.py` (128-byte frames with CRC, both directions) |
| Touch strip (capacitive converter) | `hardware_stub.c`, `panel.py` |
| LCD (800x480) and touch | Xvfb in the guest, mirrored to the app window; touch injected as X events |
| Audio codec (three ALSA playback ports) | `hardware_stub.c` paces each stream on its own clock and carries the samples to the host (`audio-sink.swift`, `audio-output.swift`) |
| Internal storage | a per-firmware 1 GB data image (`data/emmc.img`), formatted once |
| USB stick | an optional raw image attached at launch (`--usb-image`), FAT32 |
| Backlight, media notifications, helper processes | `hardware_stub.c` (unknown device nodes and ioctls fail loudly and are counted) |

Nothing in the emulator depends on a firmware version's internals. What differs between versions (lamp channel roles,
pad curve limits, selector window, calibration file format, runtime layout) lives in that version's compatibility
profile ([profiles.md](profiles.md)).

## Components

| Part | Files (in `emulator/`) |
|---|---|
| Build, launch and firmware commands | `native.py` |
| Firmware library and importer, host side (stdlib only) | `firmware.py`, `profiles.py`, `recipes.py`, `fwpack.py` |
| Linux guest base image | `Dockerfile.native-base`, `pack-guest.sh`, `guest-init.sh`, `guest-firmware.py` (import and repack in the guest), `guest-bridge.py`, `enter.py`, `input.py` |
| Hardware shim (ARM preload library) | `hardware_stub.c` |
| Panel model and wire protocol | `panel.py`, `panel_map.py`, `protocol.py` |
| macOS app | `native-window.swift`, `panel-view.swift`, `panel-layout.json`, `firmware-window.swift`, `firmware-library.swift`, `data-image.swift`, `audio-sink.swift`, `audio-output.swift`, `make-icon.swift` |
| Developer tools | `fwdev.py`, `sessionctl.py`, `e2e-screen.py`, `pcm-tap-analyze.py`, `arm-contracts.sh` |
| Hardware conformance suite | `hwsuite.py` |
| Data | `profiles/*.json`, `profiles/products/sp16.json`, `mods/utility-label.json` |

## Boot

1. The app reads its `default-config.json`, resolves the selected firmware in the library, checks it against the
   profiles bundled in the app (only a verified profile for that exact application hash boots), re-hashes the
   firmware image and starts QEMU with Hypervisor.framework.
2. QEMU boots the Debian guest kernel and the base initramfs. The firmware image is attached read-only as a
   virtio-blk disk, the data image as a second disk.
3. The guest init re-verifies the image hash and the application hash against the manifest and its own copy of the
   profiles, mounts the image read-only under a tmpfs overlay, starts Xvfb and the bridge, and runs the firmware's
   application with `qemu-arm-static` and the hardware shim preloaded.
4. The bridge connects the shim to the host over virtio-serial ports: panel frames, lamps, touch, audio, USB and
   control messages. The app draws the panel and the LCD and plays or records audio.

## Import

1. The host checks the update's trailer and CRC, and reads the key into memory: from the key file, or from the GPL
   source archive (its `.tar.gz`, split parts or the two official zip downloads).
2. It boots the base guest in import mode with the update attached read-only and sends the key over a private
   virtio-serial socket. The key is never written to a file.
3. The guest decrypts the update, checks the volume, unpacks the firmware layers, drops credential and key files,
   refuses if any remaining file contains the key, scans the application for the compatibility report, and writes a
   deterministic squashfs to an output disk.
4. The host hashes the image and stores it with its manifest (every file's hash, the update and image hashes,
   revisions, compatibility facts) in `<library>/<id>/`.

## Design decisions

| ID | Decision |
|---|---|
| D1 | The user supplies the key (a key file, or the official GPL source archive it comes from). Nothing is bundled or downloaded. The key is used for the import boot only. |
| D2 | Unknown versions are refused. Only versions with a verified profile boot. A developer build (`--research --allow-unverified`) can boot a candidate to verify it. |
| D3 | The developer workflow also produces repacked updates, for this emulator only. They carry an `SP16MODLAB-<version>` marker and are accepted only when the importer can rebuild them byte for byte from a verified base and the declared recipes. |
| D4 | Neutral product name and bundle id, no vendor logos, source-only distribution. |

Code comments refer to the stages in which the design was built: M1 firmware registry and profiles, M2 importer,
M3 runtime attach (firmware as a disk, no firmware in the guest image), M4 import screen and firmware library,
M5 compatibility report and other versions, M6 developer loop (overlay, app restart, recipes, `fwdev`, repacked
updates), M7 distribution packaging.

## Adding a firmware version

1. Import it. It is stored as `unverified`; read the compatibility report for unknown device nodes, ALSA names,
   ioctls, panel revisions and calibration markers.
2. `python3 emulator/native.py firmware profile --output build/sp16 --id <id> > emulator/profiles/sp16-<version>.json`
   writes a `candidate` profile with this import's identity and the reference profile's measurements.
3. Build a developer variant that shares the library and boots candidates:
   `python3 emulator/native.py build --decoupled --profile fullpanel --research --audio file --allow-unverified --library build/sp16/library --output build/sp16-dev --bundle-suffix dev`.
4. Launch it, correct any measurement that differs (lamp roles, pad curve, selector window, runtime layout,
   calibration), and run the hardware suite at the full tier ([hardware-suite.md](hardware-suite.md)).
5. Only a full-tier pass marks the profile `verified` (`hwsuite.py --mark-verified`). Until then normal builds refuse
   the version.

## Research socket

Builds made with `--research` open an owner-only control socket per session; its path is in the session's
`runtime.json`. `emulator/sessionctl.py --runtime <runtime.json> <command>` sends panel events, touches, raw frames,
snapshots, USB attach and eject, `metrics`, `firmware` and `app-restart`. The hardware suite and `fwdev.py` use it.
