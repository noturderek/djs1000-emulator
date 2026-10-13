---
name: sp16-emulator-setup
description: Guide a user through setting up the SP16 Emulator on their Mac - check prerequisites, build, have the user download the firmware update and GPL source archive themselves, import, run, load a modified firmware (recipes, developer overlay, repacked update), run the hardware conformance suite, and troubleshoot. Use for "set up the emulator", "build it", "import my firmware", "it won't boot", "load my mod", "run the hardware suite".
---

# SP16 Emulator setup

Work from the repository root. The user's README is the reference; this skill adds the order, the checks and the
rules. Commands assume `build/sp16` as the build; use the user's paths if they differ.

## Rules (always)

- **Firmware and the GPL archive are the user's to download.** Give them the two official links from the README and
  let them download. Download for them only after they explicitly confirm, naming the file and source first.
- **Never print, copy, log or commit key bytes.** Pass key files and archives by path only. Do not `cat`, `xxd`,
  `strings` or hash-print a key file. Do not describe how the update is encrypted.
- **Never commit** updates, keys, archives, imported libraries, data images, session folders or captures. Keep them
  in `build/` or `private/` (both git-ignored; create with `mkdir -m 700`).
- **Never flash hardware**, run a firmware's own scripts, or distribute a repacked update.
- **Audio:** when you launch to test, use builds made with `--audio file` (silent, records WAVs). Use
  `--audio coreaudio` only when the user wants to hear it.
- **Stopping:** stop an emulator only by its exact process id, found from its build path
  (`ps -axo pid,command | grep -F "<build dir>/"`), then confirm its `qemu-system-aarch64` child exits. Never quit by
  bundle id, never `pkill` or `killall`. Each running emulator uses about 3 CPU cores; keep at most three.

## 1. Check prerequisites

Run and report each; stop at the first missing item and tell the user how to fix it:

```sh
uname -sm                                  # Darwin arm64 (Apple silicon required)
sw_vers -productVersion                    # 13 or later
xcode-select -p && swiftc --version        # Xcode Command Line Tools
/usr/bin/python3 --version                 # used by the app's import screen
python3 --version                          # 3.12 or later for the build tool
command -v ninja pkg-config && brew list glib pixman   # QEMU build dependencies
docker info --format '{{.ServerVersion}}'  # Docker Desktop running (first build only)
git rev-parse --show-toplevel              # a git clone (the build checks git-ignore rules)
```

Missing tools: `xcode-select --install`, `brew install python@3.12 ninja pkgconf glib pixman`, start Docker Desktop.

## 2. Build

```sh
mkdir -m 700 -p build && umask 077
python3 emulator/native.py build --audio file --output build/sp16
```

The first build takes 10-20 minutes (QEMU from source, then the Docker guest image). On failure read the log the
error names in `build/sp16/` (`qemu-configure.log`, `qemu-build.log`, `guest-build.log`, `swift-build.log`).
A rebuild into the same folder reuses the QEMU source; `--reuse-qemu-from <other build>` copies a verified QEMU.

## 3. Firmware: the user downloads it

Tell the user to download, from the links in the README:

1. the latest firmware update (unzip it; keep the `.UPD`);
2. the GPL source archive for this sampler (two zip parts), or use the key file they already have from it.

Ask where they saved them. Check the paths exist (`ls -l`), nothing more.

## 4. Import

```sh
python3 emulator/native.py firmware import --output build/sp16 --update <path to .UPD> \
  --gpl-archive <part 1 zip> <part 2 zip>        # or: --key-file <key file>
```

Read the JSON result: `firmware_imported` with an id and a report, or `firmware_refused` with the reason. Then
`python3 emulator/native.py firmware list --output build/sp16`. Typical refusals:

| Reason | Meaning |
|---|---|
| `update CRC does not match`, `not sector-aligned` | damaged or partial download: download again |
| `GPL archive is damaged or incomplete` | both parts are needed, part 1 first |
| `the key does not decrypt this update` | wrong key for this update |
| not an update for this sampler (application missing) | a different product's update |
| already imported | it is in the library; `firmware list` |

A version without a verified profile imports but is refused at boot (see section 7 to verify one).

## 5. Run

```sh
python3 emulator/native.py launch --output build/sp16      # or --firmware <id>
```

Confirm it booted: the newest `build/sp16/sessions/native-*/console.txt` contains `SP16_NATIVE_READY`. The window
shows the panel; the LCD settles on the home screen after about 10 seconds.

## 6. Modified firmware

Follow `docs/modding.md`. Short version, with a research build:

```sh
python3 emulator/native.py build --reuse-qemu-from build/sp16 --research --audio file \
  --output build/sp16-dev --bundle-suffix dev
python3 emulator/native.py firmware import --output build/sp16-dev --update <.UPD> --gpl-archive <parts>
python3 emulator/native.py firmware select --output build/sp16-dev --id <1.60 id>   # the example targets 1.60
python3 emulator/fwdev.py new  --mod private/my-mod --id my-mod --from-recipe utility-label
python3 emulator/fwdev.py test --output build/sp16-dev --mod private/my-mod
```

`utility-label` makes the HOME screen's Utility tile read MOD LAB on 1.60. For a repacked update use
`fwdev.py package --upd ... --out private/<name>.UPD` and import it; a tampered copy must be refused.

## 7. Hardware suite

Needs a `--research --audio file` build with the firmware imported, plus a control build made with `--uncalibrated`
sharing its library. Quit both apps first; the suite starts and stops its own sessions. Needs `pillow`, `numpy`
and `tesseract`.

```sh
python3 emulator/native.py build --reuse-qemu-from build/sp16 --research --audio file --uncalibrated --library build/sp16-dev/library \
  --output build/sp16-dev-uncal --bundle-suffix dev-uncal
python3 emulator/hwsuite.py --profile emulator/profiles/sp16-<version>.json --build-dir build/sp16-dev \
  --firmware <library id> --control-build build/sp16-dev-uncal --smoke
```

Exit 0 pass, 1 fail, 2 blocked (missing profile data), 3 environment, 4 session lost. Only `--tier full` with every
check passing can mark a profile verified (`--mark-verified`). Run it on an idle Mac. Details:
`docs/hardware-suite.md`; adding a version: `docs/architecture.md#adding-a-firmware-version`.

## 8. Troubleshooting

See [references/troubleshooting.md](references/troubleshooting.md).
