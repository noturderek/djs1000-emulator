#!/usr/bin/env python3
"""Stop this emulator's QEMU when the app that started it goes away (quit, crash or kill -9).

The app starts this helper right after QEMU, as a second child of the app. When the app dies the kernel re-parents
the helper (to launchd), and that change is the signal. For a persistent build the helper first asks the guest for a
clean stop on the control socket (guest sync and unmount), then ends QEMU in steps: a wait for its own exit, SIGTERM,
then SIGKILL. It signals only the QEMU PID it was given, and only while that PID's command line still contains the
session's socket path, so a recycled PID is left alone. It exits as soon as QEMU has gone, so it never outlives the
emulator. Standard library only.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time

CLEAN_STOP_TIMEOUT = 4.0   # seconds for the guest to answer 'shutdown' with 'done'
EXIT_WAIT = 3.0            # after the clean stop, and after SIGTERM, before the next step
KILL_WAIT = 2.0            # after SIGKILL
POLL = 0.25


def command_of(pid):
    """The full command line of a live process, or '' when there is none."""
    result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return result.stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_our_qemu(pid, marker):
    """True only while the PID runs a command that names this session (its socket path)."""
    return alive(pid) and marker in command_of(pid)


def wait_for_exit(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and alive(pid):
        time.sleep(POLL)
    return not alive(pid)


def clean_stop(path, timeout=CLEAN_STOP_TIMEOUT):
    """Ask the guest to stop, as a quit does. True only when the guest answers 'done'."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            client.sendall(b"shutdown\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = client.recv(64)
                if not chunk:
                    break
                reply += chunk
            return reply.strip() == b"done"
    except OSError:
        return False


def end_qemu(qemu, marker, control=None):
    """The steps that end QEMU after its app is gone. Every signal is preceded by the identity check."""
    if control and os.path.exists(control):
        clean_stop(control)
    if wait_for_exit(qemu, EXIT_WAIT):
        return "exited"
    for sig, wait in ((signal.SIGTERM, EXIT_WAIT), (signal.SIGKILL, KILL_WAIT)):
        if not is_our_qemu(qemu, marker):
            return "gone" if not alive(qemu) else "not-ours"
        os.kill(qemu, sig)
        if wait_for_exit(qemu, wait):
            return "signal-" + ("term" if sig == signal.SIGTERM else "kill")
    return "survived"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=int, required=True, help="PID of the QEMU child to stop")
    parser.add_argument("--match", required=True,
                        help="text that the QEMU command line must contain before any signal (the session socket)")
    parser.add_argument("--control", help="guest control socket for a clean stop (persistent builds)")
    args = parser.parse_args(argv)
    owner = os.getppid()
    while True:
        if owner == 1 or os.getppid() != owner:
            break  # the app has gone and this helper was re-parented
        if not alive(args.qemu):
            return 0  # QEMU ended by itself or by a clean quit
        time.sleep(POLL)
    print(f"qemu_reaper app_gone qemu={args.qemu}", flush=True)
    outcome = end_qemu(args.qemu, args.match, args.control)
    print(f"qemu_reaper outcome={outcome}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
