# Hardware conformance suite

The suite proves that the emulated hardware works for a given firmware version, without testing that firmware's
features. It is the only way a version's compatibility profile becomes `verified`, and only verified versions boot in
a normal build.

## Principle

Test the boundary the emulator owns, not the firmware behind it. The firmware is the oracle for hardware outcomes
only: a check fails when the emulated device delivers the wrong thing (a lost encoder detent, a bad frame CRC, a
stalled audio clock), never because a version lacks a menu or a feature. Where an outcome is visible only through
the firmware's UI, the check uses behaviour every version of this sampler shows (the home screen settles, the
selector moves a cursor one step per detent, BACK goes up, a pad hit on a loaded track plays and flashes its lamp,
a saved project survives a restart), and the version's profile supplies the data needed to observe it (screen
titles, probe coordinates, lamp roles). A check whose profile data is missing is `BLOCKED`, never `PASS`.

## Checks

45 checks, `HW-01` to `HW-45`:

| Area | Checks | What is proven |
|---|---|---|
| Boot and LCD | HW-01..04 | boots to a settled home screen with live lamps; framebuffer is faithful across window resizes; touch coordinates and timing (taps, hold, drag) |
| Panel wire protocol | HW-05..09 | panel frames in both directions are valid (length, markers, CRC), correctly paced and never dropped; every host event arrives exactly once; a 1000-event burst is lossless |
| Lamps and buttons | HW-10..15 | mono and RGB lamps, dim flag, one-hot groups and brightness decode; every button bit reaches the firmware; chords and holds |
| Encoders and selector | HW-16..20 | counters never lose or invent a detent; selector window and push; knob columns |
| Pads | HW-21..25 | pressure and contact words, velocity range, hold, simultaneous hits, calibration state |
| Touch strip | HW-26..29 | position and contact, strip lamps |
| Analog words | HW-30 | analog pot words reach the firmware |
| Audio | HW-31..35 | three playback ports, clock at 1.00x (tempo from the step lamps), sample-exact streams, level lamp |
| USB | HW-36..39 | attach, mount, eject, USB STOP, presence lamp, blank and dirty sticks |
| Storage, power, soak | HW-40..44 | persistence across clean and hard restarts, isolation between firmware data images, clean quit, 5-minute soak |
| Driver coverage | HW-45 | every device node and ioctl the firmware uses is served; unknown ones are reported |

Tiers: **smoke** (16 checks, about 6 minutes), **core** (all but the soak), **full** (all 45, about 30 minutes).
Only a full-tier run can mark a profile verified.

## Running it

The suite needs a research build that records audio to files (it never plays sound):

```sh
./sp16emu build --name sp16-dev --research --audio file --bundle-suffix dev     # or: make build-dev
./sp16emu firmware import --update <your .UPD> --gpl-archive <part 1> <part 2>   # once; every build shares the library
# the control build: the same build without pad calibration, sharing the firmware library (check HW-24)
./sp16emu build --name sp16-dev-uncal --research --audio file --uncalibrated --bundle-suffix dev-uncal   # or: make build-control
./sp16emu hwsuite --profile 1.60 --firmware <library id> --smoke      # or --tier core, or no tier for full
# --build / --control name other builds; without --profile the version of the selected firmware is used
```

The suite starts and stops its own emulator sessions (by exact process id), so quit the research builds before you
run it. It drives them only through the control socket and files, generates its own fixtures (test tones, FAT32
stick images; no firmware content), and writes `hwsuite-result.json` and `hwsuite-report.md` under
`build/sp16-dev/hwsuite/<run id>/`. It needs `pillow` and `numpy` (`python3 -m pip install pillow numpy`) and
`tesseract` for LCD titles.

| Exit code | Meaning |
|---|---|
| 0 | every selected check `PASS` or `N/A` |
| 1 | at least one `FAIL` |
| 2 | no `FAIL`, but at least one `BLOCKED` (missing profile data, unmapped lamp, new ioctl) |
| 3 | environment or usage error (no research socket, no `--audio file`, unreadable profile, host overloaded) |
| 4 | the session was lost during the run; partial results are written |

Timing checks record the host load and are reported `BLOCKED` when the Mac is too busy (more than three emulators,
or a high load average). Run the suite on an otherwise idle Mac.

## Marking a profile verified

`hwsuite.py --mark-verified` writes the profile's verification block, and nothing else may. It requires one
full-tier run without `--only` in which every check is `PASS` or `N/A`, where every `N/A` matches a capability the
profile declares absent, on the exact firmware image and profile being marked.

## Not in this suite

Firmware features (menus, sequencer editing, effects, scales, project workflows) belong to a per-version feature
check, not to hardware verification. Hardware that is not emulated is listed as such in every compatibility report:
MIDI, sync, audio inputs and live sampling, output routing beyond the main stereo pair, the analog filter, network.
