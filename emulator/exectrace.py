#!/usr/bin/env python3
"""Read the executed translation-block addresses of a research `--trace exec` session out of its console.txt.

A build made with `./sp16 build --research --trace exec` runs the OEM app under qemu-user with `-d exec,nochain`
restricted to the app's text range. The guest dedupes the log and prints each block address once as `SP16_TB <hex>
<hex> ...` lines on the serial console, i.e. in the session's console.txt. This tool collects them: distinct
addresses in first-seen order, a range check and the guest's own final count.

    python3 emulator/exectrace.py <session>/console.txt [--out FILE] [--range LO..HI]

`--out` writes one `0x%08x` address per line. The summary is one JSON document on stdout; the exit status is 1 when the
console holds no address. Standard library only.
"""
import argparse
import json
import re
import sys

# The serial console is shared: another writer can cut into a batch line, so a batch may end early or start mid-line.
# Only complete 8-digit tokens count; a cut-off token and what follows it are skipped.
BATCH = re.compile(r"SP16_TB ((?:[0-9a-f]{8} )*[0-9a-f]{8})(?![0-9a-f])")
END = re.compile(r"SP16_TB_END distinct=(\d+) lines=(\d+)")
DEFAULT_RANGE = (0x116f8, 0x831ca8)  # the 1.60 application's text range, as in guest-init.sh


def addresses(text):
    """Distinct block addresses of a console log, in first-seen order. A damaged token is skipped."""
    seen = {}
    for match in BATCH.finditer(text):
        for token in match.group(1).split():
            seen.setdefault(int(token, 16), None)
    return list(seen)


def summarize(found, text, bounds=DEFAULT_RANGE):
    reported = END.findall(text)
    return {"distinct": len(found), "first": ["0x%08x" % a for a in found[:5]],
            "range": ["0x%x" % bounds[0], "0x%x" % bounds[1]],
            "outside_range": sum(1 for a in found if not bounds[0] <= a <= bounds[1]),
            "lowest": "0x%08x" % min(found) if found else None, "highest": "0x%08x" % max(found) if found else None,
            "guest_reported_distinct": int(reported[-1][0]) if reported else None}


def parse_range(text):
    low, high = (int(part, 16) for part in text.split(".."))
    return low, high


def main(argv, stdout=None):
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("console", help="the session's console.txt")
    parser.add_argument("--out", help="write the distinct addresses here, one 0x%%08x per line")
    parser.add_argument("--range", type=parse_range, default=DEFAULT_RANGE, metavar="LO..HI",
                        help="hex range the addresses are expected in (default 0x116f8..0x831ca8)")
    args = parser.parse_args(argv)
    with open(args.console, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    found = addresses(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("".join("0x%08x\n" % a for a in found))
    stdout.write(json.dumps(summarize(found, text, args.range), indent=2) + "\n")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
