#!/usr/bin/env python3
"""Decode and analyse research PCM tap captures (native.py build --research --trace pcmtap).

The guest prints each finished capture to the session console as SP16_PCM_DUMP gzip+base64 lines. This tool
extracts them into <out>/sp16-pcm-<stream>.raw (64-byte header + interleaved frames, see hardware_stub.c) and
prints per stream and per channel peak, RMS, non-zero span count and format sanity. Nothing is played.

  pcm-tap-analyze.py --console sessions/native-X/console.txt --out DIR [--onsets]
  pcm-tap-analyze.py --raw DIR/sp16-pcm-ak4458audio-0.raw
"""
import argparse
import base64
import math
import re
import struct
import sys
import zlib
from pathlib import Path

HEADER = 64
FORMATS = {2: ("<h", 2, 32768.0, "S16_LE"), 14: ("<f", 4, 1.0, "FLOAT_LE"), 10: ("<i", 4, 2147483648.0, "S32_LE"),
           6: ("<i", 4, 8388608.0, "S24_LE")}


def extract(console, out):
    """Return {stream: raw bytes} from SP16_PCM_DUMP lines; the last complete dump of a stream wins."""
    pieces, results = {}, {}
    for line in Path(console).read_text(errors="replace").splitlines():
        match = re.match(r"SP16_PCM_DUMP (\S+) (begin|end|\d+)(?: (.*))?$", line.strip())
        if not match:
            continue
        stream, tag, rest = match.groups()
        if tag == "begin":
            pieces[stream] = {}
        elif tag == "end":
            parts = pieces.pop(stream, {})
            if parts and sorted(parts) == list(range(len(parts))):
                try:
                    blob = base64.b64decode("".join(parts[i] for i in sorted(parts)))
                    results[stream] = blob[:HEADER] + zlib.decompress(blob[HEADER:])
                except (ValueError, zlib.error) as error:
                    print(f"skipped corrupt dump {stream}: {error}", file=sys.stderr)
        elif stream in pieces and rest:
            pieces[stream][int(tag)] = rest
    for stream, raw in results.items():
        (out / f"sp16-pcm-{stream}.raw").write_bytes(raw)
    return results


def parse(raw):
    if raw[:8] != b"SP16PCM1":
        raise ValueError("not a PCM tap capture")
    done, channels, fmt, rate, bpf = struct.unpack_from("<IIIII", raw, 12)
    cap, captured, first, start_ns = struct.unpack_from("<QQQQ", raw, 32)
    return dict(done=done, channels=channels, format=fmt, rate=rate, bpf=bpf, cap=cap, captured=captured,
                first=first, start_ns=start_ns)


def decode(code, label, data, count):
    """Interleaved samples as signed numbers; S24_LE keeps 24 bits in the low bytes of a 32-bit word and is sign-extended."""
    samples = struct.unpack(f"<{count}{code[1]}", data)
    if label == "S24_LE":
        samples = tuple(((v & 0xFFFFFF) ^ 0x800000) - 0x800000 for v in samples)
    return samples


def analyse(raw, onsets=False):
    h = parse(raw)
    spec = FORMATS.get(h["format"])
    if not spec:
        raise ValueError(f"unsupported ALSA format {h['format']}")
    code, width, scale, label = spec
    frames, ch = h["captured"], h["channels"]
    samples = decode(code, label, raw[HEADER:HEADER + frames * ch * width], frames * ch)
    report = dict(header=h, label=label, channels=[])
    bad = 0
    for c in range(ch):
        column = samples[c::ch]
        if label == "FLOAT_LE":
            finite = [v for v in column if math.isfinite(v)]
            bad += len(column) - len(finite)
            column = finite
        peak = max((abs(v) for v in column), default=0) / scale
        rms = math.sqrt(sum(v * v for v in column) / max(len(column), 1)) / scale
        nonzero = sum(1 for v in column if v)
        spans, inside = 0, False
        for v in column:
            if v and not inside:
                spans += 1
            inside = bool(v)
        report["channels"].append(dict(ch=c, peak=peak, rms=rms, nonzero=nonzero, spans=spans))
    report["nonfinite"] = bad
    report["over_range"] = sum(1 for v in samples if abs(v) / scale > 1.0) if label == "FLOAT_LE" else 0
    if onsets:
        # Energy envelope in 5 ms blocks over all channels: onset = block rising above 4x the median-noise floor.
        block = max(1, int(h["rate"] * 0.005))
        env = []
        for i in range(0, frames - block, block):
            e = sum(abs(v) for v in samples[i * ch:(i + block) * ch]) / (block * ch * scale)
            env.append(e)
        floor = sorted(env)[len(env) // 2] if env else 0
        thresh = max(floor * 4, 1e-5)
        marks, was = [], False
        for index, e in enumerate(env):
            if e > thresh and not was:
                marks.append(index * block / h["rate"])
            was = e > thresh
        report["onset_seconds"] = marks
    return report


def show(name, report):
    h = report["header"]
    print(f"== {name}: {report['label']} ch={h['channels']} rate={h['rate']} captured={h['captured']} frames "
          f"({h['captured'] / h['rate']:.3f} s) first_frame_index={h['first']} done={h['done']}")
    print(f"   non-finite={report['nonfinite']} over-range(|x|>1)={report['over_range']}")
    for c in report["channels"]:
        print(f"   ch{c['ch']:02d} peak={c['peak']:.6f} rms={c['rms']:.6f} nonzero={c['nonzero']} spans={c['spans']}")
    if "onset_seconds" in report:
        marks = report["onset_seconds"]
        print("   onsets(s): " + ", ".join(f"{m:.3f}" for m in marks[:40]))
        if len(marks) > 2:
            gaps = [b - a for a, b in zip(marks, marks[1:])]
            print(f"   median interval {sorted(gaps)[len(gaps) // 2]:.4f} s -> {60 / sorted(gaps)[len(gaps) // 2]:.1f} per minute")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", help="session console.txt carrying SP16_PCM_DUMP lines")
    parser.add_argument("--out", help="directory for the decoded .raw files (created owner-only)")
    parser.add_argument("--raw", nargs="*", default=[], help="already decoded capture files")
    parser.add_argument("--onsets", action="store_true")
    args = parser.parse_args()
    raws = {}
    if args.console:
        out = Path(args.out or ".")
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        raws.update(extract(args.console, out))
    for path in args.raw:
        raws[Path(path).name] = Path(path).read_bytes()
    if not raws:
        print("no captures found", file=sys.stderr)
        return 1
    for name, raw in sorted(raws.items()):
        show(name, analyse(raw, args.onsets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
