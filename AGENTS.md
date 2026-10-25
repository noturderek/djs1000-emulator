# AGENTS.md

This repository is a macOS emulator for DJS-1000 compatible firmware. It contains no firmware.

- Keep all agent rules and skills under `.agents/`. Claude Code reads `CLAUDE.md`, which imports this file. Do not add
  a separate `.claude/skills/` copy.
- Code lives in `emulator/`, user documentation in `docs/`.
- Never download firmware for the user without their explicit confirmation. Never print, copy or commit key bytes.
  Never commit updates, keys, firmware images, imported files or session captures: they live only under the
  git-ignored `build/` and `private/` folders.
- Never flash hardware or run a firmware's own update scripts.
- This is a public repo: no vendor logos, no private paths, no personal notes. Research stays in the private
  `internal/` workspace.
