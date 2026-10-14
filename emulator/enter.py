#!/usr/bin/env python3
"""Enter the disposable guest root with the sampler's expected working directory.

The file-size bound is deliberate: 1 GiB, well below 2 GiB where the firmware's 32-bit file offset handling becomes unsafe. SIGXFSZ is ignored so an over-limit write fails with EFBIG
instead of terminating the app. Ignored dispositions survive execve into qemu-arm-static and the guest.
"""

import os
import resource
import signal
import sys


FSIZE_LIMIT = 1024 * 1024 * 1024


def apply_file_size_limit(limit=FSIZE_LIMIT):
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)


def app_cwd():
    """The application's working directory: /root/pdj (the 1.60 default) unless the init passes the firmware
    profile's runtime.cwd in SP16_APP_CWD (a plain absolute path)."""
    cwd = os.environ.get("SP16_APP_CWD", "/root/pdj")
    parts = cwd.split("/")
    if not cwd.startswith("/") or ".." in parts or any(not p.replace(".", "").replace("_", "").replace("-", "").isalnum()
                                                       for p in parts[1:] if p):
        raise SystemExit("invalid application working directory")
    return cwd


def main(argv):
    if len(argv) < 3 or argv[1] != "/tmp/sp16-root" or argv[2] != "/qemu-arm-static":
        raise SystemExit("expected disposable SP-16 root and QEMU entrypoint")
    cwd = app_cwd()
    apply_file_size_limit()
    os.chroot(argv[1])
    os.chdir(cwd)
    os.execv(argv[2], argv[2:])


if __name__ == "__main__":
    main(sys.argv)
