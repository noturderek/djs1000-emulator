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
| One command for all of the below | `sp16emu.py` (`./sp16`; `make` for the common configurations) |
| Build, launch and firmware commands | `native.py` |
| Firmware library and importer, host side (stdlib only) | `firmware.py`, `profiles.py`, `recipes.py`, `fwpack.py` |
| Diagnostics for bug reports (stdlib only; the app bundles a copy) | `diagnostics.py` |
| Linux guest base image | `Dockerfile.native-base`, `pack-guest.sh`, `guest-init.sh`, `guest-firmware.py` (import and repack in the guest), `guest-bridge.py`, `enter.py`, `input.py` |
| Hardware shim (ARM preload library) | `hardware_stub.c` |
| Panel model and wire protocol | `panel.py`, `panel_map.py`, `protocol.py` |
| macOS app | `native-window.swift`, `panel-view.swift`, `panel-layout.json`, `firmware-window.swift`, `firmware-library.swift`, `app-diagnostics.swift`, `data-image.swift`, `audio-sink.swift`, `audio-output.swift`, `make-icon.swift` |
| Developer tools | `fwdev.py`, `sessionctl.py`, `e2e-screen.py`, `pcm-tap-analyze.py`, `arm-contracts.sh` |
| Hardware conformance suite | `hwsuite.py` |
| Data | `profiles/*.json`, `profiles/products/sp16.json` |

## Boot

1. The app reads its `default-config.json`, resolves the selected firmware in the library, checks it against the
   profiles bundled in the app (a verified profile for that exact application hash makes the run verified, anything
   else runs unverified on the reference profile), re-hashes the firmware image and starts QEMU with Hypervisor.framework.
2. QEMU boots the Debian guest kernel and the base initramfs. The firmware image is attached read-only as a
   virtio-blk disk, the data image as a second disk.
3. The guest init re-verifies the image hash and the application hash against the manifest and its own copy of the
   profiles, mounts the image read-only under a tmpfs overlay, starts Xvfb and the bridge, and runs the firmware's
   application with `qemu-arm-static` and the hardware shim preloaded.
4. The bridge connects the shim to the host over virtio-serial ports: panel frames, lamps, touch, audio, USB and
   control messages. The app draws the panel and the LCD and plays or records audio.

## Logs and diagnostics

The app is started through LaunchServices, so its own output is lost; it writes its own log instead. Everything is in
the build folder, owner-only:

| File | Holds |
|---|---|
| `sessions/native-<id>/app.log` (+ `.1`) | timestamped app events: launch configuration, firmware chosen and its status, import and library actions, window open and close, QEMU start and exit status, bridge connect and disconnect, notices shown. 2 MB, one rotation. No key or firmware content exists in the app. |
| `logs/app-<time>-<pid>.log` | the same for the time before a session exists (import screen, library window); the newest 20 are kept |
| `sessions/native-<id>/qemu-stderr.txt` | the last 200 lines of QEMU's stderr and its exit status (also still copied into `console.txt`) |
| `sessions/native-<id>/clean-exit` | written when the app quits normally; `app.pid` tells a later launch whether a session without it is still running |

At launch the app looks at the previous session: no `clean-exit` and no live process, or a new macOS crash report
(`~/Library/Logs/DiagnosticReports/SP16Emulator-*.ips`) naming this app, shows a non-blocking notice that offers to
collect diagnostics, once per finding. Help > Collect Diagnostics… (and `./sp16 diagnostics`) runs the bundled
`diagnostics.py`, which writes one redacted zip; see "Reporting a bug" in the README for what it contains.

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
| D2 | Any update encrypted with the correct key is accepted and runs. A version with a verified profile runs as verified; any other runs on the reference profile, marked unverified (title, library status, a one-time notice, console `SP16_FIRMWARE_UNVERIFIED`). `--allow-unverified` is accepted and does nothing. |
| D3 | The developer workflow also produces repacked updates, for this emulator only. They carry an `SP16MODLAB-<version>` marker and are verified when the importer can rebuild them byte for byte from a verified base and the declared recipes, and run as unverified otherwise. The repository ships the tooling, never a mod. |
| D4 | Neutral product name and bundle id, no vendor logos, source-only distribution. |

Code comments refer to the stages in which the design was built: M1 firmware registry and profiles, M2 importer,
M3 runtime attach (firmware as a disk, no firmware in the guest image), M4 import screen and firmware library,
M5 compatibility report and other versions, M6 developer loop (overlay, app restart, recipes, `fwdev`, repacked
updates), M7 distribution packaging.

## Adding a firmware version

1. Import it. It is stored as `unverified` and already runs; read the compatibility report for unknown device nodes, ALSA names,
   ioctls, panel revisions and calibration markers.
