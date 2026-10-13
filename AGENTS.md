# AGENTS.md

This repository is a macOS emulator for SP-16 compatible sampler firmware. It contains no firmware.

- To help someone set up, import firmware, run, load a modified firmware, run the hardware suite or troubleshoot,
  follow [.agents/skills/sp16-emulator-setup/SKILL.md](.agents/skills/sp16-emulator-setup/SKILL.md).
- Never download firmware or the GPL source archive for the user without their explicit confirmation. Never print,
  copy or commit key bytes. Never commit updates, keys, firmware images, imported files or session captures: they
  live only under the git-ignored `build/` and `private/` folders.
- Never flash hardware or run a firmware's own update scripts. Repacked updates are for this emulator only.
- Code lives in `emulator/`, user documentation in `docs/`. Run the unit tests with
  `python3 -m unittest discover -s emulator/tests -p 'test_sp16_*.py'` before proposing a change.
- When testing, launch with `--audio file` (records to WAV) unless the user asks to hear audio, and stop an emulator
  only by its exact process id (never by bundle id, never with `pkill`).
