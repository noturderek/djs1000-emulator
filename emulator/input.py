#!/usr/bin/env python3
"""Send virtual touchscreen events to the isolated SP-16 Xvfb display."""

import argparse
import ctypes
import json
import sys
import time


pressed_at = None


def navigation_key(name):
    if name not in ("Up", "Down", "Left", "Right", "Return", "Escape"):
        raise ValueError("unsupported navigation key")
    x11 = ctypes.CDLL("libX11.so.6")
    xtest = ctypes.CDLL("libXtst.so.6")
    display_pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = display_pointer
    x11.XCloseDisplay.argtypes = [display_pointer]
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes = [display_pointer, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XSync.argtypes = [display_pointer, ctypes.c_int]
    xtest.XTestFakeKeyEvent.argtypes = [display_pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(b"127.0.0.1:99")
    if not display:
        raise RuntimeError("the isolated SP-16 display is unavailable")
    try:
        code = x11.XKeysymToKeycode(display, x11.XStringToKeysym(name.encode("ascii")))
        if not code or not xtest.XTestFakeKeyEvent(display, code, 1, 0):
            raise RuntimeError("navigation key press failed")
        if not xtest.XTestFakeKeyEvent(display, code, 0, 20):
            raise RuntimeError("navigation key release failed")
        x11.XSync(display, 0)
    finally:
        x11.XCloseDisplay(display)


def touch(x, y, action="click"):
    global pressed_at
    if action not in ("click", "down", "up", "move", "wheel_up", "wheel_down"):
        raise ValueError("unsupported touch action")
    x11 = ctypes.CDLL("libX11.so.6")
    xtest = ctypes.CDLL("libXtst.so.6")
    display_pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = display_pointer
    x11.XCloseDisplay.argtypes = [display_pointer]
    x11.XDisplayWidth.argtypes = [display_pointer, ctypes.c_int]
    x11.XDisplayHeight.argtypes = [display_pointer, ctypes.c_int]
    x11.XSync.argtypes = [display_pointer, ctypes.c_int]
    xtest.XTestQueryExtension.argtypes = [display_pointer] + [ctypes.POINTER(ctypes.c_int)] * 4
    xtest.XTestFakeMotionEvent.argtypes = [display_pointer, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    xtest.XTestFakeButtonEvent.argtypes = [display_pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(b"127.0.0.1:99")
    if not display:
        raise RuntimeError("the isolated SP-16 display is unavailable")
    try:
        width, height = x11.XDisplayWidth(display, 0), x11.XDisplayHeight(display, 0)
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("click lies outside the display")
        details = [ctypes.c_int() for _ in range(4)]
        if not xtest.XTestQueryExtension(display, *(ctypes.byref(v) for v in details)):
            raise RuntimeError("XTEST input synthesis is unavailable")
        if not xtest.XTestFakeMotionEvent(display, 0, x, y, 0):
            raise RuntimeError("virtual pointer motion failed")
        if action in ("wheel_up", "wheel_down"):
            button = 4 if action == "wheel_up" else 5
            if (not xtest.XTestFakeButtonEvent(display, button, 1, 0)
                    or not xtest.XTestFakeButtonEvent(display, button, 0, 0)):
                raise RuntimeError("virtual scroll failed")
        if action in ("click", "down") and not xtest.XTestFakeButtonEvent(display, 1, 1, 0):
            raise RuntimeError("virtual touch press failed")
        if action == "down":
            pressed_at = time.monotonic()
        if action == "up" and pressed_at is not None:
            # Preserve a short tap even when the persistent bridge delivers
            # press/release faster than the OEM touchscreen polling interval.
            time.sleep(max(0, .05 - (time.monotonic() - pressed_at)))
        if action in ("click", "up") and not xtest.XTestFakeButtonEvent(display, 1, 0, 50 if action == "click" else 0):
            raise RuntimeError("virtual touch release failed")
        if action in ("click", "up"):
            pressed_at = None
        x11.XSync(display, 0)
        return {"status": "event_sent", "action": action, "x": x, "y": y, "width": width, "height": height}
    finally:
        x11.XCloseDisplay(display)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=int, nargs="?")
    parser.add_argument("y", type=int, nargs="?")
    parser.add_argument("--action", choices=("click", "down", "up", "move", "wheel_up", "wheel_down"), default="click")
    parser.add_argument("--server", action="store_true", help="read validated events over a persistent input pipe")
    args = parser.parse_args()
    if args.server:
        try:
            for line in sys.stdin:
                try:
                    event = json.loads(line)
                    if (not isinstance(event, dict) or set(event) != {"x", "y", "action"}
                            or type(event["x"]) is not int or type(event["y"]) is not int):
                        raise ValueError("invalid touch event")
                    result = touch(event["x"], event["y"], event["action"])
                except (OSError, ValueError, RuntimeError) as exc:
                    result = {"error": str(exc)}
                print(json.dumps(result), flush=True)
        finally:
            if pressed_at is not None:
                touch(0, 0, "up")
        return
    if args.x is None or args.y is None:
        parser.error("coordinates are required unless --server is used")
    try:
        result = touch(args.x, args.y, args.action)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"virtual input failed: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
