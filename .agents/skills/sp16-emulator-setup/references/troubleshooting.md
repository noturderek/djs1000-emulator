# Troubleshooting

Start from the evidence: the session folder (`build/<build>/sessions/native-*/`, newest by modification time) holds
`runtime.json`, `console.txt` (guest console) and `metrics.json` (host and guest counters).

| Symptom | Check | Fix |
|---|---|---|
| `must be inside the repository`, `must be Git-ignored`, `owner-only permissions` | build folder location and mode | build under `build/` created with `mkdir -m 700`, inside a git clone |
| `requires an Apple Silicon Mac` | `uname -m` | not supported on Intel |
| QEMU configure or build fails | `qemu-configure.log`, `qemu-build.log` | install `ninja pkgconf glib pixman`; Python 3.12+ on PATH |
| `docker build` fails | `guest-build.log` | start Docker Desktop; retry (needs network for Debian packages) |
| `swiftc` fails | `swift-build.log` | install or update the Xcode Command Line Tools |
| Import screen: Python is required | `/usr/bin/python3 --version` | `xcode-select --install` |
| Title says `(unverified)` | `native.py firmware check --output <build> --id <id>` | the version is a candidate or unknown and runs on the reference measurements; verify it with the hardware suite in a research build |
| `SP16_FIRMWARE_ERROR` then the VM stops | `console.txt` | the image changed since import (re-import), an invalid manifest, or a pad calibration value outside the profile's range |
| Window black or empty | `console.txt` for `SP16_NATIVE_READY`; `metrics.json` frame counts | if QEMU exited, read the end of `console.txt`; if ready but no frames, the firmware's app has not drawn yet (wait 10 s) |
| Pads blink red and ignore hits | build flags | the firmware reports missing pad calibration; do not build with `--uncalibrated` |
| Input does nothing | `metrics.json` `input_results` and guest `events`, `unsupported`, `failed` | `unsupported` means that control has no mapping in this version's profile |
| No lamps | `metrics.json` `lamps_live`, guest `tx_frames`, `tx_bad_crc` | report the counters; `tx_bad_crc` must be 0 |
| Slow tempo or crackling | `uptime`; `console.txt` `speed=` lines | close other emulators (at most three); every stream should show `speed=1.00x` |
| App vanishes without a crash report | `log show --last 10m --predicate 'eventMessage CONTAINS "SIGPIPE"'` | report it with the console |
| Unknown hardware use | `metrics.json` `guest.unknown_devices`, `guest.unknown_ioctls` | the firmware uses an interface the shim does not serve; report it |
| A mod has no effect | `console.txt` `SP16_DEV` and `SP16_DEV_ERROR` lines | wrong base hash or original bytes: the recipe is refused and the firmware starts unchanged |
| Repacked update shows as unverified | the library Status reason | it was not rebuilt from a verified base with the declared recipes, or it was modified; it still runs |