2. `./sp16 firmware profile --id <id> > emulator/profiles/sp16-<version>.json`
   writes a `candidate` profile with this import's identity and the reference profile's measurements.
3. Build a research variant that shares the library:
   `./sp16 build --name sp16-dev --research --audio file --bundle-suffix dev` (all builds share `build/library`).
4. Launch it, correct any measurement that differs (lamp roles, pad curve, selector window, runtime layout,
   calibration), and run the hardware suite at the full tier ([hardware-suite.md](hardware-suite.md)).
5. Only a full-tier pass marks the profile `verified` (`./sp16 hwsuite --tier full --mark-verified`). Until then the version
   runs as unverified.

## Research socket

Builds made with `--research` open an owner-only control socket per session; its path is in the session's
`runtime.json`. `./sp16 ctl <command>` (it runs `emulator/sessionctl.py` against the newest research session) sends panel events, touches, raw frames,
snapshots, USB attach and eject, `metrics`, `firmware` and `app-restart`. The hardware suite and `fwdev.py` use it.
Each call waits up to 60 s for the app's whole reply (`--timeout`, or `SP16_CONTROL_TIMEOUT`; a reply arriving in pieces
counts as one): a heavily loaded host can take well over 10 s to answer. A call with no usable reply ends in one line
naming the operation, how long it waited and whether the app process is still running (the app hung up, did not answer
in time, or answered something that is not one UTF-8 JSON object); an error the app reports is one line too. After a
timeout the client closes the connection, since a late reply would otherwise answer the next call.

### Research traces (`--trace`)

A research build (`--research`) can add one opt-in qemu-user trace of the OEM app: `./sp16 build --research --trace MODE`.
`strace` streams the syscalls to `console.txt`, `gdb` opens the qemu-user gdb stub (`-g 1234`) for lldb, `pcmtap`
records the first 4 s of each playback stream, and `exec` records which translation blocks the app executes.

`exec` runs the app with `-d exec,nochain -dfilter 0x116f8..0x831ca8` (the 1.60 app's text range; `SP16_EXEC_RANGE` in
`guest-init.sh`). The raw log is one line per executed block, far too much for the serial console, so it goes to a fifo
that a small guest filter reads. qemu-user gives each guest thread its own cpu index (the first field of every log
line), so the filter dedupes on (cpu, address) and prints each pair once, in first-execution order, in batches as
`SP16_TB c<cpu> <hex> ...` lines in the session's `console.txt`. It also prints `SP16_TB_CPU c<cpu> t=<s>` when a thread
first appears, and `SP16_TB_END distinct=N lines=M t=<s> cpus=c0:n@t,...` when the app exits (`SP16_TB_STATS` with the
same fields every 10 s while new blocks appear): `t` is seconds since the first log byte, `cpus` the distinct blocks and
first-seen second per thread. The guest has no shared writable folder, so the console is the way to the host. The output
is bounded by the number of distinct (cpu, address) pairs (a 9-byte token each, at most 1,000,000 printed); another
console writer can cut into a batch line, which the reader tolerates. `python3 emulator/exectrace.py
<session>/console.txt --out tb.txt` collects them (distinct count, per-cpu counts checked against the guest's, first
five, range check) into one `c<cpu> 0x%08x` per line.
Tracing every block slows the app a lot: expect a long boot. A clean stop (`SIGTERM` to the app's process id, or Cmd-Q)
loses at most the last half second of addresses.

### Bench mode (`--no-ui-input`)

A script that drives a session through the socket and compares the LCD against reference data can be disturbed by
anyone using the Mac: macOS sends scroll-wheel events to the window under the pointer whether or not it is active, and
a scroll over an analog pot moves it (the AD TEST page shows the new value). `./sp16 run --no-ui-input` starts the app
in bench mode: the panel drops every click, drag, scroll, key and accessibility action before it reaches the guest,
the toolbar's input controls are replaced by a "Bench mode: input off" badge, and the app opens with `open -g` and does
not activate itself, so it takes no focus. Only the app's gate stops the scroll; opening in the background alone would
not. The research socket, LCD snapshots and panelshots are unchanged (they never pass through the panel's input
handlers), the window still closes and Cmd-Q still quits, and the session's `runtime.json` records `"ui_input": false`.

An audio build (`--audio file` or `both`) writes three WAVs of about 9.5 MB a minute each into the session folder.
`./sp16 run --no-audio-files` keeps the audio sink (it still reads every PCM packet from the guest and keeps the
counters that `metrics` reports, so the guest's audio path and timing are those of a file build) but opens no WAV:
for unattended runs that read no audio. A `both` build keeps playing through CoreAudio. The metrics' audio `mode` says
`counters` for a `file` build and `coreaudio` for a `both` build, each stream's `wav` is empty, and `runtime.json`
records `"audio_files": false`. The flag survives the relaunch after a firmware choice or import.
