---
name: sp16-bootstrap
description: Bootstrap the SP16 Emulator from a fresh clone to a running emulator with audio, the firmware version the user chooses, and a virtual USB stick filled with samples and demo projects. Use for "bootstrap the emulator", "set up the emulator", "install it", "get the emulator running", "get it running with sound", "put the factory samples and demo projects on it". Prompts the user for every manual step (firmware, key, samples). For troubleshooting, modded firmware and the hardware suite use sp16-emulator-setup.
---

# SP16 Emulator bootstrap (Claude Code entry)

The full instructions are in [.agents/skills/sp16-bootstrap/SKILL.md](../../../.agents/skills/sp16-bootstrap/SKILL.md),
the same skill that Codex and other agents read. Read that file now, completely, and follow it step by step, starting
from the repository root. This page exists only so Claude Code lists the skill.

Reminders that apply before you open it: the repository ships no firmware, keys or samples (the user supplies them);
ask before every manual step and before installing anything; never print key bytes; download only after the user says
yes to the file name, source and size; never mount the USB stick while the emulator runs.
