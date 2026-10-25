"""Host architecture table (hostarch.py): the guest each kind of Mac boots. No firmware, Docker or Hypervisor needed."""
from pathlib import Path
import sys
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import firmware
import hostarch
import native

ARM, INTEL = hostarch.ARCHES["arm64"], hostarch.ARCHES["x86_64"]


def sysctl(values):
    return lambda name: values.get(name)


class HostDetectionTests(unittest.TestCase):
    def detect(self, system, machine, values):
        with mock.patch.object(hostarch.platform, "system", return_value=system), \
                mock.patch.object(hostarch.platform, "machine", return_value=machine), \
                mock.patch.object(hostarch, "_sysctl", side_effect=sysctl(values)):
            return hostarch.machine()

    def test_apple_silicon_intel_and_others(self):
        self.assertEqual(self.detect("Darwin", "arm64", {"hw.optional.arm64": "1"}), "arm64")
        self.assertEqual(self.detect("Darwin", "x86_64", {}), "x86_64")
        self.assertIsNone(self.detect("Linux", "x86_64", {}))
        self.assertIsNone(self.detect("Darwin", "ppc", {}))

    def test_rosetta_terminal_reports_the_hardware_and_is_refused(self):
        self.assertEqual(self.detect("Darwin", "x86_64", {"hw.optional.arm64": "1"}), "arm64")
        with mock.patch.object(hostarch, "machine", return_value="arm64"), \
                mock.patch.object(hostarch, "translated", return_value=True):
            with self.assertRaisesRegex(ValueError, "Rosetta"):
                hostarch.current()
        with mock.patch.object(hostarch, "machine", return_value=None):
            with self.assertRaises(ValueError):
                hostarch.current()

    def test_table_is_complete(self):
        for arch in hostarch.ARCHES.values():
            self.assertEqual(set(arch), set(ARM))
            self.assertTrue(arch["qemu_target"].endswith("-softmmu"))
            self.assertTrue(arch["qemu_binary"].startswith("qemu-system-"))


class GuestArgumentTests(unittest.TestCase):
    def test_apple_silicon_arguments_are_upstreams(self):
        args = native.arguments(Path("/q"), Path("/g"), "/s", data=True, firmware=True, arch=ARM)
        self.assertEqual(args[:6], ["-machine", "virt,gic-version=3", "-cpu", "host", "-accel", "hvf"])
        self.assertIn("console=ttyAMA0 ", args[args.index("-append") + 1])
        self.assertIn("virtio-serial-device", args)
        self.assertIn("virtio-blk-device,drive=sp16fw,serial=sp16fw", args)
        self.assertFalse(any("-pci" in a for a in args))

    def test_intel_arguments_use_q35_pci_and_ttyS0(self):
        args = native.arguments(Path("/q"), Path("/g"), "/s", data=True, firmware=True, audio="file", arch=INTEL)
        self.assertEqual(args[:6], ["-machine", "q35", "-cpu", "host", "-accel", "hvf"])
        self.assertIn("console=ttyS0 ", args[args.index("-append") + 1])
        self.assertIn("virtio-serial-pci", args)
        for drive in ("sp16fw", "sp16emmc"):
            self.assertIn(f"virtio-blk-pci,drive={drive},serial={drive}", args)
        self.assertFalse(any("virtio-" in a and a.endswith("-device") for a in args))
        self.assertFalse(any(",gic-version" in a or "ttyAMA0" in a for a in args))
        # q35 boots through SeaBIOS: the firmware files sit next to the QEMU binary.
        self.assertEqual(args[6:8], ["-L", "/qemu-firmware"])

    def test_apple_silicon_needs_no_firmware_folder(self):
        self.assertNotIn("-L", native.arguments(Path("/b/qemu-system-aarch64"), Path("/g"), "/s", arch=ARM))

    def test_import_vm_follows_the_same_table(self):
        for arch, machine, blk in ((ARM, "virt,gic-version=3", "virtio-blk-device"), (INTEL, "q35", "virtio-blk-pci")):
            with self.subTest(arch=arch["name"]):
                args = firmware.import_arguments("/q", "/k", "/i", "/u", "/o", "/s", 123, arch=arch)
                self.assertEqual(args[args.index("-machine") + 1], machine)
                self.assertIn(f"console={arch['console']} ", args[args.index("-append") + 1])
                self.assertIn(hostarch.virtio("serial", arch), args)
                self.assertIn(f"{blk},drive=sp16upd,serial=sp16upd", args)
                self.assertIn(f"{blk},drive=sp16out,serial=sp16out", args)


class FirmwareStagingTests(unittest.TestCase):
    def test_intel_firmware_files_are_copied_next_to_qemu(self):
        import tempfile
        with tempfile.TemporaryDirectory() as work:
            source, build = Path(work, "pc-bios"), Path(work, "build")
            source.mkdir(), build.mkdir()
            for name in INTEL["firmware_files"]:
                (source / name).write_bytes(b"synthetic " + name.encode())
            native.stage_qemu_firmware(source, build / "qemu-system-x86_64", INTEL)
            self.assertEqual(sorted(p.name for p in (build / "qemu-firmware").iterdir()),
                             sorted(INTEL["firmware_files"]))
            native.stage_qemu_firmware(source, build / "qemu-system-aarch64", ARM)  # no-op
            (source / "kvmvapic.bin").unlink()
            with self.assertRaisesRegex(ValueError, "kvmvapic.bin"):
                native.stage_qemu_firmware(source, Path(work, "other", "qemu-system-x86_64"), INTEL)


class BuildPlumbingTests(unittest.TestCase):
    def test_guest_kernel_and_virtio_transport_follow_the_host(self):
        dockerfile = (TOOLS / "Dockerfile.native-base").read_text()
        self.assertIn("ARG KERNEL_PACKAGE=linux-image-arm64", dockerfile)
        self.assertIn("${KERNEL_PACKAGE}", dockerfile)
        init = (TOOLS / "guest-init.sh").read_text()
        self.assertIn("modprobe virtio_mmio || :", init)
        self.assertIn("modprobe virtio_pci || :", init)
        self.assertIn("hostarch.py", native.FWLIB_FILES)

    def test_swift_extra_devices_match_the_build_transport(self):
        swift = (TOOLS / "native-window.swift").read_text()
        self.assertIn('arguments.contains("virtio-serial-pci") ? "pci" : "device"', swift)
        self.assertNotIn('"virtio-blk-device,', swift)
        self.assertNotIn('"virtio-9p-device,', swift)


if __name__ == "__main__":
    unittest.main()
