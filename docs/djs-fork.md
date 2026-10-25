# DJS-1000 work fork

This fork of [sp16-emulator](https://github.com/noturderek/sp16-emulator) (upstream snapshot of 2026-10-15) adds two
things: **Intel Mac support**, and a first **DJS-1000** firmware (`EP113_Main_R`, 1.22) running on the same emulated
hardware. The rules of the upstream repository still apply: no firmware, keys, firmware-derived files or vendor
artwork are ever committed.

## Status (2026-10-18)

| | Apple silicon | Intel (tested: Mac mini 2018, i7-8700B, macOS 15.7) |
|---|---|---|
| Unit tests, ARM contracts | unchanged | pass (510 tests; all contracts) |
| SP-16 1.60 stock | upstream (verified) | boots to HOME, encoders and audio work; not yet run through the hardware suite |
| DJS-1000 1.22 stock | not tried | boots; buttons, encoders, selector push, tempo slider, FX/headphone knobs, ribbon, pads and lamps work; samples load from the virtual stick and play through the Mac; the SEQUENCE screen's FULL/HALF/PARAM steps behave as the owner's manual describes; candidate profile, unverified |

The Apple silicon path is meant to be unchanged (same QEMU target and arguments as upstream) but has not been re-run
on an Apple silicon Mac since the changes.

## What changed

- **Intel Macs** (`emulator/hostarch.py`): one table per kind of Mac picks the QEMU target, machine, console, virtio
  transport, Debian guest kernel and Docker platform. Intel gets an x86_64 guest (q35, `ttyS0`, `virtio-*-pci`) under
  Hypervisor.framework. The OEM ARM32 app runs under `qemu-arm-static` either way. q35 boots through SeaBIOS, so the
  build stages QEMU's own `bios-256k.bin`, `linuxboot*.bin` and `kvmvapic.bin` from the pinned source into
  `qemu-firmware/` next to the QEMU binary and passes `-L`.
- **Panel profiles**: `panel.word_order` (`big`|`little`) and `panel.pad_words` let a profile describe a panel whose
  16-bit RX fields are little-endian or live at other offsets. Selector and knob offsets come from `panel.counters`.
- **Codec channel limits** (`hardware_stub.c`): the emulated PCMs report the real codecs' channel ceilings (AK4452: 2,
  AK4458: 16). Without this the DJS app opened 4-channel streams on ALSA's null plugin and its JUCE ALSA thread crashed.
- **DJS-1000**: `/root/pdj/EP113_Main_R` is a second app candidate of the product, and `profiles/djs-1.22.json` is a
  candidate profile with the DJS panel layout measured through the factory test UI (buttons, encoders, analog words,
  pad words).
- **Application families** (2026-10-18): the product descriptor's `app_families` maps each application to an id
  prefix, a display name and a reference profile (`EP085_Main_R` → `sp16` / SP-16 / `sp16-1.60`, `EP113_Main_R` →
  `djs` / DJS-1000 / `djs-1.22`). Another DJS version without its own profile (none tried yet; only 1.22 is at hand)
  boots on the DJS measurements and the DJS panel (`panel-layout-<family reference>.json`), imports as
  `djs-<version>-<hash>` with the name "DJS-1000 <version>", and `firmware profile` starts its candidate from
  `djs-1.22`. Entries imported earlier keep their ids and still boot. Ours was re-imported on 2026-10-23 as
  `djs-1.22-01b9d16b0ed3` (product `djs1000`); the old `sp16-1.22-…` entry, with its storage, is kept in `build/retired/`.
- **DJS lamps**: the DJS TX frame has no 0xaa marker (`panel.tx_marker: null`) and packs single-colour lamps as
  2-bit fields (`pair` lamp kind: 00 off, 01 full, 11 dim; `panel_map` E15). Lamp roles were measured with the
  factory LED and BUTTONS pages; `panel.lamp_colors` sets presentation colours. TEMPO RESET (pair 31) and the USB A
  port ring (pair 32, `usb_led`, a bar under the port) were matched to mattpstv's unit; `panel.lamp_dim_off` draws a
  role's dim code as dark (the USB ring after USB STOP).
- **Tempo slider**: an analog word may name a fixed reference word (`[offset, inverted, [ref_offset, ref_word]]`).
  The DJS slider is the wiper B at 0x58 next to a zero-point word A at 0x56 (held at 512). The firmware scales each
  half to its end stop, (B − A)/(1023 − A) above A and (B − A)/A below, so it responds linearly (raw-word sweep,
  2026-10-18). The first version sent A = 1023 − B, which made the response three times steeper near the centre. The panel draws it as an analog control of shape `fader` (id 104). The window
  now also sends FX LEVEL/DEPTH and headphone LEVEL (ids 101/102), which it previously drew but never sent.
- **DJS pad calibration**: `/root/data/PadCalibration.xml` with `Pad01_Final` and `Pad01_Provisional1-3` per pad (the
  firmware keeps values in 217..454); with it the pads stop blinking and trigger samples.
- **Host audio**: when no `ak4458audio,0` stream opens (DJS), the Mac plays `ak4452audio,1` channels 0/1 (the master
  DAC) instead.
- **`./sp16 run --testmode`** (research builds) starts the firmware's factory test UI.
- Research `--trace` builds copy the OEM app's own SIGSEGV report to the console (`SP16_APP_SIGSEGV`).
- A Swift expression in `panel-view.swift` is split up so the type checker finishes on slower Macs.

- **Products (2026-10-23):** one update format, two products. In `profiles/products/sp16.json` each application
  family names its product: EP085 → `sp16`, EP113 → `djs1000`. A DJS import's manifest says `djs1000`, its id is
  `djs-<version>-<hash>` and its name `DJS-1000 <version>`; the library window accepts both products. Entries
  imported before keep `sp16` and still boot.
