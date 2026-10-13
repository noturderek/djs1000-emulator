# Troubleshooting

Start from the evidence: the session folder (`build/<build>/sessions/native-*/`, newest by modification time) holds
`runtime.json`, `console.txt` (guest console) and `metrics.json` (host and guest counters).

| Symptom | Check | Fix |
|---|---|---|
| Anything fails before or during a build | `./sp16emu doctor` | it prints a `fix:` for each missing prerequisite |
| `must be inside the repository`, `must be Git-ignored`, `owner-only permissions`, `not a git work tree` | build folder location and mode | use a git clone; `./sp16emu` builds under its `build/` (owner-only, git-ignored); `chmod 700 build` if the mode is wrong |
| `build/<name> already exists` | `./sp16emu builds` | `./sp16emu run`, or `build --rebuild` to rebuild in place (keeps firmware and data), or another `--name` |
| `requires an Apple Silicon Mac` | `uname -m` | not supported on Intel |
| QEMU configure or build fails | `qemu-configure.log`, `qemu-build.log` | install `ninja pkgconf glib pixman` (`brew install ninja pkgconf glib pixman`); the build tools need Python 3.12+ (`brew install python@3.12`) |
| `docker build` fails | `guest-build.log` | start Docker Desktop; retry (needs network for Debian packages) |
| `swiftc` fails | `swift-build.log` | install or update the Xcode Command Line Tools |
| Import screen: Python is required | `/usr/bin/python3 --version` | `xcode-select --install` |
| `usb ...: is in use by a running emulator (process id N)` | `ps -axo pid,command \| grep -F "<image>"` | quit that emulator, stopping only the printed ids (and its `qemu-system-aarch64` child); never mount a stick the emulator writes to |
| `usb ...: already attached to this Mac`, or `run` starts without the stick | `hdiutil info \| grep -F stick.img` | `./sp16emu usb unmount`; the commands never force a busy volume without saying so |
| `usb ...: cannot attach` | the image is zero-filled or not FAT32 | `./sp16emu usb create --force` (deletes the stick's content), or `usb add` which formats a blank image |
| Samples do not show up in the sampler | `./sp16emu usb ls`; the `run` notes say whether the stick was attached | the stick must exist before `run` (`usb add`), be in `PIONEER DJ SAMPLER/Samples`, and not be skipped by `--no-usb`; check the format (PCM 16- or 24-bit 44.1 kHz WAV) |
| Title says `(unverified)` | `./sp16emu firmware check --id <id>` | the version is a candidate or unknown and runs on the reference measurements; verify it with the hardware suite in a research build |
| `SP16_FIRMWARE_ERROR` then the VM stops | `console.txt` | the image changed since import (re-import), an invalid manifest, or a pad calibration value outside the profile's range |
| Window black or empty | `console.txt` for `SP16_NATIVE_READY`; `metrics.json` frame counts | if QEMU exited, read the end of `console.txt`; if ready but no frames, the firmware's app has not drawn yet (wait 10 s) |
| Pads blink red and ignore hits | build flags | the firmware reports missing pad calibration; do not build with `--uncalibrated` |
| Input does nothing | `metrics.json` `input_results` and guest `events`, `unsupported`, `failed` | `unsupported` means that control has no mapping in this version's profile |
| No lamps | `metrics.json` `lamps_live`, guest `tx_frames`, `tx_bad_crc` | report the counters; `tx_bad_crc` must be 0 |
| Slow tempo or crackling | `uptime`; `console.txt` `speed=` lines | close other emulators (at most three); every stream should show `speed=1.00x` |
| The app quit or crashed | `sessions/native-*/app.log` (the app's log), `qemu-stderr.txt`, and `~/Library/Logs/DiagnosticReports/SP16Emulator-*.ips` | collect diagnostics (section 8 of the skill: `./sp16emu diagnostics` or Help > Collect Diagnostics…) and attach the zip to a bug report |
| App vanishes without a crash report | `log show --last 10m --predicate 'eventMessage CONTAINS "SIGPIPE"'` | report it with the console |
| Unknown hardware use | `metrics.json` `guest.unknown_devices`, `guest.unknown_ioctls` | the firmware uses an interface the shim does not serve; report it |
| A mod has no effect | `console.txt` `SP16_DEV` and `SP16_DEV_ERROR` lines | wrong base hash or original bytes: the recipe is refused and the firmware starts unchanged |
| Repacked update shows as unverified | the library Status reason | it was not rebuilt from a verified base with the declared recipes, or it was modified; it still runs |
