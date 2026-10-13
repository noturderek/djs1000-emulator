"""The ./sp16 usb command group and the automatic stick attach in `./sp16 run`, with hdiutil and ps mocked."""
import collections
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import struct
import sys
import unittest
from unittest import mock
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sp16_cli import CliCase, cli, MAKEFILE  # noqa: E402


def write_wav(path, rate=44100, width=2, channels=1, frames=200, tag=None):
    """A PCM WAV made with the wave module, or (tag given) a hand-built header for other format codes."""
    if tag is None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(b"\0" * (frames * width * channels))
        return path
    body = struct.pack("<HHIIHH", tag, channels, rate, rate * width * channels, width * channels, width * 8)
    data = b"\0" * (frames * width * channels)
    blob = b"WAVE" + b"fmt " + struct.pack("<I", len(body)) + body + b"data" + struct.pack("<I", len(data)) + data
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(blob)) + blob)
    return path


def write_extensible_wav(path, rate=44100, bits=16, subformat=1):
    body = struct.pack("<HHIIHH", 0xFFFE, 2, rate, rate * 2 * bits // 8, 2 * bits // 8, bits)
    body += struct.pack("<HHI", 22, bits, 3) + struct.pack("<H", subformat) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    blob = b"WAVE" + b"fmt " + struct.pack("<I", len(body)) + body + b"data" + struct.pack("<I", 0)
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(blob)) + blob)
    return path


def extended(rate):
    """The 80-bit IEEE extended encoding of an integer sample rate (AIFF)."""
    exponent = rate.bit_length() - 1
    mantissa = rate << (63 - exponent)
    return struct.pack(">HQ", 16383 + exponent, mantissa)


def write_aiff(path, rate=44100, bits=16):
    comm = struct.pack(">hIh", 1, 10, bits) + extended(rate)
    blob = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", 8) + b"\0" * 8
    Path(path).write_bytes(b"FORM" + struct.pack(">I", len(blob)) + blob)
    return path


class UsbCase(CliCase):
    """A context whose helpers (hdiutil, cp, newfs_msdos) are faked: the 'mounted' image is a folder in the temp dir."""

    def setUp(self):
        super().setUp()
        self.tool_calls = []
        self.fail = {}                       # first word(s) of a tool command -> exit code to return
        self.mounts = []
        self.ctx.run_tool = self.run_tool
        patcher = mock.patch.object(cli.platform, "system", return_value="Darwin")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cli.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.samples = self.root / "samples"
        self.samples.mkdir()

    # fake tools
    def run_tool(self, argv, timeout=300):
        argv = list(argv)
        self.tool_calls.append(argv)
        key = " ".join(argv[:2])
        if key in self.fail:
            return self.fail[key], "", f"{argv[0]}: simulated failure"
        if argv[:2] == ["hdiutil", "attach"]:
            if "-mountpoint" in argv:
                point = argv[argv.index("-mountpoint") + 1]
                self.mounts.append(point)
                return 0, f"/dev/disk9\t\t\t\n/dev/disk9s1\t\t\t{point}\n", ""
            return 0, "/dev/disk9\t\t\t\n", ""
        if argv[:2] == ["hdiutil", "detach"]:
            for child in (Path(self.mounts[-1]).iterdir() if self.mounts and Path(self.mounts[-1]).is_dir() else ()):
                shutil.rmtree(str(child)) if child.is_dir() else child.unlink()  # the volume is gone, the folder is empty
            return 0, "", ""
        if argv[0] == "newfs_msdos":
            return 0, "", ""
        if argv[0] == "cp":
            self.assertEqual(argv[1], "-X")
            destination = Path(argv[3])
            shutil.copyfile(argv[2], str(destination))
            (destination.parent / ("._" + destination.name)).write_bytes(b"x")  # what macOS adds
            return 0, "", ""
        raise AssertionError(f"unexpected tool {argv}")

    def names(self):
        return [" ".join(c[:2]) if c[0] == "hdiutil" else c[0] for c in self.tool_calls]

    def stick(self):
        return self.build / "stick.img"

    def make_stick(self, formatted=True):
        path = self.stick()
        path.write_bytes((b"\xeb\x58\x90FAT32   " + b"\1" * 69989) if formatted else b"\0" * 70000)
        os.chmod(str(path), 0o600)
        return path

    def attached(self, image, mount=None):
        entries = [{"dev-entry": "/dev/disk9"}]
        if mount:
            entries.append({"dev-entry": "/dev/disk9s1", "mount-point": str(mount)})
        self.hdiutil = plistlib.dumps({"images": [{"image-path": os.path.realpath(str(image)), "system-entities": entries}]}).decode()

    def ps_row(self, pid, command, ppid=1):
        self.ps += f"  {pid}  {ppid} {command}\n"

    def usb(self, *args):
        return self.main("usb", *args)


