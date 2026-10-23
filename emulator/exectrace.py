#!/usr/bin/env python3
"""Read the executed translation-block addresses of a research `--trace exec` session out of its console.txt.

A build made with `./sp16 build --research --trace exec` runs the OEM app under qemu-user with `-d exec,nochain`
restricted to the app's text range. Every app start is a new qemu-user that numbers its guest threads from cpu 0 again,
so the guest dedupes the log per app instance (a counter bumped on each app start) and guest thread (cpu) and prints
each (instance, cpu, block address) once as `SP16_TB i<n> c<cpu> <hex> <hex> ...` lines on the serial console, i.e. in
the session's console.txt, after an `SP16_TB_INSTANCE i<n>` marker. The `SP16_TB_STATS` and `SP16_TB_END` lines carry the
instance, its counts per cpu and a seconds-since-the-instance's-first-log-byte stamp. This tool collects them: distinct
(instance, cpu, address) triples in first-seen order, a range check and the guest's own final counts, checked per
instance and cpu.

    python3 emulator/exectrace.py <session>/console.txt [--out FILE] [--range LO..HI]

`--out` writes one `i<n> c<cpu> 0x%08x` per line in first-execution order (a block of a build without the cpu tag is
written `i<n> 0x%08x`). Older consoles still read: `SP16_TB c<cpu> <hex> ...` (before the instance counter) and
`SP16_TB <hex> ...` (before the per-thread trace) are instance 0, the latter with no cpu. The summary is one JSON
document on stdout; the exit status is 1 when the console holds no address. Standard library only.
"""
import argparse
import json
import re
import sys

# The serial console is shared: another writer can cut into a batch line, so a batch may end early or start mid-line.
# Only complete 8-digit tokens count; a cut-off token and what follows it are skipped. The `i<n>` and `c<cpu>` tags are
# optional so a console from a build before the instance counter or the per-thread trace still reads.
BATCH = re.compile(r"SP16_TB (?:i(\d+) )?(?:c(\d+) )?((?:[0-9a-f]{8} )*[0-9a-f]{8})(?![0-9a-f])")
END = re.compile(r"SP16_TB_END (?:i(\d+) )?distinct=(\d+) lines=(\d+)(?: t=([0-9.]+))?(?: cpus=(\S+))?")
CPU_COUNT = re.compile(r"c(\d+):(\d+)@([0-9.]+)")
DEFAULT_RANGE = (0x116f8, 0x831ca8)  # the 1.60 application's text range, as in guest-init.sh


def blocks(text):
    """Distinct (instance, cpu, block address) triples of a console log, in first-seen order. A damaged token is
    skipped. The cpu is None for a block printed by a build without the per-thread trace; a build without the instance
    counter is instance 0."""
    seen = {}
    for match in BATCH.finditer(text):
        instance = int(match.group(1)) if match.group(1) is not None else 0
        cpu = int(match.group(2)) if match.group(2) is not None else None
        for token in match.group(3).split():
            seen.setdefault((instance, cpu, int(token, 16)), None)
    return list(seen)


def addresses(text):
    """Distinct block addresses across every instance and cpu, in first-seen order."""
    return list(dict.fromkeys(pc for _, _, pc in blocks(text)))


def per_thread_counts(found):
    """{(instance, cpu): distinct blocks}."""
    counts = {}
    for instance, cpu, _ in found:
        counts[(instance, cpu)] = counts.get((instance, cpu), 0) + 1
    return counts


def thread_name(instance, cpu):
    return "i%d c%d" % (instance, cpu) if cpu is not None else "i%d" % instance


def thread_order(item):
    return (item[0], item[1] is None, item[1] if item[1] is not None else 0)


def guest_ends(text):
    """{instance: (distinct, seconds, {cpu: (distinct, first-seen seconds)} or None)} from the last SP16_TB_END line of
    each instance. The cpus are None when that END line carries no per-cpu counts."""
    ends = {}
    for instance, distinct, _, seconds, cpus in END.findall(text):
        ends[int(instance) if instance else 0] = (
            int(distinct), float(seconds) if seconds else None,
            {int(c): (int(n), float(t)) for c, n, t in CPU_COUNT.findall(cpus)} if cpus else None)
    return ends


def summarize(found, text, bounds=DEFAULT_RANGE):
    ends = guest_ends(text)
    counts = per_thread_counts(found)
    pcs = [pc for _, _, pc in found]
    last_end = ends[max(ends)] if ends else None
    summary = {"distinct": len(found), "distinct_addresses": len(set(pcs)),
               "instances": {"i%d" % i: n for i, n in sorted(
                   {i: sum(1 for t in found if t[0] == i) for i in {t[0] for t in found}}.items())},
               "first": ["%s0x%08x" % (thread_name(i, c) + " ", a) for i, c, a in found[:5]],
               "range": ["0x%x" % bounds[0], "0x%x" % bounds[1]],
               "outside_range": sum(1 for a in pcs if not bounds[0] <= a <= bounds[1]),
               "lowest": "0x%08x" % min(pcs) if pcs else None, "highest": "0x%08x" % max(pcs) if pcs else None,
               "guest_reported_distinct": sum(end[0] for end in ends.values()) if ends else None,
               "guest_reported_seconds": last_end[1] if last_end else None,
               "cpus": {thread_name(i, c): n for (i, c), n in sorted(counts.items(), key=lambda item: thread_order(item[0]))}}
    guest = {(i, c): v for i, end in ends.items() if end[2] is not None for c, v in end[2].items()}
    if guest:
        summary["guest_reported_cpus"] = {thread_name(i, c): guest[(i, c)][0] for i, c in sorted(guest)}
        summary["cpu_first_seen_seconds"] = {thread_name(i, c): guest[(i, c)][1] for i, c in sorted(guest)}
        # Per instance and cpu the host must hold what the guest counted, for each instance whose END line carries
        # counts; an instance without one (still running, or its END line cut off) is not checked.
        checked = {i for i, end in ends.items() if end[2] is not None}
        keys = {k for k in guest} | {k for k in counts if k[0] in checked and k[1] is not None}
        summary["cpu_count_mismatch"] = [thread_name(i, c) for i, c in sorted(keys)
                                         if guest.get((i, c), (None,))[0] != counts.get((i, c), 0)]
    return summary


def line_for(instance, cpu, pc):
    return "i%d %s0x%08x\n" % (instance, "c%d " % cpu if cpu is not None else "", pc)


def parse_range(text):
    low, high = (int(part, 16) for part in text.split(".."))
    return low, high


def main(argv, stdout=None):
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("console", help="the session's console.txt")
    parser.add_argument("--out", help="write the distinct (instance, cpu, address) triples here, one "
                                      "`i<n> c<cpu> 0x%%08x` per line")
    parser.add_argument("--range", type=parse_range, default=DEFAULT_RANGE, metavar="LO..HI",
                        help="hex range the addresses are expected in (default 0x116f8..0x831ca8)")
    args = parser.parse_args(argv)
    with open(args.console, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    found = blocks(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("".join(line_for(instance, cpu, pc) for instance, cpu, pc in found))
    stdout.write(json.dumps(summarize(found, text, args.range), indent=2) + "\n")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
