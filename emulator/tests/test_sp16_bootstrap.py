"""./sp16 bootstrap and firmware download: step planning, zip safety, the official-host allowlist, update checks.

No test touches the network, a real disk image or a real firmware: the server, hdiutil, cp and the tools are faked."""
import io
import json
import os
from pathlib import Path
import shlex
import struct
import sys
import unittest
from unittest import mock
import urllib.request
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sp16_cli import cli, PYTHON  # noqa: E402
from test_sp16_cli_usb import UsbCase, write_wav  # noqa: E402

OFFICIAL = "https://downloads.support.alphatheta.com/firmwares/music-production/TORAIZ_SP-16/TORAIZSP16_ver{}.zip"


def make_upd(version="1.40", seed=b"\x07", corrupt=False):
    """A synthetic update with a valid stock trailer (marker, version, CRC) and a sector-aligned payload."""
    payload = seed * 1024
    crc = zlib.crc32(payload) ^ (1 if corrupt else 0)
    trailer = (b"LIVESAMPLER" + version.encode() + b"\0").ljust(16, b"\0") + struct.pack("<I", crc)
    return payload + trailer


def make_zip(members):
    """members: [(name, bytes)] or [(ZipInfo, bytes)]."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def link_info(name):
    info = zipfile.ZipInfo(name)
    info.external_attr = 0o120777 << 16
    return info


class FakeResponse:
    def __init__(self, body=b"", length=None, status=200):
        self.headers = {"Content-Length": str(len(body) if length is None else length)}
        self.status = status
        self._data = io.BytesIO(body)
        self.closed = False

    def read(self, count=-1):
        return self._data.read(count)

    def close(self):
        self.closed = True


class FakeServer:
    """Stands for the official server: HEAD gives the size, GET the zip (Range is honoured)."""

    def __init__(self, body, length=None):
        self.body, self.length, self.calls = body, length, []

    def __call__(self, url, method="GET", headers=None):
        self.calls.append((method, url, dict(headers or {})))
        if method == "HEAD":
            return FakeResponse(b"", length=len(self.body) if self.length is None else self.length)
        wanted = (headers or {}).get("Range")
        if wanted:
            return FakeResponse(self.body[int(wanted.split("=")[1].rstrip("-")):], status=206)
        return FakeResponse(self.body)

    def gets(self):
        return [c for c in self.calls if c[0] == "GET"]


class UrlRuleTests(unittest.TestCase):
    def test_every_version_maps_to_its_official_url(self):
        self.assertEqual(cli.DOWNLOAD_VERSIONS, ("1.60", "1.50", "1.40", "1.31"))
        for version, code in (("1.60", "160"), ("1.50", "150"), ("1.40", "140"), ("1.31", "131")):
            url = cli.firmware_url(version)
            self.assertEqual(url, OFFICIAL.format(code))
            self.assertEqual(cli.check_firmware_url(url), url)

    def test_other_versions_are_refused(self):
        for version in ("1.70", "1.6", "", "../1.60", "1.60 "):
            with self.assertRaises(cli.UsageError, msg=version):
                cli.firmware_url(version)

    def test_only_the_official_https_url_passes(self):
        good = OFFICIAL.format("160")
        bad = [
            good.replace("https:", "http:"),
            good.replace("downloads.support.alphatheta.com", "example.com"),
            good.replace("downloads.support.alphatheta.com", "downloads.support.alphatheta.com.example.com"),
            good.replace("https://", "https://downloads.support.alphatheta.com@example.com/#"),
            good.replace("https://downloads", "https://user:pw@downloads"),
            good.replace(".com/", ".com:8443/"),
            good + "?x=1", good + "#frag",
            good.replace("ver160", "ver170"), good.replace("music-production", "other-product"),
            good.replace("/firmwares/", "/other/"),
            "file:///etc/passwd", "ftp://downloads.support.alphatheta.com/x", "", "https://",
            good.replace("TORAIZSP16_ver160.zip", "../../x/TORAIZSP16_ver160.zip"),
        ]
        for url in bad:
            with self.assertRaises(cli.CliError, msg=url):
                cli.check_firmware_url(url)

    def test_a_redirect_that_leaves_the_host_or_the_path_is_refused(self):
        handler = cli.OfficialRedirects()
        request = urllib.request.Request(OFFICIAL.format("160"))
        for target in ("https://example.com/TORAIZSP16_ver160.zip", "http://downloads.support.alphatheta.com/x.zip",
                       "https://downloads.support.alphatheta.com/firmwares/other.zip", "/elsewhere/file.zip"):
            with self.assertRaises(cli.CliError, msg=target):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        followed = handler.redirect_request(request, None, 302, "Found", {}, OFFICIAL.format("160"))
        self.assertEqual(followed.full_url, OFFICIAL.format("160"))

    def test_the_opener_checks_the_url_before_any_network_use(self):
        with mock.patch.object(urllib.request, "build_opener", side_effect=AssertionError("network")):
            with self.assertRaises(cli.CliError):
                cli.open_official("https://example.com/TORAIZSP16_ver160.zip")


class DownloadCase(UsbCase):
    def setUp(self):
        super().setUp()
        self.server = FakeServer(b"")
        self.ctx.open_url = lambda *a, **k: self.server(*a, **k)

    def serve(self, members=None, version="1.40", **kwargs):
        members = members if members is not None else [("SP16.UPD", make_upd(version))]
        self.server = FakeServer(make_zip(members), **kwargs)
        return self.server

    def download(self, version="1.40", **kwargs):
        kwargs.setdefault("yes", True)
        return cli.fetch_official_firmware(self.ctx, version, **kwargs)


class DownloadTests(DownloadCase):
    def test_a_download_verifies_extracts_and_reports_the_hash(self):
        self.serve()
        result = self.download()
        update = Path(result["update"])
        self.assertEqual(update, self.build / "downloads" / "ver140" / "SP16.UPD")
        self.assertEqual(update.read_bytes(), make_upd("1.40"))
        self.assertEqual(update.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["status"], "firmware_downloaded")
        self.assertFalse(result["pinned"])  # no profile pins 1.40
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["source"], OFFICIAL.format("140"))
        self.assertEqual([p.name for p in update.parent.iterdir()], ["SP16.UPD"])  # the zip is not kept
        self.assertIn("no profile pins version 1.40", self.ctx.err.getvalue())

    def test_head_comes_first_and_only_the_official_url_is_used(self):
        self.serve()
        self.download()
        self.assertEqual([c[0] for c in self.server.calls], ["HEAD", "GET"])
        self.assertEqual({c[1] for c in self.server.calls}, {OFFICIAL.format("140")})

    def test_a_pinned_hash_must_match(self):
        self.serve(version="1.60")
        with self.assertRaisesRegex(cli.CliError, "differs from the one the 1.60 profile pins"):
            self.download("1.60")
        self.assertEqual(list((self.build / "downloads" / "ver160").iterdir()), [])
        sha = cli.firmware_module(self.ctx).inspect_update  # the real function, on the fixture
        update = self.root / "fixture.UPD"
        update.write_bytes(make_upd("1.60"))
        pinned = sha(update)["sha256"]
        with mock.patch.object(cli, "pinned_update_sha", return_value=pinned):
            result = self.download("1.60")
        self.assertTrue(result["pinned"])
        self.assertEqual(result["sha256"], pinned)
        self.assertIn("matches the SHA-256 the profile pins", self.ctx.err.getvalue())

    def test_the_shipped_profiles_pin_1_50_and_1_60(self):
        for version in ("1.60", "1.50"):
            self.assertRegex(cli.pinned_update_sha(self.ctx, version), r"^[0-9a-f]{64}$")
        self.assertIsNone(cli.pinned_update_sha(self.ctx, "1.40"))

    def test_the_trailer_version_must_be_the_requested_one(self):
        self.serve(version="1.50")
        with self.assertRaisesRegex(cli.CliError, "update for 1.50"):
            self.download("1.40")
        self.assertEqual(list((self.build / "downloads" / "ver140").iterdir()), [])

    def test_a_damaged_update_fails_the_crc_check(self):
        self.serve([("SP16.UPD", make_upd("1.40", corrupt=True))])
        with self.assertRaisesRegex(cli.CliError, "CRC"):
            self.download()
        self.assertEqual(list((self.build / "downloads" / "ver140").iterdir()), [])

    def test_a_file_that_is_not_an_update_is_refused(self):
        self.serve([("SP16.UPD", b"x" * 1024)])
        with self.assertRaisesRegex(cli.CliError, "trailer|small|sector"):
            self.download()

    def test_the_zip_must_hold_exactly_one_update(self):
        for members, text in (([("readme.txt", b"hi")], "exactly one .UPD file, and it holds 0"),
                              ([("a.UPD", make_upd()), ("b.UPD", make_upd())], "holds 2")):
            self.serve(members)
            with self.assertRaisesRegex(cli.CliError, text):
                self.download()
        self.server = FakeServer(b"not a zip at all")
        with self.assertRaisesRegex(cli.CliError, "not a valid zip"):
            self.download()

    def test_unsafe_member_paths_are_refused_and_nothing_is_written(self):
        evil = [("../evil.UPD", make_upd()), ("/abs/evil.UPD", make_upd()), ("dir\\evil.UPD", make_upd()),
                ("a/../../evil.UPD", make_upd()), ("C:/evil.UPD", make_upd())]
        for name, data in evil:
            self.serve([(name, data)])
            with self.assertRaisesRegex(cli.CliError, "unsafe entry", msg=name):
                self.download()
        self.serve([(link_info("SP16.UPD"), b"/etc/passwd")])
        with self.assertRaisesRegex(cli.CliError, "unsafe entry"):
            self.download()
        self.assertFalse((self.build / "evil.UPD").exists())
        self.assertEqual(list((self.build / "downloads" / "ver140").iterdir()), [])

    def test_the_update_keeps_a_plain_name_even_from_a_subfolder(self):
        self.serve([("Release/SP16.UPD", make_upd()), ("__MACOSX/Release/._SP16.UPD", b"x"),
                    ("Firmware/notes.pdf", b"x")])
        result = self.download()
        self.assertEqual(Path(result["update"]).name, "SP16.UPD")
        self.serve([("odd name.UPD", make_upd())])
        (self.build / "downloads" / "ver140" / "SP16.UPD").unlink()
        with self.assertRaisesRegex(cli.CliError, "not a plain name"):
            self.download()

    def test_nothing_is_downloaded_without_agreement(self):
        server = self.serve()
        with self.assertRaisesRegex(cli.CliError, "needs the user's confirmation.*--yes"):
            self.download(yes=False)
        self.assertEqual(server.gets(), [])
        self.assertEqual([c[0] for c in server.calls], ["HEAD"])
        self.ctx.interactive = True
        with mock.patch.object(sys, "stdin", io.StringIO("n\n")):
            with self.assertRaisesRegex(cli.CliError, "not confirmed"):
                self.download(yes=False)
        self.assertEqual(server.gets(), [])
        with mock.patch.object(sys, "stdin", io.StringIO("y\n")):
            self.download(yes=False)
        self.assertEqual(len(server.gets()), 1)
        question = self.ctx.out.getvalue()
        for text in ("TORAIZSP16_ver140.zip", OFFICIAL.format("140"), "Download it? [y/N]"):
            self.assertIn(text, question)

    def test_the_question_names_file_url_and_size(self):
        server = self.serve()
        with self.assertRaises(cli.CliError) as caught:
            self.download(yes=False)
        message = str(caught.exception)
        self.assertIn("TORAIZSP16_ver140.zip", message)
        self.assertIn(OFFICIAL.format("140"), message)
        self.assertRegex(message, r"\(\d+(\.\d)? (B|KiB|MiB)\)")
        self.assertEqual(len(server.body) > 0, True)

    def test_info_only_prints_and_downloads_nothing(self):
        server = self.serve()
        result = self.download(yes=False, info_only=True)
        self.assertEqual(result["status"], "firmware_download_info")
        self.assertEqual(result["bytes"], len(server.body))
        self.assertEqual([c[0] for c in server.calls], ["HEAD"])
        self.assertIn(OFFICIAL.format("140"), self.ctx.err.getvalue())
        self.assertFalse((self.build / "downloads" / "ver140").exists())

    def test_a_second_run_reuses_the_verified_update_without_the_network(self):
        server = self.serve()
        first = self.download()
        calls = len(server.calls)
        second = self.download(yes=False)
        self.assertEqual(len(server.calls), calls)
        self.assertTrue(second["reused"])
        self.assertEqual(second["sha256"], first["sha256"])

    def test_a_damaged_earlier_file_is_replaced(self):
        server = self.serve()
        folder = self.build / "downloads" / "ver140"
        folder.mkdir(parents=True, mode=0o700)
        (folder / "SP16.UPD").write_bytes(make_upd("1.40", corrupt=True))
        result = self.download()
        self.assertFalse(result["reused"])
        self.assertEqual(len(server.gets()), 1)
        self.assertEqual((folder / "SP16.UPD").read_bytes(), make_upd("1.40"))

    def test_an_interrupted_download_resumes(self):
        server = self.serve()
        folder = self.build / "downloads" / "ver140"
        folder.mkdir(parents=True, mode=0o700)
        (folder / "TORAIZSP16_ver140.zip.part").write_bytes(server.body[:100])
        result = self.download()
        self.assertEqual(server.gets()[0][2], {"Range": "bytes=100-"})
        self.assertEqual(Path(result["update"]).read_bytes(), make_upd("1.40"))

    def test_a_short_download_says_how_to_resume(self):
        server = self.serve()
        server.length = len(server.body) + 50
        with self.assertRaisesRegex(cli.CliError, "stopped at.*again to resume"):
            self.download()

    def test_an_implausible_size_is_refused_before_downloading(self):
        server = self.serve(length=cli.MAX_DOWNLOAD_BYTES + 1)
        with self.assertRaisesRegex(cli.CliError, "not a firmware update download"):
            self.download()
        self.assertEqual(server.gets(), [])

    def test_the_download_folder_can_be_chosen(self):
        self.serve()
        target = self.root / "elsewhere"
        result = self.download(to=str(target))
        self.assertEqual(Path(result["update"]).parent, target / "ver140")
        self.assertEqual((target / "ver140").stat().st_mode & 0o777, 0o700)

    def test_a_symlinked_download_folder_is_refused(self):
        self.serve()
        real = self.root / "real"
        real.mkdir()
        (self.root / "link").symlink_to(real)
        with self.assertRaisesRegex(cli.CliError, "symlink"):
            self.download(to=str(self.root / "link"))


class DownloadCommandTests(DownloadCase):
    def test_the_command_prints_json(self):
        self.serve()
        code, out, _ = self.main("firmware", "download", "--version", "1.40", "--yes")
        self.assertEqual(code, 0, self.ctx.err.getvalue())
        data = json.loads(out)
        self.assertEqual(data["status"], "firmware_downloaded")
        self.assertEqual(data["version"], "1.40")

    def test_without_yes_a_script_is_told_to_ask_first(self):
        self.serve()
        code, _, err = self.main("firmware", "download", "--version", "1.40")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(self.server.gets(), [])

    def test_info_prints_the_facts(self):
        self.serve()
        code, out, err = self.main("firmware", "download", "--version", "1.40", "--info")
        self.assertEqual(code, 0)
        self.assertIn("TORAIZSP16_ver140.zip", err)
        self.assertEqual(json.loads(out)["status"], "firmware_download_info")

    def test_options_are_checked(self):
        self.assertEqual(self.main("firmware", "download", "--version", "9.99")[0], 2)
        self.assertEqual(self.main("firmware", "download")[0], 2)
        for extra in (["--update", "x"], ["--key-file", "x"], ["--id", "x"], ["--gpl-archive", "x"]):
            self.assertEqual(self.main("firmware", "download", "--version", "1.40", "--yes", *extra)[0], 2, extra)
        self.assertEqual(self.main("firmware", "list", "--yes")[0], 2)
        self.assertEqual(self.main("firmware", "list", "--version", "1.40")[0], 2)

    def test_dry_run_touches_nothing(self):
        self.serve()
        self.ctx.dry_run = True
        code, out, _ = self.main("firmware", "download", "--version", "1.60")
        self.assertEqual(code, 0)
        self.assertIn("downloads.support.alphatheta.com", out)
        self.assertEqual(self.server.calls, [])
        self.assertFalse((self.build / "downloads").exists())

    def test_it_needs_no_build(self):
        self.serve()
        self.assertFalse(list(self.build.iterdir()))
        self.assertEqual(self.main("firmware", "download", "--version", "1.40", "--yes")[0], 0)


class ZipSafetyTests(UsbCase):
    def test_a_zip_is_unpacked_without_junk_and_with_private_modes(self):
        archive = self.root / "pack.zip"
        archive.write_bytes(make_zip([("Pack/kick.wav", b"k"), ("Pack/sub/snare.wav", b"s"), ("__MACOSX/Pack/._kick.wav", b"x"),
                                      ("Pack/.DS_Store", b"x"), ("Pack/empty/", b"")]))
        target = cli.extract_zip_safely(self.ctx, archive, self.root / "out")
        found = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
        self.assertEqual(found, ["Pack/kick.wav", "Pack/sub/snare.wav"])
        self.assertEqual((target / "Pack" / "kick.wav").stat().st_mode & 0o777, 0o600)

    def test_unsafe_entries_stop_everything(self):
        for name in ("../evil.wav", "/abs.wav", "a\\b.wav", "x/../../y.wav"):
            archive = self.root / "bad.zip"
            archive.write_bytes(make_zip([("ok.wav", b"1"), (name, b"2")]))
            with self.assertRaisesRegex(cli.CliError, "unsafe entry", msg=name):
                cli.extract_zip_safely(self.ctx, archive, self.root / "out-x")
        archive.write_bytes(make_zip([(link_info("link.wav"), b"/etc/passwd")]))
        with self.assertRaisesRegex(cli.CliError, "unsafe entry"):
            cli.zip_check(archive)
        self.assertFalse((self.root.parent / "evil.wav").exists())
        self.assertFalse((self.root / "out-x").exists())

    def test_too_big_or_too_many_is_refused(self):
        archive = self.root / "big.zip"
        archive.write_bytes(make_zip([("a.wav", b"x" * 100)]))
        with mock.patch.object(cli, "STICK_MAX_SIZE", 50):
            with self.assertRaisesRegex(cli.CliError, "more than a stick can take"):
                cli.zip_check(archive)
        with mock.patch.object(cli, "BOOTSTRAP_MAX_FILES", 0):
            with self.assertRaisesRegex(cli.CliError, "more than a stick can take"):
                cli.zip_check(archive)

    def test_a_damaged_zip_and_a_wrong_file_are_named(self):
        broken = self.root / "broken.zip"
        broken.write_bytes(b"PK nothing")
        with self.assertRaisesRegex(cli.CliError, "not a readable zip"):
            cli.zip_check(broken)
        text = self.root / "notes.txt"
        text.write_text("x")
        with self.assertRaisesRegex(cli.CliError, "neither a folder nor a .zip"):
            cli.check_stick_source(str(text), "samples")
        with self.assertRaisesRegex(cli.CliError, "does not exist"):
            cli.check_stick_source(str(self.root / "missing"), "projects")

    def test_a_zip_with_one_top_folder_keeps_its_name_and_loose_files_use_the_zip_name(self):
        one = self.root / "Whatever.zip"
        one.write_bytes(make_zip([("MY PACK/a.wav", b"1"), ("MY PACK/b/c.wav", b"2")]))
        loose = self.root / "Loose Pack.zip"
        loose.write_bytes(make_zip([("a.wav", b"1"), ("b/c.wav", b"2")]))
        work = self.root / "work"
        work.mkdir()
        self.assertEqual(cli.materialize_source(self.ctx, one, work).name, "MY PACK")
        self.assertEqual(cli.materialize_source(self.ctx, loose, work).name, "Loose Pack")
        again = cli.materialize_source(self.ctx, loose, work)  # the same zip name twice does not collide
        self.assertEqual(again.name, "Loose Pack-2")
        folder = self.root / "plain"
        folder.mkdir()
        self.assertEqual(cli.materialize_source(self.ctx, folder, work), folder)


class StickSizeTests(unittest.TestCase):
    def test_sizes_round_up_from_1g_to_32g(self):
        gib = 1 << 30
        for needed, wanted in ((0, 1), (10, 1), (int(0.9 * gib), 1), (gib, 2), (int(2.2 * gib), 4), (4 * gib, 8),
                               (int(7.5 * gib), 8), (int(15 * gib), 16), (int(30 * gib), 32)):
            self.assertEqual(cli.stick_size_for(needed), wanted * gib, needed)
        with self.assertRaisesRegex(cli.CliError, "does not fit the largest stick"):
            cli.stick_size_for(32 * gib)


class BootCase(UsbCase):
    """A context whose tool commands are recorded; a fake `native.py build` makes the build, a fake import the entry."""

    def setUp(self):
        super().setUp()
        self.upd = self.root / "SP16.UPD"
        self.upd.write_bytes(make_upd("1.40"))
        self.key = self.root / "the.key"
        self.key.write_bytes(b"k")
        self.docs = [("ok", "all fine", "", "")]
        patcher = mock.patch.object(cli, "doctor_checks", side_effect=lambda ctx: list(self.docs))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx.call = self.fake_call
        self.tools = []
        self.entry_id = None

    # fake tools -------------------------------------------------------------------------------------------
    def fake_call(self, argv, env=None):
        argv = list(argv)
        self.calls.append((argv, dict(env or {})))
        words = argv[2:]
        if words[:1] == ["build"]:
            audio = words[words.index("--audio") + 1] if "--audio" in words else ""
            output = Path(words[words.index("--output") + 1])
            self.make_build(output.name, audio=audio)
        elif words[:2] == ["firmware", "import"]:
            sha = cli.firmware_module(self.ctx).inspect_update(words[words.index("--update") + 1])["sha256"]
            self.add_entry(sha)
        return 0

    def add_entry(self, sha, selected=False):
        self.entry_id = f"sp16-1.40-{sha[:12]}"
        folder = self.build / "library" / self.entry_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({"update": {"sha256": sha, "trailer_version": "1.40"}}))
        if selected:
            (self.build / "library" / "selected.json").write_text(json.dumps({"id": self.entry_id}))

    def update_sha(self):
        return cli.firmware_module(self.ctx).inspect_update(self.upd)["sha256"]

    def commands(self):
        """The tool commands run so far as (tool, subcommand words)."""
        return [(Path(c[1]).name, c[2:4] if c[2] == "firmware" else c[2:3]) for c, _ in self.calls]

    def bootstrap(self, *args):
        return self.main("bootstrap", *args)

    def pack(self, name="PACK"):
        folder = self.samples / name
        (folder / "sub").mkdir(parents=True)
        write_wav(folder / "kick.wav")
        write_wav(folder / "sub" / "snare.wav")
        return folder

    def projects(self):
        folder = self.root / "Projects"
        folder.mkdir()
        (folder / "Demo 01.prj").write_bytes(b"prj1")
        (folder / "Demo 02.prj").write_bytes(b"prj22")
        return folder

    def states(self, *args):
        boot = cli.plan_bootstrap(self.ctx, cli.BY_NAME["bootstrap"].parser().parse_args(list(args)))
        return {key: state for key, _, state, _ in boot.steps}


class PlanTests(BootCase):
    def test_a_fresh_machine_runs_every_step(self):
        folder = self.pack()
        states = self.states("--update", str(self.upd), "--key-file", str(self.key), "--samples", str(folder),
                             "--projects", str(self.projects()))
        self.assertEqual(states, {"doctor": "run", "build": "run", "firmware": "run", "select": "run", "usb": "run",
                                  "run": "run"})

    def test_a_partly_done_machine_skips_what_exists(self):
        self.make_build("sp16", audio="coreaudio")
        self.add_entry(self.update_sha(), selected=True)
        folder = self.pack()
        states = self.states("--update", str(self.upd), "--samples", str(folder))
        self.assertEqual(states, {"doctor": "run", "build": "skip", "firmware": "skip", "select": "skip", "usb": "run",
                                  "run": "run"})

    def test_an_unselected_firmware_is_selected_without_importing_again(self):
        self.make_build("sp16", audio="coreaudio")
        self.add_entry(self.update_sha(), selected=False)
        states = self.states("--update", str(self.upd))
        self.assertEqual((states["firmware"], states["select"]), ("skip", "run"))

    def test_a_fully_done_machine_skips_everything_but_the_check(self):
        self.make_build("sp16", audio="file")
        self.add_entry(self.update_sha(), selected=True)
        states = self.states("--audio", "file", "--update", str(self.upd), "--no-run")
        self.assertEqual(states, {"doctor": "run", "build": "skip", "firmware": "skip", "select": "skip", "usb": "skip",
                                  "run": "skip"})

    def test_a_running_emulator_is_not_launched_again(self):
        folder = self.make_build("sp16", audio="coreaudio")
        self.ps_row(4242, f"{folder}/DJS1K Replicator.app/Contents/MacOS/x")
        self.add_entry(self.update_sha(), selected=True)
        self.assertEqual(self.states("--update", str(self.upd))["run"], "skip")

    def test_a_different_audio_mode_offers_a_rebuild(self):
        self.make_build("sp16", audio="file")
        with self.assertRaisesRegex(cli.CliError, "built with audio 'file', not 'coreaudio'.*--rebuild"):
            self.states("--update", str(self.upd), "--key-file", str(self.key))
        self.assertEqual(self.states("--rebuild", "--update", str(self.upd), "--key-file", str(self.key))["build"], "run")
        self.assertEqual(self.states("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key))["build"], "skip")
        with self.assertRaisesRegex(cli.CliError, "device"):
            self.states("--audio", "file", "--audio-device", "UID1", "--update", str(self.upd), "--key-file", str(self.key))

    def test_inputs_are_checked_before_anything_runs(self):
        parse = ("--update", str(self.upd))
        with self.assertRaisesRegex(cli.UsageError, "needs --key-file KEY or --gpl-archive"):
            self.states(*parse)
        with self.assertRaisesRegex(cli.UsageError, "library is empty"):
            self.states()
        with self.assertRaisesRegex(cli.UsageError, "not both"):
            self.states(*parse, "--download-firmware", "1.60", "--key-file", str(self.key))
        with self.assertRaisesRegex(cli.UsageError, "not both"):
            self.states(*parse, "--key-file", str(self.key), "--gpl-archive", "a.zip")
        with self.assertRaisesRegex(cli.UsageError, "needs --audio coreaudio"):
            self.states(*parse, "--key-file", str(self.key), "--audio", "file", "--audio-device", "X")
        with self.assertRaisesRegex(cli.CliError, "--update: .* does not exist"):
            self.states("--update", str(self.root / "no.UPD"), "--key-file", str(self.key))
        with self.assertRaisesRegex(cli.CliError, "--key-file: .* does not exist"):
            self.states(*parse, "--key-file", str(self.root / "no.key"))
        with self.assertRaisesRegex(cli.CliError, "--samples: .* does not exist"):
            self.states(*parse, "--key-file", str(self.key), "--samples", str(self.root / "nowhere"))
        bad = self.root / "bad.zip"
        bad.write_bytes(make_zip([("../x.wav", b"1")]))
        with self.assertRaisesRegex(cli.CliError, "unsafe entry"):
            self.states(*parse, "--key-file", str(self.key), "--projects", str(bad))
        (self.root / "damaged.UPD").write_bytes(make_upd("1.40", corrupt=True))
        with self.assertRaisesRegex(cli.CliError, "--update: .*CRC"):
            self.states("--update", str(self.root / "damaged.UPD"), "--key-file", str(self.key))

    def test_an_empty_library_with_no_firmware_given_is_a_usage_error_but_a_filled_one_is_fine(self):
        self.add_entry(self.update_sha(), selected=True)
        states = self.states()
        self.assertEqual((states["firmware"], states.get("select")), ("skip", None))

    def test_a_pinned_download_already_in_the_library_needs_no_network(self):
        self.make_build("sp16", audio="coreaudio")
        self.add_entry(self.update_sha(), selected=True)
        with mock.patch.object(cli, "pinned_update_sha", return_value=self.update_sha()):
            states = self.states("--download-firmware", "1.40")
        self.assertEqual((states["firmware"], states["select"]), ("skip", "skip"))

    def test_a_download_without_a_key_is_refused_up_front(self):
        with self.assertRaisesRegex(cli.UsageError, "--key-file KEY or --gpl-archive"):
            self.states("--download-firmware", "1.60")
        self.assertEqual(self.states("--download-firmware", "1.60", "--key-file", str(self.key))["firmware"], "run")


class RunTests(BootCase):
    def test_a_fresh_run_does_every_step_in_order(self):
        folder = self.pack()
        code, out, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                        "--samples", str(folder), "--projects", str(self.projects()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands(), [("native.py", ["build"]), ("native.py", ["firmware", "import"]),
                                           ("native.py", ["firmware", "select"]), ("native.py", ["launch"])])
        build_call, import_call, select_call, launch_call = [c for c, _ in self.calls]
        self.assertEqual(build_call[build_call.index("--audio") + 1], "file")
        self.assertIn("--decoupled", build_call)
        self.assertEqual(import_call[import_call.index("--update") + 1], str(self.upd))
        self.assertEqual(import_call[import_call.index("--key-file") + 1], str(self.key))
        self.assertEqual(select_call[select_call.index("--id") + 1], self.entry_id)
        self.assertEqual(launch_call[launch_call.index("--usb-image") + 1], str(self.stick()))
        for number in range(1, 7):
            self.assertIn(f"[{number}/6]", out)
        copies = sorted(c[3].split("PIONEER DJ SAMPLER/")[1] for c in self.tool_calls if c[0] == "cp")
        self.assertEqual(copies, ["Projects/Demo 01.prj", "Projects/Demo 02.prj", "Samples/PACK/kick.wav",
                                  "Samples/PACK/sub/snare.wav"])
        self.assertEqual(self.stick().stat().st_size, 1 << 30)
        self.assertIn("Done.", out)

    def test_the_default_audio_is_coreaudio_and_a_device_passes_through(self):
        self.assertEqual(self.bootstrap("--update", str(self.upd), "--key-file", str(self.key), "--no-run",
                                        "--audio-device", "UID-1")[0], 0)
        build_call = self.calls[0][0]
        self.assertEqual(build_call[build_call.index("--audio") + 1], "coreaudio")
        self.assertEqual(build_call[build_call.index("--audio-device") + 1], "UID-1")

    def test_the_qemu_donor_passes_to_the_build(self):
        donor = self.make_build("donor", audio="file")
        code, _, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key), "--no-run",
                                      "--reuse-qemu-from", str(donor))
        self.assertEqual(code, 0, err)
        build_call = self.calls[0][0]
        self.assertEqual(build_call[build_call.index("--reuse-qemu-from") + 1], str(donor))

    def test_a_second_run_skips_every_step(self):
        folder = self.pack()
        first = ("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key), "--samples", str(folder),
                 "--no-run")
        self.assertEqual(self.bootstrap(*first)[0], 0)
        (self.build / "library" / "selected.json").write_text(json.dumps({"id": self.entry_id}))
        self.make_stick()                              # the first run's (faked) formatting leaves a real file system
        self.calls.clear()
        self.tool_calls.clear()
        with mock.patch.object(cli, "stick_missing", return_value=[]) as missing:
            code, out, err = self.bootstrap(*first)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertEqual([c for c in self.tool_calls if c[0] == "cp"], [])
        self.assertEqual(missing.call_count, 1)
        self.assertEqual(out.count("skipped"), 4)       # build, firmware, select, launch
        self.assertIn("nothing to add", out)

    def test_only_the_missing_files_are_copied(self):
        folder = self.pack()
        self.make_stick()
        self.add_entry(self.update_sha(), selected=True)
        self.make_build("sp16", audio="file")
        lacking = [([(folder / "sub" / "snare.wav", Path("PACK/sub/snare.wav"))], "Samples")]
        with mock.patch.object(cli, "stick_missing", return_value=lacking):
            code, out, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--samples", str(folder), "--no-run")
        self.assertEqual(code, 0, err)
        copies = [c[3] for c in self.tool_calls if c[0] == "cp"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0].endswith("PIONEER DJ SAMPLER/Samples/PACK/sub/snare.wav"))
        self.assertIn("added 1 file(s)", out)

    def test_zips_are_unpacked_under_build_and_removed_again(self):
        samples = self.root / "samples.zip"
        wav = write_wav(self.root / "t.wav").read_bytes()
        samples.write_bytes(make_zip([("Loop Pack/a.wav", wav), ("__MACOSX/Loop Pack/._a.wav", b"x")]))
        projects = self.root / "Factory_Projects.zip"
        projects.write_bytes(make_zip([("Demo 01.prj", b"p1"), ("Demo 02.prj", b"p2")]))
        seen = []
        real = cli.boot_usb

        def spy(ctx, args, boot):
            def listing():
                return sorted(p.name for p in self.build.iterdir() if p.name.startswith(".bootstrap-"))
            result = real(ctx, args, boot)
            seen.append(listing())
            return result
        with mock.patch.object(cli, "boot_usb", side_effect=spy):
            code, out, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                            "--samples", str(samples), "--projects", str(projects), "--no-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [[]])
        copies = sorted(c[3].split("PIONEER DJ SAMPLER/")[1] for c in self.tool_calls if c[0] == "cp")
        self.assertEqual(copies, ["Projects/Demo 01.prj", "Projects/Demo 02.prj", "Samples/Loop Pack/a.wav"])
        self.assertEqual([c for c in self.tool_calls if c[0] == "cp" and "._" in c[2]], [])

    def test_the_temporary_folder_is_removed_when_a_copy_fails(self):
        samples = self.root / "samples.zip"
        write_wav(self.root / "t.wav")
        samples.write_bytes(make_zip([("a.wav", (self.root / "t.wav").read_bytes())]))
        self.fail["cp -X"] = 1
        code, _, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                      "--samples", str(samples), "--no-run")
        self.assertEqual(code, 1)
        self.assertIn("cannot copy", err)
        self.assertEqual([p.name for p in self.build.iterdir() if p.name.startswith(".bootstrap-")], [])

    def test_a_failing_step_stops_the_run_with_its_exit_code(self):
        real = self.fake_call

        def failing(argv, env=None):
            if list(argv)[2:4] == ["firmware", "import"]:
                self.calls.append((list(argv), {}))
                return 7
            return real(argv, env)
        self.ctx.call = failing
        code, _, _ = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key))
        self.assertEqual(code, 7)
        self.assertEqual(self.commands(), [("native.py", ["build"]), ("native.py", ["firmware", "import"])])

    def test_a_failed_check_stops_before_anything_is_built(self):
        self.docs = [("fail", "Docker running (builds only; running needs no Docker)", "installed but not running",
                      "start Docker Desktop")]
        code, out, _ = self.bootstrap("--update", str(self.upd), "--key-file", str(self.key))
        self.assertEqual(code, 1)
        self.assertIn("fix: start Docker Desktop", out)
        self.assertIn("run the same bootstrap command again", out)
        self.assertEqual(self.calls, [])

    def test_docker_is_not_needed_once_the_build_exists(self):
        self.make_build("sp16", audio="file")
        self.add_entry(self.update_sha(), selected=True)
        self.docs = [("fail", "Docker running (builds only; running needs no Docker)", "", "start Docker Desktop")]
        code, _, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--no-run")
        self.assertEqual(code, 0, err)
        self.docs = [("fail", "Xcode Command Line Tools", "", "xcode-select --install")]
        self.assertEqual(self.bootstrap("--audio", "file", "--update", str(self.upd), "--no-run")[0], 1)

    def test_the_download_runs_between_build_and_import(self):
        server = FakeServer(make_zip([("SP16.UPD", make_upd("1.40"))]))
        self.ctx.open_url = server
        code, out, err = self.bootstrap("--audio", "file", "--download-firmware", "1.40", "--yes", "--key-file",
                                        str(self.key), "--no-run")
        self.assertEqual(code, 0, err)
        import_call = self.calls[1][0]
        self.assertEqual(import_call[import_call.index("--update") + 1],
                         str(self.build / "downloads" / "ver140" / "SP16.UPD"))
        self.assertEqual([c[0] for c in server.calls], ["HEAD", "GET"])

    def test_the_download_needs_agreement_when_not_interactive(self):
        server = FakeServer(make_zip([("SP16.UPD", make_upd("1.40"))]))
        self.ctx.open_url = server
        code, _, err = self.bootstrap("--audio", "file", "--download-firmware", "1.40", "--key-file", str(self.key))
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(server.gets(), [])

    def test_the_stick_size_can_be_chosen(self):
        folder = self.pack()
        code, _, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                      "--samples", str(folder), "--stick-size", "64M", "--no-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stick().stat().st_size, 64 << 20)

    def test_nothing_that_passes_no_check_is_added(self):
        junk = self.root / "junk"
        junk.mkdir()
        (junk / "notes.txt").write_text("x")
        code, _, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                      "--samples", str(junk), "--no-run")
        self.assertEqual(code, 1)
        self.assertIn("nothing to add", err)


class DryRunTests(BootCase):
    def test_a_dry_run_lists_every_step_and_changes_nothing(self):
        folder = self.pack()
        self.ctx.dry_run = True
        code, out, err = self.bootstrap("--audio", "file", "--update", str(self.upd), "--key-file", str(self.key),
                                        "--samples", str(folder), "--projects", str(self.projects()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tool_calls, [])
        self.assertFalse(self.stick().exists())
        self.assertIn("dry run", out)
        for number in range(1, 7):
            self.assertIn(f"[{number}/6]", out)
        for command in ("build", "firmware import", "firmware select", "launch"):
            self.assertRegex(out, rf"\+ \S+ \S+native\.py {command} ")
        self.assertIn("--key-file", out)
        self.assertIn(str(self.stick()), out)
        self.assertIn("PIONEER DJ SAMPLER/Projects", out)
        self.assertEqual([p.name for p in self.build.iterdir()], [])

    def test_a_dry_run_says_what_it_would_skip(self):
        self.make_build("sp16", audio="file")
        self.add_entry(self.update_sha(), selected=True)
        self.ctx.dry_run = True
        code, out, _ = self.bootstrap("--audio", "file", "--update", str(self.upd), "--no-run")
        self.assertEqual(code, 0)
        self.assertIn("already built with the same audio", out)
        self.assertIn("already in the library", out)
        self.assertIn("--no-run", out)
        self.assertNotIn("native.py", out)

    def test_a_dry_run_of_a_download_makes_no_request(self):
        server = FakeServer(b"")
        self.ctx.open_url = server
        self.ctx.dry_run = True
        code, out, _ = self.bootstrap("--download-firmware", "1.60", "--key-file", str(self.key))
        self.assertEqual(code, 0)
        self.assertEqual(server.calls, [])
        self.assertIn("would download official firmware 1.60 from downloads.support.alphatheta.com", out)


class StickMissingTests(UsbCase):
    def test_only_absent_or_different_files_are_returned(self):
        source = self.samples / "src"
        source.mkdir()
        (source / "same.wav").write_bytes(b"12345")
        (source / "other.wav").write_bytes(b"12345")
        (source / "new.wav").write_bytes(b"1")
        (source / "p.prj").write_bytes(b"ab")
        point = self.root / "mnt"
        top = point / "PIONEER DJ SAMPLER"
        (top / "Samples" / "PACK").mkdir(parents=True)
        (top / "Projects").mkdir()
        (top / "Samples" / "PACK" / "same.wav").write_bytes(b"12345")
        (top / "Samples" / "PACK" / "OTHER.wav").write_bytes(b"123")        # another size, other case
        (top / "Samples" / "PACK" / "._same.wav").write_bytes(b"junk")
        (top / "Projects" / "p.prj").write_bytes(b"ab")
        groups = [([(source / "same.wav", Path("PACK/same.wav")), (source / "other.wav", Path("PACK/other.wav")),
                    (source / "new.wav", Path("PACK/new.wav"))], "Samples"),
                  ([(source / "p.prj", Path("p.prj"))], "Projects")]
        self.make_stick()

        class Mounted:
            def __enter__(inner):
                return point

            def __exit__(inner, *exc):
                return False
        with mock.patch.object(cli, "mounted_stick", return_value=Mounted()) as mounted:
            left = cli.stick_missing(self.ctx, self.stick(), groups)
        self.assertEqual(mounted.call_args.kwargs, {"writable": False})
        self.assertEqual([[r.as_posix() for _, r in items] for items, _ in left], [["PACK/other.wav", "PACK/new.wav"]])
        self.assertEqual([folder for _, folder in left], ["Samples"])


class MakefileBootstrapTests(unittest.TestCase):
    def make(self, *args):
        from test_sp16_cli import MAKEFILE
        import subprocess
        result = subprocess.run(["make", "-C", str(MAKEFILE.parent), *args], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
        return result.returncode, result.stdout, result.stderr

    def test_the_target_expands_to_the_command(self):
        code, out, err = self.make("-n", "bootstrap", "UPD=/p a/SP16.UPD", "GPL=one.zip two.zip", "SAMPLES=/s/one /s/two",
                                   "PROJECTS=/p/proj.zip")
        self.assertEqual(code, 0, err)
        self.assertEqual(shlex.split(out)[2:], [
            "bootstrap", "--name", "sp16", "--audio", "coreaudio", "--update", "/p a/SP16.UPD", "--gpl-archive",
            "one.zip", "two.zip", "--samples", "/s/one", "--samples", "/s/two", "--projects", "/p/proj.zip"])

    def test_audio_download_and_the_options_pass_through(self):
        code, out, err = self.make("-n", "bootstrap", "VERSION=1.60", "KEY=k.key", "AUDIO=file", "AUDIO_DEVICE=U1",
                                   "NAME=mine", "YES=1", "REBUILD=1", "ARGS=--no-run", "SAMPLES=a")
        self.assertEqual(code, 0, err)
        self.assertEqual(shlex.split(out)[2:], [
            "bootstrap", "--name", "mine", "--audio", "file", "--audio-device", "U1", "--rebuild", "--download-firmware",
            "1.60", "--key-file", "k.key", "--yes", "--samples", "a", "--no-run"])

    def test_missing_or_contradicting_variables_fail_early(self):
        code, _, err = self.make("-n", "bootstrap", "UPD=x")
        self.assertNotEqual(code, 0)
        self.assertIn("make bootstrap needs KEY=<your key file> or GPL=", err)
        code, _, err = self.make("-n", "bootstrap", "UPD=x", "VERSION=1.60", "KEY=k")
        self.assertNotEqual(code, 0)
        self.assertIn("not both", err)
        code, _, err = self.make("-n", "bootstrap", "VERSION=1.60")
        self.assertNotEqual(code, 0)
        self.assertIn("KEY=", err)

    def test_nothing_is_required_to_top_up_an_existing_setup(self):
        code, out, err = self.make("-n", "bootstrap", "SAMPLES=/s/more")
        self.assertEqual(code, 0, err)
        self.assertEqual(shlex.split(out)[2:], ["bootstrap", "--name", "sp16", "--audio", "coreaudio", "--samples", "/s/more"])

    def test_a_version_in_the_environment_does_not_start_a_download(self):
        import subprocess
        from test_sp16_cli import MAKEFILE
        result = subprocess.run(["make", "-C", str(MAKEFILE.parent), "-n", "bootstrap"], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "HOME": "/nonexistent", "VERSION": "1.60"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("download", result.stdout)


if __name__ == "__main__":
    unittest.main()
