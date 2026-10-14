# Contributing

Thank you for helping. Bug reports with a diagnostics zip (see "Reporting a bug" in the [README](README.md)) are as
welcome as code.

## Ground rules

- **No firmware or firmware-derived material, ever.** No updates, keys, firmware images, files extracted from an
  update, sample packs, projects or data images, not in commits, issues, pull requests or test fixtures. Tests use
  synthetic bytes only. Your imported firmware and builds live in the git-ignored `build/` and `private/` folders.
- **No mods.** The repository ships the tooling to develop and run mods ([docs/modding.md](docs/modding.md)), not
  patches against a firmware: no recipes with real offsets or hashes, no replacement files, no preload libraries that
  target a specific firmware.
- **No vendor logos or artwork.** The app uses a neutral name and a project-made icon.
- Never flash hardware or run a firmware's own update scripts, and never share a repacked update.

## Development

```sh
./sp16 doctor     # prerequisites
./sp16 test       # the firmware-free unit tests (what CI runs); needs Python 3.12 or later
./sp16 test --coverage   # the same with line and branch coverage (pip install coverage)
make build-dev && ./sp16 smoke   # end-to-end smoke test with a sped-up screencast (needs imported firmware)
```

- Python tools use the standard library only; the app is Swift against Apple system frameworks with no third-party
  packages. Match the style, naming and comment density of the code around your change.
- Add or update tests for behaviour you change. For work that needs real firmware (a build, an import, the hardware
  suite), say in the pull request what you ran and on which firmware version; never attach the firmware.
- When testing a build, use `--audio file` (`make build-record`) unless you need to hear it, and stop an emulator only
  by its exact process id.
- Update the README, `docs/` and the agent skills in `.agents/skills/` when you change a command, an option or a
  behaviour they describe.

Agents working in this repository follow [AGENTS.md](AGENTS.md).

## Pull requests

Keep a pull request to one change, describe what it does and how you tested it, and link the issue it fixes. For a
change to the app, the build, the guest or the panel model, run `./sp16 smoke` and give its result; a screencast is
optional (GitHub needs the web page to attach one). CI runs the
unit tests with coverage and refuses committed firmware, keys and images. Commit style and the full checklist are in
the [sp16-pull-request skill](.agents/skills/sp16-pull-request/SKILL.md).

## Licence

This project is released into the public domain under the [Unlicense](LICENSE). By contributing, you agree to
release your contribution under the same terms.
