# Roadmap

Goal: the latest official DJS-1000 firmware (1.22) runs on a Mac in a way other people can build, test and extend.
No custom mods in the first releases. The shared engine with sp16-emulator comes later.

## Phase 0: port (done)
- mattpstv's DJS1K Replicator brought over onto sp16-emulator `125b35d`, same folder layout and commands.
- `./sp16 test`: 515 firmware-free tests pass on Apple silicon.
- Built, imported and booted DJS 1.22 on Apple silicon (silent audio build).

## Phase 1: publish and make contributable
- Push the repository; CI (`.github/workflows/tests.yml`) runs on it.
- Finish the README for the DJS-1000 (screenshots without vendor art, DJS panel map).
- Issue labels and a short "good first issue" list taken from the gaps below.

## Phase 2: missing features (in this order)
1. Headphone output and the standby/power key.
2. Audio input (LIVE SAMPLING).
3. PRO DJ LINK (the app needs a faked ethernet link and player announcements).
4. MIDI (DIN and USB gadget).
5. Selector ring lamp, brightness byte, touch words for the factory touch page.

## Phase 3: tests
- Keep the firmware-free unit and contract tests green for every change.
- DJS profile and panel tests for every new control and lamp (like the existing `test_sp16_djs_*`).
- A DJS hardware-suite tier (boot to PROJECT, panel round trip, pads and velocity, tempo slider, audio, USB stick),
  then promote the `djs-1.22` profile from candidate to verified.
- A smoke test with screencast for the DJS (like `./sp16 smoke`).

## Phase 4: shared engine
Extract what sp16-emulator and this project share (QEMU host, guest image, importer, USB, audio transport,
diagnostics, hwsuite framework) once the DJS side passes its first verified hardware-suite run.
