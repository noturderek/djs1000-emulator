# AGENTS.md

This repository is a macOS emulator for SP-16 compatible sampler firmware. It contains no firmware.

- Keep all agent rules and skills under `.agents/`. Claude Code reads `CLAUDE.md`, which imports this file; follow the same `.agents/skills/` entrypoints used by Codex. Do not add a separate `.claude/skills/` copy.
- To take someone from a fresh clone to a running emulator (build with audio, firmware of their choice, samples and
  demo projects on a virtual USB stick, launch), follow
  [.agents/skills/sp16-bootstrap/SKILL.md](.agents/skills/sp16-bootstrap/SKILL.md); `./sp16 bootstrap` does the
  deterministic part and asks for nothing the skill has not collected.
- To help someone set up, import firmware, run, load a modified firmware, run the hardware suite or troubleshoot,
  follow [.agents/skills/sp16-emulator-setup/SKILL.md](.agents/skills/sp16-emulator-setup/SKILL.md).
- Build and run through `./sp16` (`./sp16 help` lists every command; `make help` the ready-made configurations;
  `./sp16 doctor` checks prerequisites). `--dry-run` shows the exact tool command without running it.
- Never download firmware or the GPL source archive for the user without their explicit confirmation. The only
  download the tool makes itself is `./sp16 firmware download` (the official firmware update, from the official
  server only; pass `--yes` only after the user agreed to the file, source and size it printed with `--info`). Never print,
  copy or commit key bytes. Never commit updates, keys, firmware images, imported files or session captures: they
  live only under the git-ignored `build/` and `private/` folders.
- For a crash or bug, collect diagnostics (`./sp16 diagnostics`, or Help >
  Collect Diagnostics… in the app) and let the user attach the zip to an issue; never attach firmware, updates, keys or
  data images.
- Never flash hardware or run a firmware's own update scripts. Repacked updates are for this emulator only.
- Code lives in `emulator/`, user documentation in `docs/`. Run the unit tests with
  `./sp16 test` (`python3 -m unittest discover -s emulator/tests -p 'test_sp16_*.py'`) before proposing a change.
- When testing, build with `--audio file` (`make build-record`; records to WAV) unless the user asks to hear audio, and stop an emulator
  only by its exact process id (never by bundle id, never with `pkill`).
