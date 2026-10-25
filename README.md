# DJS-1000 Emulator

A macOS emulator for the Pioneer DJ DJS-1000 sampler. It emulates the machine around the firmware (panel, pads,
encoders, lamps, LCD, touch strip, audio codec, USB stick and internal storage) and boots a firmware update that
**you** supply. The application and everything else that runs inside the emulated machine come from that update; this
repository contains none of it.

> **Not affiliated.** This project is independent and is not affiliated with, endorsed by or supported by
> AlphaTheta Corporation or Pioneer DJ. It ships no firmware, no firmware-derived files, no key material and no vendor
> logos. You obtain the official firmware update yourself and use it under its own terms.

## Status

Early. The stock, official firmware (1.22) boots to the PROJECT screen on Apple silicon, with the panel, pads and audio
output working. Several features are missing (PRO DJ LINK, audio input, headphone output, MIDI, standby), custom mods
are out of scope for now, and the implementation is being reworked. The plan is in [docs/roadmap.md](docs/roadmap.md).

This project grew out of mattpstv's DJS1K Replicator, which is built on
[sp16-emulator](https://github.com/noturderek/sp16-emulator) (the TORAIZ SP-16 sibling: same update format and board
family). It keeps that project's layout and commands so the two can share an engine later. Other DJS-1000 emulators
are listed in [docs/related-projects.md](docs/related-projects.md).

## What you need

- A Mac with Apple silicon, macOS 13 or later (Intel is supported by the code but less tested).
- Xcode Command Line Tools, Python 3.12 or later, and the QEMU build dependencies; Docker for the build step only.
  `./sp16 doctor` checks all of this and prints the fix for anything missing.
- The DJS-1000 firmware update (`DJS1000.UPD`, version 1.22) from AlphaTheta's official support pages, and the key
  that opens it (the importer accepts a key file or the SP-16 GPL source archive). You download these yourself.

## Quick start

```sh
git clone https://github.com/noturderek/djs1000-emulator.git
cd djs1000-emulator
./sp16 doctor
./sp16 build --name djs --audio coreaudio     # or --audio file for a silent build
./sp16 firmware import --build djs --update DJS1000.UPD --key-file KEY
./sp16 firmware list                          # note the djs-1.22-... id
./sp16 run --name djs --firmware djs-1.22-<id>
```

The command is still called `./sp16`, as in sp16-emulator. `./sp16 help` lists every command. Builds, imported
firmware and virtual USB sticks live in the git-ignored `build/` folder. The full guide (USB stick, audio, commands,
troubleshooting) is [docs/usage.md](docs/usage.md).

## Repository layout

```text
sp16, Makefile    the command line (`./sp16 help`) and ready-made build configurations (`make help`)
emulator/         the emulator: build tool, firmware importer, guest, hardware shim, panel model, macOS app,
                  profiles, hardware suite, unit tests
docs/             usage guide, roadmap, architecture, modding tooling, hardware suite, profiles
.agents/skills/   agent skills for setup and pull requests
```

Run the firmware-free unit tests with `./sp16 test`.

## Reporting a bug

Collect diagnostics with **Help > Collect Diagnostics…** in the app, or `./sp16 diagnostics`, and attach the zip to an
issue. Nothing is sent automatically, and the zip never contains firmware, updates, keys or your data images. Details
are in [docs/usage.md](docs/usage.md#reporting-a-bug). **Never attach firmware, update files or key files to an issue.**

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): no firmware or firmware-derived material, no mods, and `./sp16 test` before a
pull request.

## License

The code in this repository is released into the public domain under the [Unlicense](LICENSE). Third-party components
that the build downloads (QEMU, Debian packages) keep their own licences; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Firmware you import is not covered by either and remains subject to
its owner's terms.
