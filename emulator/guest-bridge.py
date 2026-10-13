#!/usr/bin/env python3
"""Stream real pixels, touch and semantic panel controls over a dedicated virtio port."""
import importlib.util
import os
import re
import select
from pathlib import Path
import socket
import struct
import threading
import time

import panel_map
from protocol import (ACK, CONTROL, FRAME, INPUT_FAILED, INPUT_INVALID, INPUT_UNSUPPORTED, KEY,
                      RESEARCH, TOUCH, TX_FRAME, WHEEL, decode_control, decode_key,
                      decode_research, decode_touch, decode_wheel, encode_feedback,
                      encode_result, encode_stats, frame_pixels, message, receive)
from panel import PanelModel, crc16

spec = importlib.util.spec_from_file_location("sp16_input", "/opt/sp16/input.py")
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)

ROOT = "/tmp/sp16-root"
PERIOD = float(os.environ.get("SP16_PANEL_PERIOD_MS", "10")) / 1000
timing = {"period": PERIOD}
HEARTBEAT = 0.1
# The host connects to the UI port once and never reconnects. When it goes away (quit, crash, kill -9) the bridge
# waits this long for a hang-up to clear, then exits; guest-init then runs the clean stop (OEM killed, data synced
# and unmounted) and powers off, so QEMU does not outlive the app and keep the data image locked.
HOST_GRACE = 8.0