class ParsingTests(UsbCase):
    def parse(self, *argv):
        return cli.BY_NAME["usb"].parser().parse_args(list(argv))

    def test_options_and_files_may_be_mixed(self):
        args = self.parse("add", "a.wav", "--to", "kicks", "b.wav", "--image", "s.img", "--any", "c.wav")
        self.assertEqual((args.action, args.files, args.to, args.image, args.any),
                         ("add", ["a.wav", "b.wav", "c.wav"], "kicks", "s.img", True))
        args = self.parse("create", "--size", "2G", "--force")
        self.assertEqual((args.action, args.files, args.size, args.force), ("create", [], "2G", True))
        self.assertEqual(self.parse("ls").files, [])

    def test_unknown_action_is_a_usage_error(self):
        code, _, err = self.usb("format")
        self.assertEqual(code, 2)
        self.assertIn("usb:", err)
        self.assertIn("./sp16 help usb", err)

    def test_options_for_other_actions_and_stray_files_are_refused(self):
        for argv in (["ls", "--force"], ["add", "a.wav", "--size", "2G"], ["create", "--to", "x"], ["mount", "--any"]):
            code, _, err = self.usb(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("only for usb", err)
        code, _, err = self.usb("ls", "a.wav")
        self.assertEqual(code, 2)
        self.assertIn("takes no files", err)
        code, _, err = self.usb("add")
        self.assertEqual(code, 2)
        self.assertIn("give the sample files", err)

    def test_files_after_double_dash_are_files(self):
        wav = write_wav(self.samples / "-odd.wav")
        code, out, _ = self.usb("add", "--", str(wav))
        self.assertEqual(code, 0, self.ctx.err.getvalue())
        self.assertIn("Added 1 file", out)

    def test_size_parsing(self):
        for text, size in (("1G", 1 << 30), ("512M", 512 << 20), ("2g", 2 << 30), ("1GiB", 1 << 30), ("64m", 64 << 20)):
            self.assertEqual(cli.parse_size(text), size, text)
        for text in ("", "big", "10", "63M", "33G", "-1G", "1.5G"):
            with self.assertRaises(cli.UsageError, msg=text):
                cli.parse_size(text)

    def test_dry_run_runs_nothing(self):
        wav = write_wav(self.samples / "k.wav")
        for argv in (["create"], ["add", str(wav), "--to", "kicks"], ["ls"], ["mount"], ["unmount"]):
            self.ctx.dry_run = True
            code, out, _ = self.usb(*argv)
            self.assertEqual(code, 0)
            self.assertIn(f"./sp16 usb {argv[0]}", out)
        self.assertEqual(self.tool_calls, [])
        self.assertFalse(self.stick().exists())


class ImageResolutionTests(UsbCase):
    def test_default_is_stick_img_in_the_build_folder(self):
        self.assertEqual(cli.resolve_image(self.ctx, None), self.build / "stick.img")

    def test_sp16emu_build_dir_moves_the_default(self):
        other = self.root / "elsewhere"
        ctx = cli.Context(tools=cli.HERE, root=self.root, env={"SP16EMU_BUILD_DIR": str(other)})
        self.assertEqual(cli.resolve_image(ctx, None), other / "stick.img")

    def test_given_paths_are_made_absolute(self):
        self.assertEqual(cli.resolve_image(self.ctx, "/x/y.img"), Path("/x/y.img"))
        self.assertEqual(cli.resolve_image(self.ctx, "rel.img"), Path.cwd() / "rel.img")
        self.assertEqual(cli.resolve_image(self.ctx, "~/s.img"), Path(os.path.expanduser("~/s.img")))
        self.assertEqual(cli.resolve_image(self.ctx, "/x/../y/./z.img"), Path("/y/z.img"))

    def test_commas_and_symlinks_are_refused(self):
        with self.assertRaisesRegex(cli.CliError, "comma"):
            cli.resolve_image(self.ctx, "/x/a,b.img")
        real = self.root / "real.img"
        real.write_bytes(b"x")
        link = self.root / "link.img"
        link.symlink_to(real)
        with self.assertRaisesRegex(cli.CliError, "symlink"):
            cli.resolve_image(self.ctx, str(link))

    def test_a_missing_default_stick_names_the_fix(self):
        code, _, err = self.usb("ls")
        self.assertEqual(code, 1)
        self.assertIn("./sp16 usb create", err)
        self.assertEqual(self.tool_calls, [])


class AutoAttachTests(UsbCase):
    def run_argv(self, *args):
        self.make_build("sp16")
        self.make_library(self.build / "library")
        return self.argv("run", *args)

    def test_no_stick_no_attach(self):
        argv, notes = self.run_argv()
        self.assertNotIn("--usb-image", argv)
        self.assertEqual(notes, [])

    def test_an_existing_default_stick_is_attached_with_a_note(self):
        stick = self.make_stick()
        argv, notes = self.run_argv()
        self.assertEqual(argv[argv.index("--usb-image") + 1], str(stick))
        self.assertTrue(any("attaching the USB stick" in n and "--no-usb" in n for n in notes), notes)

    def test_no_usb_opts_out(self):
        self.make_stick()
        argv, notes = self.run_argv("--no-usb")
        self.assertNotIn("--usb-image", argv)
        self.assertNotIn("--no-usb", argv)
        self.assertEqual(notes, [])

    def test_an_explicit_image_wins(self):
        self.make_stick()
        argv, notes = self.run_argv("--usb-image", "other.img")
        self.assertEqual(argv[argv.index("--usb-image") + 1], "other.img")
        self.assertEqual(argv.count("--usb-image"), 1)
        self.assertEqual(notes, [])

    def test_both_flags_contradict(self):
        self.make_stick()
        with self.assertRaisesRegex(cli.UsageError, "contradict"):
            self.run_argv("--usb-image", "x.img", "--no-usb")

    def test_a_stick_used_by_another_emulator_is_left_out_with_a_warning(self):
        stick = self.make_stick()
        self.ps_row(4321, f"/app/qemu-system-aarch64 -drive file={stick},format=raw,if=none")
        argv, notes = self.run_argv()
        self.assertNotIn("--usb-image", argv)
        self.assertEqual(len(notes), 1)
        self.assertIn("4321", notes[0])
        self.assertIn("without a USB stick", notes[0])

    def test_a_stick_mounted_on_the_mac_is_left_out(self):
        stick = self.make_stick()
        self.attached(stick, mount=self.root / "mnt")
        argv, notes = self.run_argv()
        self.assertNotIn("--usb-image", argv)
        self.assertTrue(any("mounted on this Mac" in n and "usb unmount" in n for n in notes), notes)

    def test_an_unreadable_process_or_disk_list_never_attaches(self):
        self.make_stick()
        self.ctx.capture = lambda argv, timeout=30: (1, "")
        argv, notes = self.run_argv()
        self.assertNotIn("--usb-image", argv)
        self.assertTrue(any("without a USB stick" in n for n in notes), notes)

    def test_a_non_regular_default_is_ignored(self):
        self.stick().mkdir()
        argv, notes = self.run_argv()
        self.assertNotIn("--usb-image", argv)
        self.assertEqual(notes, [])


class HolderTests(UsbCase):
    def test_processes_naming_the_image_are_holders(self):
        stick = self.make_stick()
        self.ps_row(100, "/bin/zsh")
        self.ps_row(200, f"/App/Contents/MacOS/emu --usb-image {stick} --firmware x")
        self.ps_row(201, f"qemu-system-aarch64 -drive file={stick},format=raw")
        self.assertEqual([p for p, _ in cli.image_holders(self.ctx, stick)], [200, 201])

    def test_similar_names_are_not_holders(self):
        stick = self.make_stick()
        self.ps_row(300, f"tail -f {stick}.log")
        self.ps_row(301, f"cp {stick}2 /tmp/x")
        self.ps_row(302, f"vim {self.root}/other/build/stick.img")
        self.ps_row(303, f"vim /prefix{stick}")
        self.assertEqual(cli.image_holders(self.ctx, stick), [])

    def test_this_process_and_its_parents_are_not_holders(self):
        stick = self.make_stick()
        self.ps_row(os.getpid(), f"python3 ./sp16 usb add --image {stick}", ppid=7001)
        self.ps_row(7001, f"make usb-add USB_IMAGE={stick}", ppid=7002)
        self.ps_row(7002, f"-zsh {stick}")
        self.ps_row(7003, f"make usb-add USB_IMAGE={stick}")   # an unrelated second command: a holder
        self.assertEqual([p for p, _ in cli.image_holders(self.ctx, stick)], [7003])

    def test_the_resolved_path_counts_too(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "viasymlink"
        link.symlink_to(real)
        image = link / "s.img"
        image.write_bytes(b"x")
        self.ps_row(400, f"qemu-system-aarch64 -drive file={real}/s.img")
        self.assertEqual([p for p, _ in cli.image_holders(self.ctx, image)], [400])

    def test_a_failing_ps_is_an_error_not_a_green_light(self):
        self.ctx.capture = lambda argv, timeout=30: (1, "")
        with self.assertRaisesRegex(cli.CliError, "cannot list the running processes"):
            cli.image_holders(self.ctx, self.root / "x.img")

    def test_every_mounting_command_refuses_while_an_emulator_holds_the_image(self):
        stick = self.make_stick()
        wav = write_wav(self.samples / "k.wav")
        self.ps_row(555, f"/App/emu --usb-image {stick}")
        for argv in (["add", str(wav)], ["ls"], ["mount"], ["create", "--force"]):
            code, _, err = self.usb(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("running emulator (process id 555)", err, argv)
            self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(stick.stat().st_size, 70000)  # the image was not recreated

    def test_a_blank_image_is_not_formatted_while_held(self):
        stick = self.make_stick(formatted=False)
        wav = write_wav(self.samples / "k.wav")
        self.ps_row(556, f"/App/emu --usb-image {stick}")
        self.assertEqual(self.usb("add", str(wav))[0], 1)
        self.assertEqual(self.tool_calls, [])

    def test_every_mounting_command_refuses_while_the_mac_has_it_attached(self):
        stick = self.make_stick()
        wav = write_wav(self.samples / "k.wav")
        self.attached(stick, mount="/Volumes/SP16USB")
        for argv in (["add", str(wav)], ["ls"], ["mount"], ["create", "--force"]):
            code, _, err = self.usb(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("already attached to this Mac as /dev/disk9 at /Volumes/SP16USB", err)
            self.assertIn("./sp16 usb unmount", err)
        self.assertEqual(self.tool_calls, [])


class AudioCheckTests(UsbCase):
    def label(self, path):
        return cli.audio_class(path)

    def test_pcm_16_and_24_bit_44100_are_fine(self):
        for width in (2, 3):
            for channels in (1, 2):
                self.assertIsNone(self.label(write_wav(self.samples / "a.wav", width=width, channels=channels)))
        self.assertIsNone(self.label(write_wav(self.samples / "b.wav", tag=1)))
        self.assertIsNone(self.label(write_extensible_wav(self.samples / "c.wav")))
        self.assertIsNone(self.label(write_extensible_wav(self.samples / "c24.wav", bits=24)))
        self.assertIsNone(self.label(write_aiff(self.samples / "d.aif")))
        self.assertIsNone(self.label(write_aiff(self.samples / "d24.aif", bits=24)))

    def test_other_formats_get_a_label_saying_what_differs(self):
        self.assertEqual(self.label(write_wav(self.samples / "x.wav", rate=48000)), "WAV 48 kHz")
        self.assertEqual(self.label(write_wav(self.samples / "x.wav", rate=22050)), "WAV 22.05 kHz")
        self.assertEqual(self.label(write_wav(self.samples / "x.wav", width=1)), "WAV 8-bit")
        self.assertEqual(self.label(write_wav(self.samples / "x.wav", rate=48000, width=3)), "WAV 48 kHz")
        self.assertEqual(self.label(write_wav(self.samples / "x.wav", rate=96000, width=1)), "WAV 8-bit, 96 kHz")

    def test_float_and_unclassifiable_extensible_are_flagged(self):
        self.assertEqual(self.label(write_wav(self.samples / "f.wav", width=4, tag=3)), "WAV not plain PCM")
        self.assertEqual(self.label(write_extensible_wav(self.samples / "g.wav", bits=32, subformat=3)),
                         "WAV not plain PCM")
        self.assertEqual(self.label(write_extensible_wav(self.samples / "h.wav", rate=48000)), "WAV 48 kHz")

    def test_aiff_is_checked_too(self):
        self.assertEqual(self.label(write_aiff(self.samples / "e.aiff", rate=48000, bits=24)), "AIFF 48 kHz")
        self.assertEqual(cli.extended_to_rate(extended(44100)), 44100)
        self.assertEqual(cli.extended_to_rate(extended(96000)), 96000)

    def test_unreadable_audio_files_are_flagged_not_fatal(self):
        short = self.samples / "short.wav"
        short.write_bytes(b"RIFF")
        junk = self.samples / "junk.wav"
        junk.write_bytes(b"x" * 100)
        for path in (short, junk, self.samples / "missing.wav"):
            self.assertIn("cannot be read", self.label(path))
        items, _ = cli.collect_samples(self.ctx, [str(short), str(junk)], False)
        self.assertEqual(len(items), 2)
        self.assertIn("2 file(s) are with an audio header that cannot be read", self.ctx.err.getvalue())

    def test_one_summary_line_per_format_class(self):
        for number in range(30):
            write_wav(self.samples / f"s{number:02d}.wav", rate=48000)
        for number in range(5):
            write_wav(self.samples / f"f{number}.wav", width=4, tag=3)
        for number in range(40):
            write_wav(self.samples / f"ok{number}.wav", width=3)
        items, _ = cli.collect_samples(self.ctx, [str(self.samples)], False)
        self.assertEqual(len(items), 75)
        lines = [l for l in self.ctx.err.getvalue().splitlines() if "warning:" in l]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("30 file(s) are WAV 48 kHz, not PCM 16- or 24-bit 44.1 kHz" in l and "s00.wav" in l for l in lines))
        self.assertTrue(any("5 file(s) are WAV not plain PCM" in l for l in lines))

    def test_collection_skips_other_files_and_hidden_ones(self):
        good = write_wav(self.samples / "kick.WAV")
        (self.samples / "notes.txt").write_text("hi")
        (self.samples / ".hidden.wav").write_bytes(b"x")
        (self.samples / "bad:name.wav").write_bytes(b"x")
        items, skipped = cli.collect_samples(self.ctx, [str(good), str(self.samples / "notes.txt"),
                                                        str(self.samples / ".hidden.wav"),
                                                        str(self.samples / "bad:name.wav")], False)
        self.assertEqual([r.name for _, r in items], ["kick.WAV"])
        self.assertEqual(skipped, 3)
        err = self.ctx.err.getvalue()
        self.assertIn("notes.txt: not WAV or AIFF audio", err)
        self.assertIn("--any", err)
        self.assertIn("hidden", err)
        self.assertIn("character that FAT does not allow", err)

    def test_any_copies_other_files(self):
        (self.samples / "notes.txt").write_text("hi")
        items, skipped = cli.collect_samples(self.ctx, [str(self.samples / "notes.txt")], True)
        self.assertEqual(len(items), 1)
        self.assertEqual(skipped, 0)

    def test_folders_are_walked_and_names_must_not_collide(self):
        folder = self.samples / "drums"
        (folder / "hats").mkdir(parents=True)
        write_wav(folder / "kick.wav")
        write_wav(folder / "hats" / "closed.wav")
        (folder / ".DS_Store").write_bytes(b"x")
        other = self.root / "other"
        other.mkdir()
        write_wav(other / "kick.wav")
        items, skipped = cli.collect_samples(self.ctx, [str(folder), str(other / "kick.wav"),
                                                        str(folder / "kick.wav")], False)
        self.assertEqual(sorted(str(r) for _, r in items), ["drums/hats/closed.wav", "drums/kick.wav", "kick.wav"])
        self.assertEqual(skipped, 2)  # the hidden .DS_Store, and the second kick.wav (same destination)
        self.assertIn("would replace", self.ctx.err.getvalue())

    def test_a_missing_source_is_an_error(self):
        with self.assertRaisesRegex(cli.CliError, "no such file: nope.wav"):
            cli.collect_samples(self.ctx, ["nope.wav"], False)

    def test_subdir_checks(self):
        self.assertEqual(cli.check_subdir(None), "")
        self.assertEqual(cli.check_subdir("kicks"), "kicks")
        self.assertEqual(cli.check_subdir("drums//kicks/"), "drums/kicks")
        for bad in ("", "/abs", "..", "a/../b", "a:b", "x*", "."):
            with self.assertRaises(cli.UsageError, msg=bad):
                cli.check_subdir(bad)


class WorkflowTests(UsbCase):
    def test_create_formats_like_the_guest_and_detaches_every_time(self):
        code, out, err = self.usb("create", "--size", "64M")
        self.assertEqual(code, 0, err)
        image = self.stick()
        self.assertEqual(image.stat().st_size, 64 << 20)
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.names(), ["hdiutil attach", "newfs_msdos", "hdiutil detach", "hdiutil attach", "hdiutil detach"])
        attach_raw, format_call = self.tool_calls[0], self.tool_calls[1]
        self.assertEqual(attach_raw[:4], ["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage"])
        self.assertIn("-nomount", attach_raw)
        self.assertEqual(format_call, ["newfs_msdos", "-F", "32", "-v", "SP16USB", "/dev/rdisk9"])
        self.assertIn("PIONEER DJ SAMPLER/Samples", out)
        self.assertEqual(self.mounts[0].split(os.sep)[-2], ".usb-mnt")
        self.assertTrue(self.mounts[0].startswith(str(self.build) + os.sep))
        self.assertFalse(Path(self.mounts[0]).exists())  # the temporary mount point is gone

    def test_create_makes_the_samples_folder(self):
        made = []
        real = cli.make_tree

        def recording(point, sub=""):
            target = real(point, sub)
            made.append(str(target.relative_to(point)))
            return target
        with mock.patch.object(cli, "make_tree", side_effect=recording):
            self.assertEqual(self.usb("create", "--size", "64M")[0], 0)
        self.assertEqual(made, ["PIONEER DJ SAMPLER/Samples"])

    def test_create_refuses_to_overwrite_unless_forced(self):
        stick = self.make_stick()
        code, _, err = self.usb("create")
        self.assertEqual(code, 1)
        self.assertIn("already exists; --force", err)
        self.assertEqual(stick.stat().st_size, 70000)
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(self.usb("create", "--force", "--size", "64M")[0], 0)
        self.assertEqual(stick.stat().st_size, 64 << 20)

    def test_a_failed_create_leaves_no_image_behind(self):
        self.fail["newfs_msdos -F"] = 1
        code, _, err = self.usb("create", "--size", "64M")
        self.assertEqual(code, 1)
        self.assertIn("newfs_msdos failed", err)
        self.assertFalse(self.stick().exists())
        self.assertEqual(self.names()[-1], "hdiutil detach")

    def test_add_creates_the_image_copies_and_cleans_up(self):
        wav = write_wav(self.samples / "kick.wav")
        wav24 = write_wav(self.samples / "pad.wav", width=3, rate=48000)
        code, out, err = self.usb("add", str(wav), str(wav24), "--to", "drums")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: 1 file(s) are WAV 48 kHz", err)
        self.assertIn("pad.wav", err)
        self.assertIn("creating", err)
        self.assertIn("Added 2 file(s)", out)
        self.assertIn("PIONEER DJ SAMPLER/Samples/drums", out)
        self.assertEqual([c[:2] for c in self.tool_calls if c[0] == "cp"], [["cp", "-X"]] * 2)
        self.assertEqual(self.names()[-1], "hdiutil detach")
        self.assertEqual(self.names().count("hdiutil attach"), self.names().count("hdiutil detach"))
        copy_targets = [c[3] for c in self.tool_calls if c[0] == "cp"]
        for target in copy_targets:
            self.assertIn("/PIONEER DJ SAMPLER/Samples/drums/", target)
        self.assertTrue(self.stick().exists())

    def test_add_removes_what_macos_adds_before_detaching(self):
        events = []
        real = cli.remove_junk
        with mock.patch.object(cli, "remove_junk", side_effect=lambda path: events.append("junk") or real(path)):
            orig = self.run_tool

            def traced(argv, timeout=300):
                if argv[:2] == ["hdiutil", "detach"]:
                    events.append("detach")
                return orig(argv, timeout)
            self.ctx.run_tool = traced
            self.make_stick()
            wav = write_wav(self.samples / "kick.wav")
            self.assertEqual(self.usb("add", str(wav))[0], 0)
        self.assertEqual(events, ["junk", "detach"])

    def test_the_junk_remover_only_removes_junk(self):
        mount = self.root / "vol"
        (mount / "PIONEER DJ SAMPLER" / "Samples").mkdir(parents=True)
        (mount / ".fseventsd").mkdir()
        (mount / ".fseventsd" / "uuid").write_text("x")
        (mount / ".DS_Store").write_text("x")
        samples = mount / "PIONEER DJ SAMPLER" / "Samples"
        (samples / "kick.wav").write_text("x")
        (samples / "._kick.wav").write_text("x")
        (samples / ".keep").write_text("x")
        self.assertEqual(cli.remove_junk(mount), 3)
        left = sorted(str(p.relative_to(mount)) for p in mount.rglob("*") if p.is_file())
        self.assertEqual(left, ["PIONEER DJ SAMPLER/Samples/.keep", "PIONEER DJ SAMPLER/Samples/kick.wav"])

    def test_a_blank_image_is_formatted_in_place(self):
        stick = self.make_stick(formatted=False)
        wav = write_wav(self.samples / "kick.wav")
        code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 0, err)
        self.assertIn("is blank; formatting", err)
        self.assertIn("newfs_msdos", self.names())
        self.assertEqual(stick.stat().st_size, 70000)  # formatted in place, not replaced

    def test_a_formatted_image_is_not_formatted_again(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        self.assertEqual(self.usb("add", str(wav))[0], 0)
        self.assertNotIn("newfs_msdos", self.names())

    def test_detach_happens_even_when_a_copy_fails(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        self.fail["cp -X"] = 1
        code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 1)
        self.assertIn("cannot copy", err)
        self.assertEqual(self.names()[-1], "hdiutil detach")
        self.assertFalse(Path(self.mounts[0]).exists())

    def test_a_busy_device_is_retried_then_forced(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        results = iter([1, 1, 0])
        orig = self.run_tool

        def flaky(argv, timeout=300):
            if argv[:2] == ["hdiutil", "detach"] and "-force" not in argv:
                self.tool_calls.append(list(argv))
                code = next(results, 1)
                return code, "", "hdiutil: couldn't unmount: Resource busy"
            return orig(argv, timeout)
        self.ctx.run_tool = flaky
        self.assertEqual(self.usb("add", str(wav))[0], 0)
        self.assertEqual(self.names().count("hdiutil detach"), 3)  # two busy answers, then success: no force
        self.assertFalse(any("-force" in c for c in self.tool_calls))

    def test_a_failed_detach_is_reported(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        self.fail["hdiutil detach"] = 16
        code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 1)
        self.assertIn("cannot detach /dev/disk9", err)
        self.assertEqual(sum(1 for c in self.tool_calls if c[:2] == ["hdiutil", "detach"]), 6)  # 5 tries, then force

    def test_not_enough_room_is_refused_before_copying(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        with mock.patch.object(shutil, "disk_usage", return_value=collections.namedtuple("usage", "total used free")(100, 90, 10)):
            code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 1)
        self.assertIn("stick has 10 B free", err)
        self.assertNotIn("cp", self.names())
        self.assertEqual(self.names()[-1], "hdiutil detach")

    def test_replacing_a_file_is_said(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        real = cli.make_tree

        def with_existing(point, sub="", root="Samples"):
            target = real(point, sub, root)
            (target / "kick.wav").write_bytes(b"old")
            return target
        with mock.patch.object(cli, "make_tree", side_effect=with_existing):
            code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 0)
        self.assertIn("replaced kick.wav", err)

    def test_nothing_to_add_is_an_error_and_touches_nothing(self):
        (self.samples / "notes.txt").write_text("x")
        code, _, err = self.usb("add", str(self.samples / "notes.txt"))
        self.assertEqual(code, 1)
        self.assertIn("nothing to add", err)
        self.assertEqual(self.tool_calls, [])
        self.assertFalse(self.stick().exists())

    def test_ls_mounts_read_only_and_hides_macos_files(self):
        self.make_stick()

        def listing(argv, timeout=300):
            self.tool_calls.append(list(argv))
            if argv[:2] == ["hdiutil", "attach"]:
                point = Path(argv[argv.index("-mountpoint") + 1])
                (point / "PIONEER DJ SAMPLER" / "Samples").mkdir(parents=True)
                (point / "PIONEER DJ SAMPLER" / "Samples" / "kick.wav").write_bytes(b"x" * 2048)
                (point / "PIONEER DJ SAMPLER" / "Samples" / "._kick.wav").write_bytes(b"x")
                (point / ".fseventsd").mkdir()
                return 0, f"/dev/disk9\t\t\t{point}\n", ""
            return 0, "", ""
        self.ctx.run_tool = listing
        code, out, err = self.usb("ls")
        self.assertEqual(code, 0, err)
        attach = next(c for c in self.tool_calls if c[:2] == ["hdiutil", "attach"])
        self.assertIn("-readonly", attach)
        self.assertEqual(out.splitlines()[0].split(": ")[1].split(",")[0], "1 file(s)")
        self.assertIn("PIONEER DJ SAMPLER/Samples/kick.wav  2.0 KiB", out)
        self.assertNotIn("._kick", out)
        self.assertNotIn("fseventsd", out)
        self.assertEqual(self.tool_calls[-1][:2], ["hdiutil", "detach"])

    def test_mount_keeps_it_mounted_and_prints_the_mount_point(self):
        stick = self.make_stick()
        code, out, err = self.usb("mount")
        self.assertEqual(code, 0, err)
        point = out.strip()
        self.assertEqual(point, self.mounts[0])
        self.assertTrue(point.startswith(str(self.build / ".usb-mnt") + os.sep))
        self.assertEqual(self.names(), ["hdiutil attach"])
        self.assertIn("usb unmount", err)
        # a second call while it is mounted at the same place just prints it again
        self.attached(stick, mount=point)
        self.tool_calls.clear()
        code, out, _ = self.usb("mount")
        self.assertEqual((code, out.strip()), (0, point))
        self.assertEqual(self.tool_calls, [])

    def test_unmount_detaches_and_removes_our_mount_point(self):
        stick = self.make_stick()
        point = self.build / ".usb-mnt" / "stick-1"
        point.mkdir(parents=True)
        (point / "._junk").write_text("x")
        self.attached(stick, mount=point)
        code, out, err = self.usb("unmount")
        self.assertEqual(code, 0, err)
        self.assertIn("Unmounted", out)
        self.assertEqual(self.tool_calls, [["hdiutil", "detach", "/dev/disk9"]])
        self.assertFalse(point.exists())

    def test_unmount_of_an_image_mounted_elsewhere_leaves_that_folder_alone(self):
        stick = self.make_stick()
        elsewhere = self.root / "Vol"
        elsewhere.mkdir()
        (elsewhere / "._keep").write_text("x")
        self.attached(stick, mount=elsewhere)
        self.assertEqual(self.usb("unmount")[0], 0)
        self.assertTrue((elsewhere / "._keep").exists())
        self.assertTrue(elsewhere.exists())

    def test_unmount_when_not_mounted(self):
        self.make_stick()
        code, out, _ = self.usb("unmount")
        self.assertEqual(code, 0)
        self.assertIn("is not mounted", out)
        self.assertEqual(self.tool_calls, [])

    def test_clean_everything_refuses_while_a_stick_is_mounted(self):
        point = self.build / ".usb-mnt" / "stick-1"
        point.mkdir(parents=True)
        (self.build / "stick.img").write_bytes(b"x")
        with mock.patch.object(cli.os.path, "ismount", side_effect=lambda p: str(p) == str(point)):
            code, _, err = self.main("clean", "--everything", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("usb unmount", err)
        self.assertTrue((self.build / "stick.img").exists())

    def test_the_mount_point_stays_inside_the_build_folder(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        self.usb("add", str(wav))
        self.assertTrue(all(m.startswith(str(self.build) + os.sep) for m in self.mounts))
        # nothing else was created next to the image or in the samples folder
        self.assertEqual(sorted(p.name for p in self.build.iterdir()), [".usb-mnt", "stick.img"])
        self.assertEqual(sorted(p.name for p in self.samples.iterdir()), ["kick.wav"])

    def test_an_image_outside_build_is_used_but_mounted_inside_build(self):
        image = self.root / "gig.img"
        wav = write_wav(self.samples / "kick.wav")
        self.assertEqual(self.usb("add", str(wav), "--image", str(image))[0], 0)
        self.assertTrue(image.exists())
        self.assertTrue(all(m.startswith(str(self.build) + os.sep) for m in self.mounts))
        self.assertFalse((self.build / "stick.img").exists())

    def test_needs_macos(self):
        with mock.patch.object(cli.platform, "system", return_value="Linux"):
            code, _, err = self.usb("ls")
            self.assertEqual(code, 1)
            self.assertIn("need macOS", err)


class LargePackTests(UsbCase):
    def make_pack(self, count=3):
        pack = self.samples / "Pack"
        for sub in ("kicks", "loops/long"):
            (pack / sub).mkdir(parents=True)
        for number in range(count):
            write_wav(pack / "kicks" / f"k{number}.wav")
        write_wav(pack / "loops" / "long" / "l.wav", width=3)
        (pack / "readme.txt").write_text("x")
        (pack / "__MACOSX").mkdir()
        write_wav(pack / "__MACOSX" / "ghost.wav")
        return pack

    def test_tree_keeps_the_structure_without_the_folder_name(self):
        pack = self.make_pack()
        items, skipped = cli.collect_samples(self.ctx, [], False, [str(pack)])
        self.assertEqual(sorted(str(r) for _, r in items),
                         ["kicks/k0.wav", "kicks/k1.wav", "kicks/k2.wav", "loops/long/l.wav"])
        self.assertEqual(skipped, 1)  # readme.txt; __MACOSX is never entered

    def test_a_folder_argument_keeps_its_name(self):
        items, _ = cli.collect_samples(self.ctx, [str(self.make_pack())], False)
        self.assertIn("Pack/loops/long/l.wav", [str(r) for _, r in items])

    def test_tree_needs_a_folder(self):
        wav = write_wav(self.samples / "a.wav")
        with self.assertRaisesRegex(cli.CliError, "needs a folder"):
            cli.collect_samples(self.ctx, [], False, [str(wav)])

    def test_add_tree_copies_into_the_subfolder_with_the_structure(self):
        self.make_stick()
        pack = self.make_pack()
        code, out, err = self.usb("add", "--tree", str(pack), "--to", "Factory")
        self.assertEqual(code, 0, err)
        targets = sorted(c[3].split("/Samples/")[1] for c in self.tool_calls if c[0] == "cp")
        self.assertEqual(targets, ["Factory/kicks/k0.wav", "Factory/kicks/k1.wav", "Factory/kicks/k2.wav",
                                   "Factory/loops/long/l.wav"])
        self.assertIn("Added 4 file(s)", out)
        self.assertEqual(self.usb("ls", "--tree", "x")[0], 2)

    def test_many_skips_are_summarised_too(self):
        for number in range(30):
            (self.samples / f"n{number:02d}.txt").write_text("x")
        write_wav(self.samples / "a.wav")
        items, skipped = cli.collect_samples(self.ctx, [str(self.samples)], False)
        self.assertEqual((len(items), skipped), (1, 30))
        err = self.ctx.err.getvalue()
        self.assertEqual(err.count("skipping"), cli.Chatter.LIMIT)
        self.assertIn("22 more files that are not WAV or AIFF audio (not listed)", err)

    def test_size_suggestions(self):
        self.assertEqual(cli.suggest_size(1 << 20), "2G")
        self.assertEqual(cli.suggest_size(int(2.2e9)), "4G")
        self.assertEqual(cli.suggest_size(int(5.1e9)), "8G")
        self.assertEqual(cli.suggest_size(9 << 30), "16G")
        self.assertEqual(cli.suggest_size(99 << 30), "32G")

    def test_eight_and_thirty_two_gigabytes_are_accepted(self):
        self.assertEqual(cli.parse_size("8G"), 8 << 30)
        self.assertEqual(cli.parse_size("32G"), 32 << 30)

    def test_a_new_stick_is_sized_for_the_files(self):
        pack = self.make_pack()
        with mock.patch.object(cli, "suggest_size", return_value="8G"):
            code, _, err = self.usb("add", "--tree", str(pack))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stick().stat().st_size, 8 << 30)
        self.assertIn("8.0 GiB", err)

    def test_not_enough_room_suggests_a_size_and_a_new_image(self):
        self.make_stick()
        wav = write_wav(self.samples / "kick.wav")
        usage = collections.namedtuple("usage", "total used free")(100, 90, 10)
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            code, _, err = self.usb("add", str(wav))
        self.assertEqual(code, 1)
        self.assertIn("nothing was copied", err)
        self.assertIn("usb create --size 2G --force", err)
        self.assertIn("--image PATH", err)



class PrivacyFolderTests(UsbCase):
    """A repository inside Downloads, Desktop, Documents or iCloud Drive: macOS refuses a mount whose mount point is in
    there (mount(2) EPERM, reported by hdiutil as "no mountable file systems"), so the mount point goes to the cache."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.project = self.home / "Downloads" / "sp16-emulator"
        self.build = self.project / "build"
        self.build.mkdir(parents=True, mode=0o700)
        self.ctx.home = self.home
        self.ctx.root = self.project
        self.ctx.build = self.build
        self.cache = self.home / "Library" / "Caches" / "sp16emu" / "usb-mnt"

    def test_protected_folders_are_recognised(self):
        home = self.home
        for path in (home / "Downloads", home / "Downloads" / "x" / "y", home / "downloads" / "x",
                     home / "Desktop" / "x", home / "Documents", home / "Library" / "Mobile Documents"
                     / "com~apple~CloudDocs" / "x"):
            self.assertIsNotNone(cli.protected_folder(path, home), str(path))
        for path in (home / "Downloadsx", home / "Projects" / "x", home / "Library" / "Mobile Documents" / "com~apple~Other",
                     self.root / "build"):
            self.assertIsNone(cli.protected_folder(path, home), str(path))
        self.assertEqual(cli.protected_folder(self.build, home), str(home / "Downloads"))

    def test_mount_root_moves_out_of_a_protected_build_folder(self):
        self.assertEqual(cli.usb_mount_root(self.ctx), self.cache)
        self.ctx.build = self.root / "build"
        self.assertEqual(cli.usb_mount_root(self.ctx), self.root / "build" / ".usb-mnt")

    def test_create_mounts_outside_the_protected_folder(self):
        code, _, err = self.usb("create", "--size", "64M")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.stick().exists())
        self.assertTrue(self.mounts[0].startswith(str(self.cache) + os.sep), self.mounts)
        self.assertFalse(self.mounts[0].startswith(str(self.project) + os.sep))
        expected = ["hdiutil attach", "newfs_msdos", "hdiutil detach", "hdiutil attach", "hdiutil detach"]
        self.assertTrue(self.names() == expected, self.names())

    def test_mount_and_unmount_clean_up_in_the_cache(self):
        self.make_stick()
        code, out, err = self.usb("mount")
        self.assertEqual(code, 0, err)
        here = Path(out.strip())
        self.assertTrue(str(here).startswith(str(self.cache) + os.sep))
        self.assertTrue(here.is_dir())
        (here / ".DS_Store").write_bytes(b"x")
        self.attached(self.stick(), mount=here)
        code, _, err = self.usb("unmount")
        self.assertEqual(code, 0, err)
        self.assertFalse((here / ".DS_Store").exists() or here.exists())
        self.assertEqual(self.names()[-1], "hdiutil detach")

    def test_a_refused_mount_names_the_privacy_cause(self):
        self.fail["hdiutil attach"] = 1
        point = self.project / "mnt"
        with self.assertRaises(cli.CliError) as caught:
            cli.hdiutil_attach(self.ctx, self.stick(), ["-nobrowse", "-mountpoint", str(point)])
        message = str(caught.exception)
        self.assertIn("privacy protection", message)
        self.assertIn("Full Disk Access", message)
        self.assertNotIn("usb create --force", message)

    def test_a_failed_mount_during_create_detaches_and_leaves_no_image(self):
        original = self.run_tool

        def refuse_mounts(argv, timeout=300):
            if argv[:2] == ["hdiutil", "attach"] and "-mountpoint" in argv:
                self.tool_calls.append(list(argv))
                return 1, "", "hdiutil: attach failed - no mountable file systems"
            return original(argv, timeout)
        self.ctx.run_tool = refuse_mounts
        code, _, err = self.usb("create", "--size", "64M")
        self.assertEqual(code, 1)
        self.assertIn("no mountable file systems", err)
        self.assertFalse(self.stick().exists())
        # the format pass detached its device; the refused mount had none to detach
        self.assertTrue(self.names() == ["hdiutil attach", "newfs_msdos", "hdiutil detach", "hdiutil attach"], self.names())


class HelpCoverageTests(UsbCase):
    def test_help_usb_lists_every_option_action_and_the_safety_rule(self):
        code, out, _ = self.main("help", "usb")
        self.assertEqual(code, 0)
        for word in ("create", "add", "ls", "mount", "unmount", "--image", "--size", "--force", "--to", "--any",
                     "24-bit 44.1 kHz", "running emulator", "Examples:"):
            self.assertIn(word, out)

    def test_run_help_mentions_the_automatic_stick(self):
        out = self.main("help", "run")[1]
        self.assertIn("--no-usb", out)
        self.assertIn("build/stick.img", out)

    def test_the_makefile_lists_the_usb_targets(self):
        text = MAKEFILE.read_text()
        for target in ("usb-create", "usb-add", "usb-ls", "usb-mount", "usb-unmount"):
            self.assertRegex(text, rf"(?m)^{target}:.*## \S")


class DestTests(UsbCase):
    def test_dest_paths_are_checked(self):
        for text, wanted in (("Projects", "Projects"), ("Projects/", "Projects"), ("PIONEER DJ SAMPLER/Projects", "Projects"),
                             (cli.STICK_FOLDER.lower() + "/Projects/Demos", "Projects/Demos"), ("Samples/drums", "Samples/drums"),
                             (None, "")):
            self.assertEqual(cli.check_dest(text), wanted, text)
        for text in ("", "  ", "/Projects", "~/Projects", "..", "Projects/../..", "../Projects", "a/./b", "a\\b",
                     "PIONEER DJ SAMPLER", "PIONEER DJ SAMPLER/..", "Pro:jects", "x\0y"):
            with self.assertRaises(cli.UsageError, msg=repr(text)):
                cli.check_dest(text)

    def test_add_dest_copies_any_file_type_under_the_firmware_folder(self):
        self.make_stick()
        project = self.samples / "Demo 01.prj"
        project.write_bytes(b"project")
        code, out, err = self.usb("add", str(project), "--dest", "Projects")
        self.assertEqual(code, 0, err)
        self.assertIn("Added 1 file(s)", out)
        self.assertIn("PIONEER DJ SAMPLER/Projects", out)
        copies = [c[3] for c in self.tool_calls if c[0] == "cp"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0].endswith("/PIONEER DJ SAMPLER/Projects/Demo 01.prj"))
        self.assertNotIn("not WAV or AIFF", err)

    def test_add_dest_inside_samples_still_skips_what_is_not_audio(self):
        self.make_stick()
        (self.samples / "notes.txt").write_text("x")
        wav = write_wav(self.samples / "k.wav")
        code, out, err = self.usb("add", str(self.samples / "notes.txt"), str(wav), "--dest", "Samples/kicks")
        self.assertEqual(code, 0, err)
        self.assertIn("skipping notes.txt", err)
        self.assertTrue([c[3] for c in self.tool_calls if c[0] == "cp"][0].endswith("/Samples/kicks/k.wav"))

    def test_add_dest_cannot_leave_the_firmware_folder(self):
        self.make_stick()
        project = self.samples / "p.prj"
        project.write_bytes(b"x")
        for dest in ("..", "../Other", "/etc", "Projects/../../x", "~"):
            code, _, err = self.usb("add", str(project), "--dest", dest)
            self.assertEqual(code, 2, dest)
            self.assertIn("--dest", err)
        self.assertEqual([c for c in self.tool_calls if c[0] == "cp"], [])

    def test_dest_and_to_exclude_each_other_and_dest_is_for_add_only(self):
        project = self.samples / "p.prj"
        project.write_bytes(b"x")
        code, _, err = self.usb("add", str(project), "--dest", "Projects", "--to", "x")
        self.assertEqual(code, 2)
        self.assertIn("exclude each other", err)
        code, _, err = self.usb("ls", "--dest", "Projects")
        self.assertEqual(code, 2)
        self.assertIn("only for usb add", err)

    def test_add_dest_removes_what_macos_adds(self):
        self.make_stick()
        project = self.samples / "p.prj"
        project.write_bytes(b"x")
        with mock.patch.object(cli, "remove_junk", wraps=cli.remove_junk) as junk:
            self.assertEqual(self.usb("add", str(project), "--dest", "Projects")[0], 0)
        self.assertEqual(junk.call_count, 1)
        self.assertEqual(self.names()[-1], "hdiutil detach")

    def test_dry_run_shows_the_destination(self):
        project = self.samples / "p.prj"
        project.write_bytes(b"x")
        self.ctx.dry_run = True
        code, out, _ = self.usb("add", str(project), "--dest", "Projects")
        self.assertEqual(code, 0)
        self.assertIn("'PIONEER DJ SAMPLER/Projects'", out)
        self.assertEqual(self.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
