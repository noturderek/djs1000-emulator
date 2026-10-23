#!/usr/bin/env python3
"""Read the executed translation-block addresses of a research `--trace exec` session out of its console.txt.

A build made with `./sp16 build --research --trace exec` runs the OEM app under qemu-user with `-d exec,nochain`
restricted to the app's text range. The guest dedupes the log per guest thread (qemu-user gives each guest thread its
own cpu index) and prints each (cpu, block address) once as `SP16_TB c<cpu> <hex> <hex> ...` lines on the serial
console, i.e. in the session's console.txt, plus a per-cpu count and a seconds-since-trace-start stamp on the
`SP16_TB_STATS` and `SP16_TB_END` lines. This tool collects them: distinct (cpu, address) pairs in first-seen order, a
range check and the guest's own final counts, checked per cpu.

    python3 emulator/exectrace.py <session>/console.txt [--out FILE] [--range LO..HI]

`--out` writes one `c<cpu> 0x%08x` per line in first-execution order (a line from an older build, `SP16_TB <hex> ...`
without a cpu tag, is written as a bare `0x%08x`). The summary is one JSON document on stdout; the exit status is 1 when
the console holds no address. Standard library only.
"""
import argparse
import json
import re
import sys

# The serial console is shared: another writer can cut into a batch line, so a batch may end early or start mid-line.
# Only complete 8-digit tokens count; a cut-off token and what follows it are skipped. The `c<cpu>` tag is optional so
# a console from a build before the per-thread trace still reads (its blocks have no cpu).
BATCH = re.compile(r"SP16_TB (?:c(\d+) )?((?:[0-9a-f]{8} )*[0-9a-f]{8})(?![0-9a-f])")
END = re.compile(r"SP16_TB_END distinct=(\d+) lines=(\d+)(?: t=([0-9.]+))?(?: cpus=(\S+))?")
CPU_COUNT = re.compile(r"c(\d+):(\d+)@([0-9.]+)")
DEFAULT_RANGE = (0x116f8, 0x831ca8)  # the 1.60 application's text range, as in guest-init.sh


def blocks(text):
    """Distinct (cpu, block address) pairs of a console log, in first-seen order. A damaged token is skipped.
    The cpu is None for a block printed by a build without the per-thread trace."""
    seen = {}
    for match in BATCH.finditer(text):
        cpu = int(match.group(1)) if match.group(1) is not None else None
        for token in match.group(2).split():
            seen.setdefault((cpu, int(token, 16)), None)
    return list(seen)


def addresses(text):
    """Distinct block addresses across every cpu, in first-seen order."""
    return list(dict.fromkeys(pc for _, pc in blocks(text)))


def per_cpu_counts(found):
    counts = {}
    for cpu, _ in found:
        counts[cpu] = counts.get(cpu, 0) + 1
    return counts


def guest_cpus(text):
    """The last SP16_TB_END's per-cpu {cpu: (distinct, first-seen seconds)} or None."""
    reported = END.findall(text)
    if not reported or not reported[-1][3]:
        return None
    return {int(c): (int(n), float(t)) for c, n, t in CPU_COUNT.findall(reported[-1][3])}


def summarize(found, text, bounds=DEFAULT_RANGE):
    reported = END.findall(text)
    counts = per_cpu_counts(found)
    guest = guest_cpus(text)
    pcs = [pc for _, pc in found]
    summary = {"distinct": len(found), "distinct_addresses": len(set(pcs)),
               "first": ["%s0x%08x" % ("c%d " % c if c is not None else "", a) for c, a in found[:5]],
               "range": ["0x%x" % bounds[0], "0x%x" % bounds[1]],
               "outside_range": sum(1 for a in pcs if not bounds[0] <= a <= bounds[1]),
               "lowest": "0x%08x" % min(pcs) if pcs else None, "highest": "0x%08x" % max(pcs) if pcs else None,
               "guest_reported_distinct": int(reported[-1][0]) if reported else None,
               "guest_reported_seconds": float(reported[-1][2]) if reported and reported[-1][2] else None,
               "cpus": {("c%d" % c if c is not None else "none"): n
                        for c, n in sorted(counts.items(), key=lambda item: (item[0] is None, item[0]))}}
    if guest is not None:
        summary["guest_reported_cpus"] = {"c%d" % c: guest[c][0] for c in sorted(guest)}
        summary["cpu_first_seen_seconds"] = {"c%d" % c: guest[c][1] for c in sorted(guest)}
        # Per cpu the host must hold what the guest counted, unless the guest's END line itself was cut off or capped.
        summary["cpu_count_mismatch"] = sorted("c%d" % c for c in set(guest) | {k for k in counts if k is not None}
                                               if guest.get(c, (None,))[0] != counts.get(c, 0))
    return summary


def line_for(cpu, pc):
    return "%s0x%08x\n" % ("c%d " % cpu if cpu is not None else "", pc)


def parse_range(text):
    low, high = (int(part, 16) for part in text.split(".."))
    return low, high


def main(argv, stdout=None):
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("console", help="the session's console.txt")
    parser.add_argument("--out", help="write the distinct (cpu, address) pairs here, one `c<cpu> 0x%%08x` per line")
    parser.add_argument("--range", type=parse_range, default=DEFAULT_RANGE, metavar="LO..HI",
                        help="hex range the addresses are expected in (default 0x116f8..0x831ca8)")
    args = parser.parse_args(argv)
    with open(args.console, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    found = blocks(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("".join(line_for(cpu, pc) for cpu, pc in found))
    stdout.write(json.dumps(summarize(found, text, args.range), indent=2) + "\n")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