class Counters:
    """Small thread-safe counters reported to the host for event-loss/queue checks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = {"rx_frames": 0, "rx_drops": 0, "cdc_frames": 0, "cdc_drops": 0,
                       "tx_frames": 0, "tx_bad_crc": 0, "events": 0, "unsupported": 0,
                       "failed": 0}
        # Requests the shim refused with ENOTTY (it still fails them loudly); reported so that a firmware version
        # using driver requests the shim does not know is diagnosable from metrics (docs/architecture.md, M5).
        self.unknown_ioctls = {}
        # Device nodes the shim does not serve (hardware suite HW-45): path -> opens reported.
        self.unknown_devices = {}
        # Requests the shim accepted, by device kind (hardware suite HW-45: every declared request was served).
        self.ioctls_seen = {}

    def add(self, name, amount=1):
        with self.lock:
            self.values[name] += amount

    def unknown_ioctl(self, request):
        with self.lock:
            if request in self.unknown_ioctls or len(self.unknown_ioctls) < 64:
                self.unknown_ioctls[request] = self.unknown_ioctls.get(request, 0) + 1

    def unknown_device(self, path):
        with self.lock:
            if path in self.unknown_devices or len(self.unknown_devices) < 64:
                self.unknown_devices[path] = self.unknown_devices.get(path, 0) + 1

    def ioctl_seen(self, request, kind):
        with self.lock:
            if request in self.ioctls_seen or len(self.ioctls_seen) < 64:
                self.ioctls_seen[request] = kind

    def snapshot(self):
        with self.lock:
            return {**self.values, "unknown_ioctls": dict(self.unknown_ioctls),
                    "unknown_devices": dict(self.unknown_devices), "ioctls_seen": dict(self.ioctls_seen)}


UNKNOWN_IOCTL = re.compile(r"\[sp16-ui-stub\] unsupported ioctl fd=\d+ request=(0x[0-9a-f]{1,8})$")
UNKNOWN_DEVICE = re.compile(r"\[sp16-ui-stub\] unknown device open path=(/[!-~]{1,63})$")
IOCTL_SEEN = re.compile(r"\[sp16-ui-stub\] ioctl accepted request=(0x[0-9a-f]{1,8}) device=([a-z]{1,16})$")


def main():
    # Device supplied only by this VM's QEMU arguments; no TCP listener.
    path = Path("/dev/vport0p1")
    for _ in range(100):
        if path.exists():
            break
        time.sleep(.1)
    descriptor = os.open(path, os.O_RDWR)
    stream = os.fdopen(os.dup(descriptor), "rb", buffering=0)
    lock = threading.Lock()
    stopped = threading.Event()
    model = PanelModel(pad_curve=panel_map.PAD_CURVE)
    counters = Counters()
    state = {"txlog": False, "generation": 0, "lamps": {}, "live": False}
    logged_lines = 0

    def diagnostics():
        nonlocal logged_lines
        # Export only our own messages (shim device lines, fwdev preload libraries), never arbitrary OEM logs.
        lines = Path("/output/application.txt").read_text(errors="replace").splitlines()
        for line in lines[logged_lines:]:
            if line.startswith(("[sp16-ui-stub]", "[sp16-mod]")):  # shim and developer preload libraries
                print(line, flush=True)
                unknown = UNKNOWN_IOCTL.match(line)
                if unknown:
                    counters.unknown_ioctl(unknown.group(1))
                seen = IOCTL_SEEN.match(line)
                if seen:
                    counters.ioctl_seen(seen.group(1), seen.group(2))
                device = UNKNOWN_DEVICE.match(line)
                if device:
                    counters.unknown_device(device.group(1))
        logged_lines = len(lines)

    def send(kind, payload):
        send_raw(message(kind, payload))

    def send_raw(prepared):
        with lock:
            data = memoryview(prepared)
            while data:
                count = os.write(descriptor, data)
                if not count:
                    raise EOFError("native window disconnected")
                data = data[count:]

    def datagram(sock, target, payload, sent, dropped):
        try:
            sock.sendto(payload, target)
            counters.add(sent)
            return True
        except OSError:
            # No reader yet or full queue: state frames are absolute, the next one repairs it.
            counters.add(dropped)
            return False

    def frames():
        previous, sequence, last_diagnostics = None, 0, 0
        try:
            while not stopped.is_set():
                source = Path("/output/Xvfb_screen0").read_bytes()
                pixels = frame_pixels(source)
                if pixels != previous:
                    sequence += 1
                    send(FRAME, struct.pack(">HHI", 800, 480, sequence) + pixels)
                    previous = pixels
                if time.monotonic() - last_diagnostics >= 1:
                    diagnostics()
                    send_raw(encode_stats({**counters.snapshot(), "adc": model.adc_words(), "rx": model.rx_state()}))
                    last_diagnostics = time.monotonic()
                time.sleep(.02)
        except (OSError, EOFError, ValueError) as exc:
            print(f"SP16_FRAME_ERROR {exc}", flush=True)
            stopped.set()

    def streamer():
        """Deliver a complete RX frame every period while input is active, else a heartbeat.

        The OEM held-button counters advance once per received frame, so held controls
        need a steady frame stream; the nominal SPI interval is 3 ms but the real jiffy
        quantization is unknown, hence a configurable period (default 10 ms).
        """
        out = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
        cdc = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
        panel_target, cdc_target = f"{ROOT}/dev/sp16-panel-input", f"{ROOT}/dev/sp16-cdc-input"
        last, baseline = 0.0, False
        try:
            while not stopped.wait(timing["period"]):
                due = model.step()
                now = time.monotonic()
                if not baseline or due or model.active() or now - last >= HEARTBEAT:
                    datagram(out, panel_target, model.frame(), "rx_frames", "rx_drops")
                    baseline, last = True, now
                if model.cdc_changed:
                    # CDC is signalled only on change, so a dropped datagram must be retried.
                    if not datagram(cdc, cdc_target, model.cdc_bytes(), "cdc_frames", "cdc_drops"):
                        model.cdc_changed = True
        finally:
            out.close()
            cdc.close()

    def listen_tx():
        """Capture OEM panel TX frames (lamp state source); decode only proven fields."""
        tx_path = f"{ROOT}/dev/sp16-panel-tx"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.settimeout(.02)
        try:
            os.unlink(tx_path)
        except FileNotFoundError:
            pass
        listener.bind(tx_path)
        last_sent, last_frame, unsent = 0.0, None, False

        def flush():
            nonlocal last_sent, unsent
            send_raw(encode_feedback(state["generation"], True, state["lamps"]))
            last_sent, unsent = time.monotonic(), False

        try:
            while not stopped.is_set():
                try:
                    datagram_bytes = listener.recv(256)
                except socket.timeout:
                    datagram_bytes = None
                # A change that was throttled is flushed even when no new frame follows.
                if unsent and time.monotonic() - last_sent >= 1 / 60:
                    flush()
                if datagram_bytes is None or len(datagram_bytes) < 8:
                    continue
                try:
                    multi, part, parts, _, length = struct.unpack_from("<BBBBI", datagram_bytes)
                    frame = datagram_bytes[8:8 + min(length, 128)]
                    counters.add("tx_frames")
                    if len(frame) == 128 and crc16(frame[:124]) != struct.unpack_from("<H", frame, 124)[0]:
                        counters.add("tx_bad_crc")
                        continue
                    if state["txlog"]:
                        send(TX_FRAME, bytes((multi, part, parts)) + frame)
                    if frame == last_frame:
                        continue  # the OEM resends unchanged lamp state continuously
                    last_frame = frame
                    lamps = panel_map.decode_lamps(frame) if len(frame) == 128 else None
                    if lamps is not None:
                        merged = {**state["lamps"], **lamps}
                        if merged != state["lamps"] or not state["live"]:
                            state["lamps"], state["live"] = merged, True
                            state["generation"] += 1
                            unsent = True
                            if time.monotonic() - last_sent >= 1 / 60:
                                flush()
                except (struct.error, ValueError):
                    counters.add("failed")
        finally:
            listener.close()

    def host_gone(reason):
        """Exit after the grace period unless the host returns; guest-init then runs the clean stop and poweroff."""
        print(f"SP16_HOST_LOST reason={reason} grace={HOST_GRACE}", flush=True)
        time.sleep(HOST_GRACE)
        if poller.poll(0):
            print("SP16_HOST_LOST exit=yes", flush=True)
            os._exit(0)
        print("SP16_HOST_LOST host_returned=yes", flush=True)
        return False

    poller = select.poll()
    poller.register(descriptor, select.POLLERR | select.POLLHUP)

    def host_watch():
        """poll(2) reports POLLERR/POLLHUP on the port while the host end of the chardev is closed."""
        while True:
            if poller.poll(200):
                host_gone("hangup")

    threading.Thread(target=frames, daemon=True).start()
    threading.Thread(target=host_watch, daemon=True).start()
    threading.Thread(target=streamer, daemon=True).start()
    threading.Thread(target=listen_tx, daemon=True).start()
    sequence = 0
    try:
        while not stopped.is_set():
            try:
                kind, payload = receive(stream)
            except (EOFError, OSError):
                host_gone("eof")
                continue
            try:
                if kind == CONTROL:
                    try:
                        control, action, value, event = decode_control(payload)
                    except ValueError:
                        counters.add("failed")
                        send_raw(encode_result(0, INPUT_INVALID))
                        continue
                    status = model.apply(control, action, value)
                    counters.add("events")
                    if status == INPUT_UNSUPPORTED:
                        counters.add("unsupported")
                    send_raw(encode_result(event, status))
                    continue
                if kind == RESEARCH:
                    op, offset, data = decode_research(payload)
                    if op == "raw":
                        model.set_raw(offset, data)
                    elif op == "cdc":
                        model.set_cdc_raw(data)
                    elif op == "clear":
                        model.clear_overrides()
                    elif op == "period":
                        timing["period"] = offset / 1000
                    else:
                        state["txlog"] = bool(offset)
                    continue
                if kind == TOUCH:
                    x, y, action = decode_touch(payload)
                    controls.touch(x, y, action)
                elif kind == WHEEL:
                    x, y, steps = decode_wheel(payload)
                    model.apply(panel_map.CONTROL_IDS["selector"], "delta", steps)
                elif kind == KEY:
                    key = decode_key(payload)
                    selector = panel_map.CONTROL_IDS["selector"]
                    if key in ("Up", "Down"):
                        model.apply(selector, "delta", 1 if key == "Down" else -1)
                    elif key == "Return":
                        model.apply(selector, "press")
                        time.sleep(.05)
                        model.apply(selector, "release")
                    else:
                        controls.navigation_key(key)
                else:
                    raise ValueError("unsupported host message")
            except OSError as exc:
                counters.add("failed")
                print(f"SP16_INPUT_ERROR errno={exc.errno}", flush=True)
                if kind == CONTROL:
                    send_raw(encode_result(event, INPUT_FAILED))
                    continue
            except ValueError:
                # Malformed legacy/research messages are rejected without stopping the bridge.
                counters.add("failed")
            sequence += 1
            send(ACK, struct.pack(">I", sequence))
    finally:
        # Disconnect releases every hold before the guest stops reading input.
        model.release_all()
        time.sleep(timing["period"] * 3)
        stopped.set()
        stream.close()
        os.close(descriptor)


if __name__ == "__main__":
    main()
