#!/usr/bin/env python3
"""Read-only OEM LCD title oracle after a GUI or socket action."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from sessionctl import Session


def title_from_ocr(output):
    return next((re.sub(r"\s+", " ", line.strip()).upper()
                 for line in output.splitlines() if line.strip()), "")


def inspect(runtime_path, expected, attempts=4):
    started = time.monotonic()
    expected = title_from_ocr(expected)
    if not expected:
        raise ValueError("expected title must not be empty")
    if attempts < 2:
        raise ValueError("at least two snapshots are required")
    runtime = json.loads(runtime_path.read_text())
    session_dir = Path(runtime["session"])
    if session_dir.resolve() != runtime_path.parent.resolve():
        raise ValueError("runtime.json session path does not match its directory")
    if not shutil.which("tesseract"):
        return {"status": "BLOCKED", "expected": expected,
                "observed": "tesseract executable unavailable", "elapsed_ms": 0}
    session = Session(runtime_path)
    captures = []
    titles = []
    consecutive = 0
    passed = False
    error = None
    try:
        for number in range(attempts):
            name = f"e2e-screen-{uuid4().hex}-{number}.png"
            path = session.snapshot(name)
            captures.append(path)
            ocr = subprocess.run(["tesseract", str(path), "stdout"], text=True,
                                 capture_output=True, check=True)
            title = title_from_ocr(ocr.stdout)
            titles.append(title)
            consecutive = consecutive + 1 if title == expected else 0
            if consecutive >= 2:
                passed = True
                break
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        error = str(exc)
    finally:
        session.close()
        for path in captures if passed else captures[:-1]:
            path.unlink(missing_ok=True)
    return {
        "status": "PASS" if passed else "FAIL", "expected": expected,
        "observed": {"titles": titles, "error": error,
                     "failure_capture": str(captures[-1]) if captures and not passed else None},
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--expect", required=True,
                        help="exact first OCR title line, e.g. HOME or HOME > UTILITY")
    parser.add_argument("--id", default="lcd.title", help="narrow E2E subcase ID")
    args = parser.parse_args()
    try:
        outcome = inspect(args.runtime, args.expect)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        outcome = {"status": "FAIL", "expected": args.expect.upper(), "observed": str(exc),
                   "elapsed_ms": 0}
    result = {"id": args.id, **outcome}
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
