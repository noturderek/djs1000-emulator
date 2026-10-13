# Modding guide

You can change how a firmware behaves in the emulator without rebuilding the emulator, and without ever changing
the imported firmware image. Changes are applied in the guest, into a tmpfs overlay above the read-only image, each
time the firmware's application starts.

Everything here runs on the emulator only. Nothing flashes hardware or runs a firmware's update scripts.

## Setup: a research build

The developer loop needs a build with the control socket:

```sh
mkdir -m 700 build private        # once; both are git-ignored and owner-only
python3 emulator/native.py build --research --audio file \
  --output build/sp16-dev --bundle-suffix dev
python3 emulator/native.py firmware import --output build/sp16-dev --update <your .UPD> --gpl-archive <part 1> <part 2>
```

`--bundle-suffix dev` gives this build its own bundle id, so it can run next to your normal build.

## A mod folder

```text
my-mod/
  mod.json       {"schema": 1, "id": "my-mod", "base_profile": "sp16-1.60", "description": "..."}
  recipes/       *.json patch recipes
  files/         replaced or added files at their guest paths (your own content only)
  src/           C sources of preload libraries, built by `fwdev.py sdk`
  preload/       ARM preload libraries (*.so), loaded after the hardware shim
```

Create one, optionally from the bundled example:

```sh
python3 emulator/fwdev.py new --mod private/my-mod --id my-mod --from-recipe utility-label
```

## Recipes

A recipe patches one file of the firmware by offset or by byte pattern. It names the file's SHA-256 before and after,
and how many matches it expects, so a recipe can never apply to the wrong firmware or apply partially. Replacements
keep the original length. The example `emulator/mods/utility-label.json` replaces the 8-byte string `UTILITY` with
`MOD LAB` in the application, so the HOME screen's Utility tile reads MOD LAB:

```json
{
  "schema": 1,
  "id": "utility-label",
  "base_profile": "sp16-1.60",
  "files": {
    "<application path>": {
      "base_sha256": "<hash of the stock file>",
      "patches": [{"offset": 8931164, "original_hex": "5554494c49545900",
                   "replacement_hex": "4d4f44204c414200", "expected_count": 1}],
      "result_sha256": "<hash after the patch>"
    }
  }
}
```

Use `"pattern_hex"` instead of `"offset"` to find the bytes by content. A recipe with wrong original bytes or a
wrong base hash is refused before anything changes; the firmware then starts unchanged and the console shows
`SP16_DEV_ERROR`.

## The loop

```sh
python3 emulator/fwdev.py check --output build/sp16-dev --mod private/my-mod   # preflight, no boot
python3 emulator/fwdev.py run   --output build/sp16-dev --mod private/my-mod   # launch with the overlay
# edit a recipe or a file, then run again: only the firmware's app restarts (about 0.2 s), and the screen settles
# a few seconds later
python3 emulator/fwdev.py test  --output build/sp16-dev --mod private/my-mod   # check + run + verify
```

`fwdev.py test` confirms the guest applied the overlay and the declared result hash, that lamps are live and audio
counters move, and optionally that an LCD region changed against a stock baseline after a tap
(`--baseline stock.png --expect-change X0 Y0 X1 Y1 --touch X Y`).

## Replaced files and preload libraries

- `files/<guest path>` replaces or adds a file in the overlay. `fwdev.py check` refuses unmodified copies of firmware
  files: put only your own content there.
- `src/*.c` are built by `python3 emulator/fwdev.py sdk --mod private/my-mod` into `preload/*.so` with the ARM cross
  toolchain in a throwaway Docker container, with the same flags as the hardware shim. The library binds to the
  firmware's own C library at run time and is loaded after the shim.

## Package a mod

```sh
python3 emulator/fwdev.py package --output build/sp16-dev --mod private/my-mod --out private/my-mod.tar.gz
```

The archive holds your recipes, sources and libraries with a manifest; it refuses unmodified firmware files.

## Repacked updates (emulator only)

```sh
python3 emulator/fwdev.py package --upd --output build/sp16-dev --mod private/my-mod \
  --gpl-archive <part 1> <part 2> --out private/my-mod.UPD
python3 emulator/native.py firmware import --output build/sp16-dev --update private/my-mod.UPD --gpl-archive <part 1> <part 2>
```

The repacked update is built from the verified base in your library and the mod's recipes, deterministically. It
carries an `SP16MODLAB-<base version>` marker and a manifest of its base and recipes. On import the emulator rebuilds
it from the base and the recipes and accepts it only if the result is identical; it then inherits the base's
profile. A changed byte anywhere is refused. Repacked updates take recipes only (`files/` and `preload/` stay
developer overlays).

A repacked update contains the vendor's firmware. Keep it in `private/`, never distribute it and never flash it to a
device.
