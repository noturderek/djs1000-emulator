# SP16 Emulator

A macOS emulator of the sampler hardware that is compatible with the TORAIZ SP-16. It emulates the machine (panel,
pads, encoders, lamps, LCD, touch strip, audio codec, USB stick and internal storage) and boots a firmware update
that **you** supply. The application, its libraries and everything else that runs inside the emulated machine come
from that update; this repository contains none of it.

> **Not affiliated.** This project is independent and is not affiliated with, endorsed by or supported by
> AlphaTheta Corporation, Pioneer DJ or the makers of the TORAIZ SP-16. It ships no firmware, no firmware-derived
> files, no key material and no vendor logos. You download the firmware update and the GPL source archive yourself,
> from the official pages linked below, and use them under their own terms.

## What you need

- A Mac with Apple silicon (the emulator uses Hypervisor.framework), macOS 13 or later.
- Xcode Command Line Tools (`xcode-select --install`): Swift, `codesign`, and `/usr/bin/python3`, which the app uses
  to import firmware.
- Python 3.12 or later for the build tool (for example `brew install python@3.12`).
- QEMU build dependencies: `brew install ninja pkgconf glib pixman`. QEMU itself is downloaded from
  download.qemu.org as source and built on your Mac during the first build.
- Docker Desktop, for the one-time build of the Linux guest base image (Debian packages and this project's code).
  The emulator does not need Docker to run.
- About 3 GB of free disk space.
- For the hardware conformance suite only: `python3 -m pip install pillow numpy` and `brew install tesseract`.

## Quick start

### 1. Build

```sh
git clone <this repository> sp16-emulator
cd sp16-emulator
mkdir -m 700 build
umask 077
python3 emulator/native.py build --output build/sp16
```

The first build downloads and compiles QEMU and builds the guest base image with Docker: a few minutes on a recent
Mac (about 2 minutes measured), longer while Docker first downloads the Debian packages. The result
is `build/sp16/SP-16 Compatible Emulator.app`, plus the firmware library folder `build/sp16/library`. Everything
under `build/` is ignored by git and readable only by you; the build tool refuses any other place.

Further builds (a research build for modding or the hardware suite) can copy the verified QEMU from this one with
`--reuse-qemu-from build/sp16` instead of compiling it again.

Add `--audio coreaudio` to hear the emulator through your Mac's default output, or `--audio file` to record each
audio stream to a WAV file in the session folder instead (silent). Without `--audio` it is silent.

### 2. Get the firmware update and the GPL source archive

Download both yourself:

- **Firmware update.** The official firmware page:
  <https://support.alphatheta.com/en-us/articles/4405128836121>. Download the latest update (1.60), unzip it and
  keep the `.UPD` file. Version 1.50 is also supported if you have its update file.
- **GPL source archive.** The official open source page:
  <https://www.pioneerdj.com/en/support/open-source-code-distribution/gnu-open-source-license/>. Download the
  TORAIZ SP-16 source archive (two zip parts). Keep both parts, in order.

The importer needs your update file plus the GPL source archive, or the key file from that archive if you already
have it. It reads the key in memory for the import only; the key is never written to the firmware library, the
manifest, logs or the app.

Keep the update, the archive and anything you import in private folders. Do not commit or share them.

### 3. Import

Either start the app (step 4) with an empty library and use its import screen (choose the `.UPD`, then the key file
or both GPL archive parts), or use the command line:

```sh
python3 emulator/native.py firmware import --output build/sp16 \
  --update ~/Downloads/TORAIZSP16.UPD --gpl-archive ~/Downloads/gpl-part-1.zip ~/Downloads/gpl-part-2.zip
# or: --key-file <key file>
```

The import takes about 10 seconds. It checks the update, unpacks it inside the emulator's own Linux guest, and
stores a read-only firmware image and a manifest in the library. It prints a compatibility report. Any update that
is intact and was encrypted with the correct key is accepted, whoever made it. Refused imports (damaged or truncated
file, wrong key, not an update for this sampler, already imported) leave nothing behind.

### 4. Run

```sh
python3 emulator/native.py launch --output build/sp16
```

or open `build/sp16/SP-16 Compatible Emulator.app`. The first boot creates a 1 GB internal storage image for that
firmware; projects and settings persist there across launches. Close the window to stop the emulator.

`python3 emulator/native.py firmware list --output build/sp16` lists imports; `select`, `rename`, `remove`, `show`
and `check --id <id>` manage them. In the app, Firmware > Library does the same.

## Supported firmware versions

A version is **verified** when it has a verified compatibility profile in `emulator/profiles/` (one that passed the
full hardware conformance suite). Verified today: **1.50 and 1.60**. Any other update that was encrypted with the
correct key also runs, as **unverified**: it uses the reference measurements of the verified versions, the window title
says "(unverified)", the library lists it as unverified, and the app shows its compatibility report once. The
integrity checks are the same for every version (a changed library image is refused at boot).

| Version | Profile | Status |
|---|---|---|
| 1.60 | `sp16-1.60.json` | verified |
| 1.50 | `sp16-1.50.json` | verified |

To turn an unverified version into a verified one, see [docs/architecture.md](docs/architecture.md#adding-a-firmware-version).

## Load a modified firmware

There are three ways, described in [docs/modding.md](docs/modding.md):

1. **Recipes**: small JSON patch files (base hash, offset or byte pattern, replacement, expected result hash),
   applied when the app starts. `emulator/mods/utility-label.json` renames one HOME screen label as an example.
2. **Developer overlay**: a folder of replaced files, recipes and preload libraries shared into a running emulator;
   edit, then restart only the firmware's app in about ten seconds.
3. **Repacked update**: `fwdev.py package --upd` builds a modified update from a verified base and your recipes.
   It carries an `SP16MODLAB-<version>` marker, so it can never pass for an official update, and the importer accepts
   it only if it can rebuild it byte for byte from your verified base. Repacked updates are for this emulator only:
   never flash them to hardware and never distribute them (they contain the vendor's firmware).

## Hardware conformance suite

`emulator/hwsuite.py` checks that the emulated hardware behaves correctly for a given firmware (panel wire protocol,
lamps, encoders, pads, strip, audio clock, USB, storage, power, driver coverage) without testing that firmware's
features. It runs against a build made with `--research --audio file` plus an `--uncalibrated` control build that
shares its library, and starts its own sessions:

```sh
python3 emulator/hwsuite.py --profile emulator/profiles/sp16-1.60.json --build-dir build/sp16-dev \
  --firmware <library id> --control-build build/sp16-dev-uncal --smoke
```

See [docs/hardware-suite.md](docs/hardware-suite.md) for the full steps.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `must be Git-ignored` or `owner-only permissions` | Build under `build/` (created with `mkdir -m 700 build`) in a git clone of this repository. |
| `without symlink parents` | The clone's path must not pass through a symlink (on macOS `/tmp` is one; clone into your home folder). |
| `this native HVF build currently requires an Apple Silicon Mac` | Intel Macs are not supported. |
| `module 'hashlib' has no attribute 'file_digest'` | `python3` is too old (the Command Line Tools ship 3.9); put Python 3.12 or later first on your `PATH`. |
| QEMU build fails | Read `build/sp16/qemu-configure.log` or `qemu-build.log`; usually a missing `ninja`, `pkgconf`, `glib` or `pixman`. |
| QEMU link fails with `64-bit mach-o not 8-byte aligned` | A QEMU build folder configured with GNU `ar` (Homebrew `binutils` on `PATH`) from an older checkout; the build now uses Apple's `/usr/bin/ar`. Delete `build/sp16/qemu-build` and build again. |
| Guest image build fails | Start Docker Desktop and retry; read `build/sp16/guest-build.log`. |
| Window title says `(unverified)` | The version has no verified profile (see the table above); it runs on the reference measurements. `native.py firmware check --id <id>` prints the report. |
| Import: `update CRC does not match` or `not sector-aligned` | The update file is damaged or incomplete; download it again. |
| Import: `GPL archive is damaged or incomplete` | Give both zip parts, in order (part 1, then part 2). |
| Import: `the key does not decrypt this update` | The key does not belong to this update. |
| The app's import screen says Python is missing | Install the Xcode Command Line Tools (`xcode-select --install`). |
| Pads blink red and ignore hits | The firmware reports missing pad calibration; rebuild without `--uncalibrated`. |
| The window is black | Read `console.txt` in the newest folder under `build/sp16/sessions/`. |
| The app quit or crashed | Start it again: it offers to collect diagnostics. Then see [Reporting a bug](#reporting-a-bug). The newest folder under `build/sp16/sessions/` holds `app.log` (the app's log) and `qemu-stderr.txt`. |

## Reporting a bug

If the emulator crashes, will not boot, or behaves differently from the real hardware, collect diagnostics and attach
them to an issue (the bug report form asks for them).

1. In the app choose **Help > Collect Diagnostics…**. If the app did not shut down cleanly last time, it offers this
   itself the next time it starts. Without the app (for example when it will not start), run
   `python3 emulator/native.py diagnostics --output build/sp16`. Options: `--sessions N` (how many recent sessions,
   default 3), `--include-audio` (adds the sessions' WAV captures, which can be large), `--out file.zip`.
2. The result is one zip, `sp16-diagnostics-<time>.zip`, written to your Desktop (macOS may ask whether the app may
   access the Desktop; if you decline, or there is no Desktop, it is written to the build folder instead). The app
   shows it in Finder.
3. Open an issue with the "Bug report" form and attach the zip. Look through it first: it is an ordinary zip.

**Nothing is sent automatically.** The tool only writes the zip on your Mac.

What is in the zip: `report.json` (emulator version, build options, macOS and Mac model, CPU, free disk, Python,
whether Docker is installed, QEMU version, and the firmware library: name, version, kind, verified or unverified with
the reason, the update, application and image hashes and the compatibility findings); for the last sessions the guest
console, metrics, runtime description, the app's own log, QEMU's error output and the last screen picture; the app logs
and build logs (the last 500 lines of each); and macOS crash reports of the last 14 days for the app and QEMU.

What is never in it: firmware, updates (`.UPD`), key files or GPL archives, the firmware image, or your data image
(projects and settings). The tool refuses any file named `*.key`, `*.upd`, `*.sqfs`, `*.img`, `*.iso`, `*.sfs` or
`*.mot`. Text files are redacted: your home folder becomes `~`, your account name `<user>`, your Mac's name
`<host>`, your full name `<name>` and e-mail addresses `<email>`. Add `--key-file <your key file>` to the command to
also prove that the key does not occur anywhere in the zip (the key is never printed). Paths outside your home folder,
such as an external drive's name, are kept.

**Never attach firmware, update files, key files or data images to an issue**, whatever a maintainer asks.

## Repository layout

```text
emulator/            build tool, firmware importer, Linux guest base, hardware shim, panel model, macOS app,
                     compatibility profiles, example recipe, developer tools, hardware suite, unit tests
docs/                architecture, modding guide, hardware suite, profile reference
.agents/skills/      an agent skill that walks you through setup, import, run and troubleshooting
```

Run the firmware-free unit tests with:

```sh
python3 -m unittest discover -s emulator/tests -p 'test_sp16_*.py'
```

## License

The code in this repository is released under the MIT License ([LICENSE](LICENSE)). Third-party components that the
build downloads and builds on your Mac (QEMU, Debian packages in the guest base image) keep their own licences; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Firmware you import is not covered by either and remains subject
to its owner's terms.
