#!/usr/bin/env python3
"""Drive a research-enabled native SP-16 session through its owner-only control socket.

The socket exists only for sessions built with `native.py build --research`; its path
is in the session's runtime.json (`control_socket`). Events go through the same host
transport as the panel view, so results are correlated and counted identically.
"""
import argparse
import json
from pathlib import Path
import socket
import sys
import time

from panel_map import CONTROL_IDS

ACTIONS = {"release": 0, "press": 1, "delta": 2, "value": 3}


def adc_words(metrics):
    """Extract the guest's reported analog RX words from a metrics reply."""
    words = (metrics.get("guest") or {}).get("adc")
    if not isinstance(words, dict):
        raise RuntimeError("guest has not reported ADC words yet (guest build predates the adc stat)")
    return words


class Session:
    def __init__(self, runtime_json):
        runtime = json.loads(Path(runtime_json).read_text())
        if not runtime.get("control_socket"):
            raise ValueError("session was not built with --research")
        self.dir = Path(runtime["session"])
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(10)
        self.socket.connect(runtime["control_socket"])
        self.reader = self.socket.makefile("r")

    def call(self, **command):
        self.socket.sendall((json.dumps(command) + "\n").encode())
        reply = json.loads(self.reader.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply

    @staticmethod
    def control(name):
        return CONTROL_IDS[name] if name in CONTROL_IDS else int(name)

    def event(self, name, action, value=0):
        return self.call(op="event", id=self.control(name), action=ACTIONS[action], value=value)

    def press(self, name, seconds=0.15, value=100):
        self.event(name, "press", value)
        time.sleep(seconds)
        self.event(name, "release", 0)

    def turn(self, name, detents, gap=0.03):
        step = 1 if detents > 0 else -1
        for _ in range(abs(detents)):
            self.event(name, "delta", step)
            time.sleep(gap)

    def touch(self, x, y, action):
        return self.call(op="touch", x=x, y=y, action=action)

    def raw(self, offset, data):
        return self.call(op="raw", offset=offset, hex=bytes(data).hex())

    def period(self, ms):
        return self.call(op="period", ms=ms)

    def clear(self):
        return self.call(op="clear")

    def snapshot(self, name):
        self.call(op="snapshot", name=name)
        return self.dir / name

    def resize(self, width, height):
        return self.call(op="resize", width=width, height=height)

    def panelshot(self, name):
        self.call(op="panelshot", name=name)
        return self.dir / name

    def usb(self, action):
        """Virtual USB stick: "eject" (guest flushes and unmounts, then the app sees it removed; replies usb-busy
        and leaves the stick mounted if something still holds it) or "attach" (remount and announce again)."""
        return self.call(op="usb", action=action)

    def metrics(self):
        return self.call(op="metrics")

    def app_restart(self):
        """Decoupled builds: restart only the OEM app (the developer overlay is re-applied first)."""
        return self.call(op="app-restart")

    def firmware(self):
        """Decoupled builds: running firmware, library list and the current manifest summary."""
        return self.call(op="firmware")

    def adc(self):
        """Analog RX words (0x26 drive, 0x28 main_out, 0x2a hp_cutoff) the guest last reported.
        The guest refreshes this about once per second, so wait ~1.2 s after an event."""
        return adc_words(self.metrics())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="session runtime.json")
    parser.add_argument("command", choices=["event", "press", "turn", "touch", "raw", "clear",
                                            "snapshot", "metrics", "txlog", "period", "panelshot", "resize", "adc", "usb",
                                            "app-restart", "firmware"])
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    session = Session(args.runtime)
    a = args.args
    if args.command == "event":
        result = session.event(a[0], a[1], int(a[2]) if len(a) > 2 else 0)
    elif args.command == "press":
        session.press(a[0], float(a[1]) if len(a) > 1 else .15, int(a[2]) if len(a) > 2 else 100)
        result = {"ok": True}
    elif args.command == "turn":
        session.turn(a[0], int(a[1]))
        result = {"ok": True}
    elif args.command == "touch":
        result = session.touch(int(a[0]), int(a[1]), ["move", "down", "up"].index(a[2]))
    elif args.command == "raw":
        result = session.raw(int(a[0], 0), bytes.fromhex(a[1]))
    elif args.command == "clear":
        result = session.clear()
    elif args.command == "txlog":
        result = session.call(op="txlog", on=0 if a and a[0] == "off" else 1)
    elif args.command == "period":
        result = session.period(int(a[0]))
    elif args.command == "resize":
        result = session.resize(int(a[0]), int(a[1]))
    elif args.command == "panelshot":
        result = {"path": str(session.panelshot(a[0]))}
    elif args.command == "usb":
        result = session.usb(a[0] if a else "eject")
    elif args.command == "adc":
        result = session.adc()
    elif args.command == "app-restart":
        result = session.app_restart()
    elif args.command == "firmware":
        result = session.firmware()
    elif args.command == "snapshot":
        result = {"path": str(session.snapshot(a[0]))}
    else:
        result = session.metrics()
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
