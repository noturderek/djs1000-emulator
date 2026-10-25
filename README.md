# DJS-1000 Emulator

A macOS emulator of the sampler/sequencer hardware that is compatible with the DJS-1000. It aims to emulate the
machine (panel, pads, encoders, lamps, display, audio, USB storage) and to boot a firmware update that **you**
supply. This repository contains none of that firmware.

> **Status: early scaffold.** Nothing runs yet. The layout below mirrors
> [sp16-emulator](https://github.com/noturderek/sp16-emulator) and will fill in as the work progresses.

> **Not affiliated.** This project is independent and is not affiliated with, endorsed by or supported by
> AlphaTheta Corporation or Pioneer DJ. It ships no firmware, no firmware-derived files, no key material and no
> vendor logos. You obtain any firmware update yourself, from the official source, and use it under its own terms.

## What you will need

- A Mac with Apple silicon, macOS 13 or later.
- Xcode Command Line Tools (`xcode-select --install`).
- Python 3.12 or later for the build tools.
- A firmware update for the DJS-1000, downloaded by you from the official support pages.

The exact prerequisites will be listed here, and checked by a `doctor` command, once the build exists.

## Quick start

Not available yet. Planned shape, as in sp16-emulator:

```sh
git clone https://github.com/noturderek/djs1000-emulator.git
cd djs1000-emulator
```

## Planned scope

- Build and run the emulator from the repository root with a single launcher.
- Import a user-supplied firmware update into a local, git-ignored library.
- Virtual USB stick for samples and projects.
- Tooling for developing and testing firmware mods (tooling only, no mods or patches shipped).
- A firmware-free unit test suite and a hardware conformance suite.

## Repository layout

```text
emulator/         emulator code (build tool, importer, hardware model, app, tests)
docs/             user documentation
.agents/skills/   agent skills
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): no firmware or firmware-derived material, no mods.

## License

The code in this repository is released into the public domain under the [Unlicense](LICENSE). Firmware you import
is not covered by it and remains subject to its owner's terms.
