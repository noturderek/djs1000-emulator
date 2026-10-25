"""The guest VM for each kind of Mac. Host side; stdlib only, Python 3.9+ (the app bundles a copy with the importer).

The firmware's ARM32 application always runs under qemu-arm-static inside the guest, so only the guest itself depends
on the host: an aarch64 guest on Apple silicon, an x86_64 guest on Intel, both under Hypervisor.framework. Everything
that differs (QEMU target, machine, console, virtio transport, Debian kernel) is in this one table.
"""
from pathlib import Path
import platform
import subprocess

# QEMU firmware the guest boots with, copied from the pinned QEMU source (pc-bios/) next to the QEMU binary.
FIRMWARE_DIR = "qemu-firmware"

ARCHES = {
    "arm64": {
        "name": "Apple silicon",
        "qemu_target": "aarch64-softmmu",
        "qemu_binary": "qemu-system-aarch64",
        "machine": "virt,gic-version=3",
        "console": "ttyAMA0",
        # virtio-mmio transport on the virt board: virtio-blk-device, virtio-serial-device, ...
        "virtio": "device",
        "kernel_package": "linux-image-arm64",
        "docker_platform": "linux/arm64",
        # The virt board boots -kernel directly: no firmware files.
        "firmware_files": [],
    },
    "x86_64": {
        "name": "Intel",
        "qemu_target": "x86_64-softmmu",
        "qemu_binary": "qemu-system-x86_64",
        "machine": "q35",
        "console": "ttyS0",
        "virtio": "pci",
        "kernel_package": "linux-image-amd64",
        "docker_platform": "linux/amd64",
        # q35 starts in SeaBIOS, which loads -kernel through the linuxboot option ROM.
        "firmware_files": ["bios-256k.bin", "linuxboot_dma.bin", "linuxboot.bin", "kvmvapic.bin"],
    },
}


def _sysctl(name):
    try:
        result = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def translated():
    """True when this process runs under Rosetta: it would build an Intel QEMU on an Apple silicon Mac."""
    return _sysctl("sysctl.proc_translated") == "1"


def machine():
    """The Mac's native architecture key ("arm64" or "x86_64"), or None on another system."""
    if platform.system() != "Darwin":
        return None
    if _sysctl("hw.optional.arm64") == "1":
        return "arm64"
    value = platform.machine()
    return value if value in ARCHES else None


def current():
    """The table entry for this Mac. Refuses other systems and Rosetta (open a native terminal instead)."""
    key = machine()
    if key is None:
        raise ValueError("the emulator runs on a Mac (Apple silicon or Intel) only")
    if key == "arm64" and translated():
        raise ValueError("this terminal runs under Rosetta; open a native terminal to build for Apple silicon")
    return ARCHES[key]


def virtio(kind, arch):
    """QEMU device name of a virtio device on this guest, e.g. virtio("blk", arch) -> virtio-blk-pci."""
    return f"virtio-{kind}-{arch['virtio']}"


def firmware_dir(qemu):
    """Where the QEMU binary at this path finds its firmware files."""
    return Path(qemu).parent / FIRMWARE_DIR


def vm_arguments(arch, qemu=None):
    """The machine, CPU and accelerator arguments every guest boot starts with (plus -L for firmware, when needed)."""
    firmware = ["-L", str(firmware_dir(qemu))] if arch["firmware_files"] and qemu is not None else []
    return ["-machine", arch["machine"], "-cpu", "host", "-accel", "hvf", *firmware]
