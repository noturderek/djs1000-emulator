#!/usr/bin/env python3
"""Expose a research build's guest gdbstub on a host TCP port for lldb/gdb.

Research builds made with `--trace gdb` run the OEM app under qemu-user's gdbstub (-g 1234) inside the guest. The
guest has no NIC beyond loopback, so native.py adds a dedicated `sp16.gdb` virtio-serial port and guest-init.sh runs
a forwarder that tunnels the in-guest stub out to the host end of that port (a unix socket `<session>/ui.sock.gdb`).

This host-side bridge is the last hop: it listens on 127.0.0.1:<port> (default 1234) and splices each client to that
unix socket, so a stock host debugger reaches the stub:

    emulator/gdb-bridge.py --build <build-name>      # auto-find the running session socket
    emulator/gdb-bridge.py --socket /tmp/sp16-<id>/ui.sock.gdb

then, in another terminal:

    lldb
    (lldb) gdb-remote 127.0.0.1:1234

The app stays halted at entry until the debugger continues it, so start this bridge and attach before expecting the
firmware UI. Only one client is served at a time (a gdb session is point-to-point); the bridge keeps running so you
can detach and re-attach. Nothing here is specific to a firmware or touches real hardware.
"""
import argparse
import re
import selectors
import socket
import subprocess
import sys
import time


def discover_socket(build=None):
    """Find the `.gdb` unix socket of a running research build by scanning qemu command lines.

    Matches the chardev path native.py emits (`socket,id=sp16gdb,path=<...>.gdb,...`). With --build, also require the
    session/socket path to contain that build's sessions directory so multiple running builds are told apart."""
    try:
        listing = subprocess.run(["ps", "-ww", "-eo", "command"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"could not list processes to find the session socket: {error}")
    found = []
    for line in listing.splitlines():
        if "qemu-system-aarch64" not in line:
            continue
        match = re.search(r"path=([^,\s]*\.gdb)\b", line)
        if match and (build is None or f"/{build}/" in line or f"/{build}/" in match.group(1) or build in line):
            found.append(match.group(1))
    found = sorted(set(found))
    if not found:
        raise SystemExit("no running --trace gdb build found; start one and pass --socket if auto-detect misses it")
    if len(found) > 1:
        raise SystemExit("multiple gdb sockets found; pass --socket explicitly:\n  " + "\n  ".join(found))
    return found[0]


def splice(client, unix):
    """Pump bytes both ways between the TCP client and the unix socket until either side closes."""
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ)
    selector.register(unix, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in selector.select():
                source = key.fileobj
                target = unix if source is client else client
                data = source.recv(4096)
                if not data:
                    return
                target.sendall(data)
    finally:
        selector.close()


def serve(unix_path, host, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as error:
        raise SystemExit(f"could not listen on {host}:{port}: {error}")
    listener.listen(1)
    print(f"gdb bridge: {host}:{port} <-> {unix_path}\nattach with:  lldb -o 'gdb-remote {host}:{port}'", flush=True)
    while True:
        client, _ = listener.accept()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix.connect(unix_path)
        except OSError as error:
            print(f"client rejected: cannot reach {unix_path}: {error}", file=sys.stderr, flush=True)
            client.close()
            time.sleep(0.2)
            continue
        print("client attached", flush=True)
        try:
            splice(client, unix)
        except OSError as error:
            print(f"session ended: {error}", file=sys.stderr, flush=True)
        finally:
            client.close()
            unix.close()
            print("client detached", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--socket", help="the session's .gdb unix socket (default: auto-detect the running build)")
    group.add_argument("--build", help="auto-detect the socket of this build's running session")
    parser.add_argument("--host", default="127.0.0.1", help="TCP listen address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=1234, help="TCP listen port (default 1234)")
    args = parser.parse_args()
    unix_path = args.socket or discover_socket(args.build)
    serve(unix_path, args.host, args.port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