- **Test kit:** `python3 emulator/make_test_kit.py build/test-kit && ./sp16 usb add build/test-kit/*.wav` writes the
  synthetic HW-KICK / HW-SNARE / HW-HAT / HW-TONE440 used in all DJS measurements (byte-identical to the 2026-10-17
  originals). The repository ships no audio; no factory content is needed or included.
- **Commit ids changed on 2026-10-23** when the commit author was set to mattpstv; see
  [commit-id-map.md](commit-id-map.md) for ids quoted in older notes.

## Running the DJS-1000 firmware

```sh
./sp16 build --name sp16-dev --research --audio file --bundle-suffix dev
./sp16 firmware import --build sp16-dev --update DJS1000.UPD --key-file KEY     # same key as the SP-16
./sp16 firmware list                                                          # note the djs-1.22-… id (ours: djs-1.22-01b9d16b0ed3)
./sp16 run --name sp16-dev --firmware djs-1.22-<id> [--testmode]
```

The DJS-1000 gets **its own panel** (`emulator/panel-layout-djs-1.22.json`, generated by
`emulator/layouts/make-djs-layout.py` from positions measured on a top-panel photo; drawn with the emulator's own
styles, no vendor artwork). The app picks `panel-layout-<profile id>.json` when the booted firmware's profile has one,
so the SP-16 keeps its panel. Every DJS button, the six encoders, the selector and FX LEVEL/DEPTH are wired; INPUT
LEVEL is drawn but, like the SP-16's analog filter pots, has no field in the panel frame.

## Known gaps

- Two lamp fields have no drawn role: 0x69 f2 is the selector ring (blinks while a project loads; the layout has no
  ring lamp yet) and 0x6a f2 is lit from boot (HOME button?). The PAD/BUTTON BRIGHTNESS byte (0x6d?) is unconfirmed.
- Pad velocity maps 1..127 to pressure word A 187..874 (B 512). Measured on 1.22 at the UTILITY defaults (2026-10-20):
  A 184 is the first hit (the SP-16's 169 was silent) and A 828 is full level. The other PAD CURVE / THRESHOLD /
  SENSITIVITY settings change the response as on hardware. The full table is in the sp16-research
  `members/mattpstv/djs-1000/wiki/concepts/djs-pad-features.md`. The real pads' force-to-word scale is unknown.
- The factory TOUCH PANEL page reads raw touch from RX 0x5e/0x60, which the emulator does not send (normal screens
  take X11 taps and work).
- No MIDI, network (SYNC opens "No LINKED Players found") or audio input, as upstream.
- Ejecting the virtual stick makes the firmware unload samples it loaded from USB (real behaviour); read projects
  with the read-only method in `members/mattpstv/djs-1000/scripts/pullprj.py` instead.
- Name **DJS1K Replicator** (2026-10-24; working name "DJS-1000 Compatible Emulator" on 10-23): `DJS1K Replicator.app`,
  executable `DJS1KReplicator`, bundle id `local.djs1k-replicator`, icon file `djs1k-replicator`, repo `djs1k-replicator`.
  Crash-report lookups still accept the old `SP16Emulator-*.ips` names. Still upstream-named: the `./sp16` command and
  the `sp16` update-format descriptor.

## Debugging the DJS app with lldb (read-only)

Upstream's `trace-gdb-bridge` branch (imported 2026-10-18) lets a host debugger reach qemu-user's gdbstub. Tested on
the Intel Mac with DJS 1.22:

```
./sp16 build --name djs-gdb --research --trace gdb --audio coreaudio --bundle-suffix gdb --library build/library
./sp16 run --name djs-gdb --firmware djs-1.22-01b9d16b0ed3 --no-usb --ephemeral   # app halts at the loader entry
python3 emulator/gdb-bridge.py --build djs-gdb                                       # 127.0.0.1:1234
lldb -o 'gdb-remote 127.0.0.1:1234'      # stops at pc 0x3ffdad40 (ld-linux entry), arch arm
(lldb) process detach                    # the app runs on to its UI
```

- qemu-user's stub accepts **one** connection per app start. End every session with `process detach`; quitting lldb
  or a batch that stops on an error leaves the app halted, and a second attach then times out. Restart the build
  (`./sp16 run ...` after stopping the app by its PID) to attach again.
- Use `--no-usb --ephemeral` when the normal `djs` build is also running (they would share the stick image).
- Our limits: inspect only (registers, memory, breakpoints). Writing memory or registers is runtime patching and needs
  Matt's OK, like the mod loop.

## Next

Release-candidate inventory and related emulators (commit-collective/djs1000-emu, cubiq4): see
`../../djs-sp16-firmware-research/docs/sharing-plan-2026-10-23.md` and `related-projects.md`.


1. ~~Velocity vs PAD CURVE, per-step velocity, velocity and the LFO~~ (done 2026-10-20; see the wiki). Left: the real pads' force scale (needs the unit's PAD calibration page).
   QUANTIZE during REC and the OFFSET lock done 2026-10-22; the SEQUENCE screen's QUANTIZE button (clears entry 10 for
   the track's whole pattern) done 2026-10-23. A second scripted session: `--ephemeral --no-ui-input
   --usb-image build/stick-lab.img` (a `cp -c` clone), never the stick.img another session holds.
2. Draw the selector ring as a lamp; identify 0x6a f2; brightness byte; the touch words for the factory page.
3. MIDI over the USB gadget (`g_midi`) or DIN, so the external chainer runbook can run against the emulator.
4. DJS checks for the hardware suite; try an older DJS update if one turns up (the families path is unit-tested only).
5. Re-run everything on an Apple silicon Mac.
