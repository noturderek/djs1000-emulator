#!/usr/bin/env python3
"""Write a small synthetic test kit: HW-KICK, HW-SNARE, HW-HAT and HW-TONE440 (16-bit 44.1 kHz mono WAV).

The sounds are generated here, so the repository ships no audio and nobody's samples. They are the kit the DJS-1000
research used (docs/djs-1000.md): the 440 Hz tone for pitch and timing, the kick and hat as onset markers.

    python3 emulator/make_test_kit.py build/test-kit && ./sp16 usb add build/test-kit/*.wav
"""
import math
from pathlib import Path
import random
import struct
import sys
import wave

RATE = 44100


def write(path, samples):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 30000)) for s in samples))


def kit():
    """{file name: samples}; the same output on every run (fixed seed)."""
    rng = random.Random(1)
    kick, phase = [], 0.0
    for i in range(int(RATE * 0.5)):
        t = i / RATE
        phase += 2 * math.pi * (50 + 120 * math.exp(-t * 30)) / RATE
        kick.append(math.sin(phase) * math.exp(-t * 7))
    snare = [(rng.uniform(-1, 1) * 0.7 + 0.4 * math.sin(2 * math.pi * 190 * i / RATE)) * math.exp(-i / RATE * 18)
             for i in range(int(RATE * 0.35))]
    hat, high, previous = [], 0.0, 0.0
    for i in range(int(RATE * 0.12)):
        noise = rng.uniform(-1, 1)
        high = 0.9 * (high + noise - previous); previous = noise
        hat.append(high * math.exp(-i / RATE * 40))
    tone = [0.6 * math.sin(2 * math.pi * 440 * i / RATE) * min(1, (RATE - i) / 2000) for i in range(RATE)]
    return {"HW-KICK.wav": kick, "HW-SNARE.wav": snare, "HW-HAT.wav": hat, "HW-TONE440.wav": tone}


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: make_test_kit.py OUTPUT_FOLDER")
    folder = Path(argv[1]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    for name, samples in kit().items():
        write(folder / name, samples)
        print(folder / name)


if __name__ == "__main__":
    main(sys.argv)
