"""The Daedalus and Melete bridge tokens: made owner-only, handed to each bridge and to
Pionir by the launcher, read by Pionir's config, and checked by doctor.

Daedalus (Tech-Support/daedalus/daedalus/config.py) reads DAEDALUS_TOKEN (falling back to
MELETE_TOKEN); Melete (melete/config.py) reads MELETE_TOKEN; both then want
``Authorization: Bearer <token>`` on each job call, and neither checks anything when the
variable is unset. Every probe here goes to a fake opener - nothing reaches a live bridge.
"""
import io
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pionir import bridge_auth, cli
from pionir.config import PionirSettings

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "pionir.ps1"
FAKE = "f" * 20 + "a" * 23          # 43 url-safe characters, built from pieces


def _acl(path: Path) -> list[str]:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    out = subprocess.run([str(system32 / "icacls.exe"), str(path)], capture_output=True,
                         text=True, errors="replace", check=True).stdout
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    lines[0] = lines[0][len(str(path)):].strip()
    return [line for line in lines if not line.startswith("Successfully processed")]


class TokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "secrets"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_tokens_are_made_random_and_url_safe(self) -> None:
        made = bridge_auth.ensure_tokens(self.dir)
        self.assertEqual(made, {"daedalus-token.txt": "made", "melete-token.txt": "made"})
        daedalus = (self.dir / "daedalus-token.txt").read_text(encoding="utf-8")
        melete = (self.dir / "melete-token.txt").read_text(encoding="utf-8")
        for token in (daedalus, melete):
            self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")      # 32 bytes, base64url
        self.assertNotEqual(daedalus, melete)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["daedalus-token.txt", "melete-token.txt"])   # no temp left

    def test_an_existing_token_is_never_overwritten(self) -> None:
        bridge_auth.ensure_tokens(self.dir)
        before = (self.dir / "daedalus-token.txt").read_text(encoding="utf-8")
        again = bridge_auth.ensure_tokens(self.dir)
        self.assertEqual(again, {"daedalus-token.txt": "kept", "melete-token.txt": "kept"})
        self.assertEqual((self.dir / "daedalus-token.txt").read_text(encoding="utf-8"), before)

    def test_only_the_missing_one_is_made(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "melete-token.txt").write_text(FAKE, encoding="utf-8")
        made = bridge_auth.ensure_tokens(self.dir)
        self.assertEqual(made, {"daedalus-token.txt": "made", "melete-token.txt": "kept"})
        self.assertEqual((self.dir / "melete-token.txt").read_text(encoding="utf-8"), FAKE)

    def test_an_unusable_token_file_is_refused_not_replaced(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "daedalus-token.txt").write_text("short", encoding="utf-8")
        with self.assertRaisesRegex(OSError, "no usable token"):
            bridge_auth.ensure_tokens(self.dir)
        self.assertEqual((self.dir / "daedalus-token.txt").read_text(encoding="utf-8"),
                         "short")

    def test_a_file_that_cannot_be_restricted_is_not_left_behind(self) -> None:
        with patch.object(bridge_auth, "owner_only", side_effect=OSError("no acl")):
            with self.assertRaisesRegex(OSError, "no acl"):
                bridge_auth.ensure_tokens(self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_another_launcher_making_it_first_is_kept(self) -> None:
        self.dir.mkdir(parents=True)
        real = bridge_auth.owner_only

        def racing(path: Path) -> None:
            real(path)
            target = self.dir / "daedalus-token.txt"
            if not target.exists():
                target.write_text(FAKE, encoding="utf-8")

        with patch.object(bridge_auth, "owner_only", side_effect=racing):
            made = bridge_auth.ensure_tokens(self.dir)
        self.assertEqual(made["daedalus-token.txt"], "kept")
        self.assertEqual((self.dir / "daedalus-token.txt").read_text(encoding="utf-8"), FAKE)

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_the_file_is_owner_only_with_nothing_inherited(self) -> None:
        bridge_auth.ensure_tokens(self.dir)
        sid = bridge_auth._owner_sid()
        self.assertRegex(sid or "", r"^S-1-5-")
        for name in bridge_auth.TOKENS:
            aces = _acl(self.dir / name)
            self.assertEqual(len(aces), 1, aces)
            self.assertNotIn("(I)", aces[0])
            self.assertTrue(aces[0].endswith(":(F)"), aces)
            owner = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Acl -LiteralPath '{self.dir / name}').Access | "
                 "ForEach-Object { $_.IdentityReference.Translate("
                 "[System.Security.Principal.SecurityIdentifier]).Value }"],
                capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(owner, [sid])

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_the_file_is_owner_only_on_posix(self) -> None:
        bridge_auth.ensure_tokens(self.dir)
        for name in bridge_auth.TOKENS:
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)

    def test_read_token_strips_and_refuses_short_or_missing(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "a.txt").write_text("\ufeff" + FAKE + "\r\n", encoding="utf-8")
        (self.dir / "b.txt").write_text("x" * 31, encoding="utf-8")
        self.assertEqual(bridge_auth.read_token("a.txt", self.dir), FAKE)
        self.assertIsNone(bridge_auth.read_token("b.txt", self.dir))
        self.assertIsNone(bridge_auth.read_token("c.txt", self.dir))

    def test_the_cli_command_makes_them_in_the_owners_secrets_folder(self) -> None:
        home = Path(self._tmp.name)
        with patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}), \
                patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(cli.main(["bridge-tokens"]), 0)
        self.assertIn('"made"', out.getvalue())
        self.assertNotIn(bridge_auth.read_token("daedalus-token.txt",
                                                home / ".pionir" / "secrets") or "?",
                         out.getvalue())                     # a token is never printed
        for name in bridge_auth.TOKENS:
            self.assertIsNotNone(bridge_auth.read_token(name, home / ".pionir" / "secrets"))

    def test_the_cli_command_fails_when_they_cannot_be_made(self) -> None:
        with patch.object(bridge_auth, "ensure_tokens", side_effect=OSError("denied")), \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cli.main(["bridge-tokens"]), 1)


class ConfigFallbackTests(unittest.TestCase):
    def test_tokens_come_from_the_secrets_folder_when_not_in_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            secrets = Path(home) / ".pionir" / "secrets"
            secrets.mkdir(parents=True)
            (secrets / "daedalus-token.txt").write_text(FAKE + "\n", encoding="utf-8")
            (secrets / "melete-token.txt").write_text(FAKE[::-1], encoding="utf-8")
            base = {"USERPROFILE": home, "HOME": home, "PIONIR_STATE_ROOT": home}
            with patch.dict(os.environ, base, clear=True):
                settings = PionirSettings.from_environment()
            self.assertEqual(settings.daedalus_token, FAKE)
            self.assertEqual(settings.melete_token, FAKE[::-1])
            self.assertNotIn(FAKE, repr(settings))
            (secrets / "melete-token.txt").write_text("too-short", encoding="utf-8")
            with patch.dict(os.environ, base, clear=True):
                self.assertIsNone(PionirSettings.from_environment().melete_token)
            with patch.dict(os.environ, {**base, "PIONIR_DAEDALUS_TOKEN": "e" * 40,
                                         "PIONIR_MELETE_TOKEN": "m" * 40}, clear=True):
                settings = PionirSettings.from_environment()
            self.assertEqual(settings.daedalus_token, "e" * 40)     # the environment wins
            self.assertEqual(settings.melete_token, "m" * 40)


def _assignment(text: str, name: str) -> str:
    found = re.findall(rf'^\s*\${name}\s*=\s*"(.*)"\s*$', text, flags=re.M)
    assert len(found) == 1, (name, found)
    return found[0]


def _token_env(prelude: str) -> dict[str, str]:
    return dict(re.findall(r"`\$env:(\w+)=\(Get-Content -Raw \(Join-Path `\$HOME "
                           r"'\.pionir\\secrets\\([\w.-]+)'\)\)\.Trim\(\); ", prelude))


class LauncherTests(unittest.TestCase):
    """Parses pionir.ps1 the way Pionir Desktop's drift test does."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = LAUNCHER.read_text(encoding="utf-8")

    def test_each_bridge_is_started_with_its_own_token(self) -> None:
        self.assertEqual(_token_env(_assignment(self.text, "daedalusEnv")),
                         {"DAEDALUS_TOKEN": "daedalus-token.txt"})
        self.assertEqual(_token_env(_assignment(self.text, "meleteEnv")),
                         {"MELETE_TOKEN": "melete-token.txt"})
        self.assertIn('Pane-Cmd "Daedalus :8771" $daedalusDir "python -m daedalus.server" '
                      '$daedalusEnv)', self.text)
        self.assertIn('Pane-Cmd "Melete :8770" $meleteDir "python -m melete.server" '
                      '$meleteEnv)', self.text)

    def test_pionir_is_handed_both_tokens(self) -> None:
        self.assertEqual(_token_env(_assignment(self.text, "pionirPrelude")),
                         {"PIONIR_DAEDALUS_TOKEN": "daedalus-token.txt",
                          "PIONIR_MELETE_TOKEN": "melete-token.txt"})
        self.assertIn('"python -m pionir server --port $Port$browserFlag" $pionirPrelude',
                      self.text)

    def test_the_tokens_are_made_before_any_pane_and_gate_the_bridges(self) -> None:
        made = self.text.index("& python -m pionir bridge-tokens")
        self.assertLess(made, self.text.index("$panes += "))
        self.assertLess(self.text.index("if ($Stop)"), made)     # -Stop makes nothing
        self.assertIn("$bridgeTokensOk = ($LASTEXITCODE -eq 0)", self.text)
        for port in ("8771", "8770"):
            self.assertRegex(self.text, r"if \(-not \$bridgeTokensOk\) \{ \}\s+elseif "
                                        rf"\(Test-Port {port}\)")

    def test_the_names_are_the_ones_the_bridges_and_pionir_read(self) -> None:
        config = (ROOT / "src" / "pionir" / "config.py").read_text(encoding="utf-8")
        for name in ("PIONIR_DAEDALUS_TOKEN", "PIONIR_MELETE_TOKEN"):
            self.assertIn(f'os.environ.get("{name}")', config)


class _Answer:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _opener(daedalus, melete_up=True):
    seen = []

    def open_(request, timeout=None):
        seen.append(request)
        url = request.full_url
        if "/jobs/" in url:
            if isinstance(daedalus, BaseException):
                raise daedalus
            if daedalus == 200:
                return _Answer()
            raise urllib.error.HTTPError(url, daedalus, "x", {}, None)
        if url.endswith("/health"):
            if melete_up:
                return _Answer()
            raise urllib.error.URLError("refused")
        raise AssertionError(url)
    return open_, seen


def _settings(**overrides) -> PionirSettings:
    values = {"daedalus_url": "http://127.0.0.1:18771", "melete_url": "http://127.0.0.1:18770",
              "daedalus_token": None, "melete_token": None}
    values.update(overrides)
    return PionirSettings(**values)


class DoctorTests(unittest.TestCase):
    def test_a_daedalus_that_refuses_without_a_token_is_fine(self) -> None:
        open_, seen = _opener(401)
        report = bridge_auth.bridge_report(_settings(daedalus_token=FAKE, melete_token=FAKE),
                                           opener=open_)
        self.assertEqual(report["daedalus"]["status"], "token_required")
        self.assertNotIn("warning", report["daedalus"])
        probe = seen[0]
        self.assertEqual(probe.get_method(), "POST")
        self.assertEqual(probe.full_url, "http://127.0.0.1:18771/jobs/pionir-auth-probe/cancel")
        self.assertIsNone(probe.get_header("Authorization"))     # asked WITHOUT a token

    def test_a_daedalus_that_answers_a_job_call_without_a_token_is_warned_about(self) -> None:
        for code in (404, 200, 409, 422):
            open_, _ = _opener(code)
            report = bridge_auth.bridge_report(_settings(melete_token=FAKE), opener=open_)
            self.assertEqual(report["daedalus"]["status"], "open", code)
            self.assertIn("WITHOUT a token", report["daedalus"]["warning"])

    def test_a_daedalus_that_is_not_running_is_not_warned_about(self) -> None:
        open_, _ = _opener(urllib.error.URLError("refused"))
        report = bridge_auth.bridge_report(_settings(melete_token=FAKE), opener=open_)
        self.assertEqual(report["daedalus"]["status"], "not_running")
        self.assertNotIn("warning", report["daedalus"])

    def test_melete_is_never_sent_a_job_and_is_warned_about_without_a_token(self) -> None:
        open_, seen = _opener(401)
        report = bridge_auth.bridge_report(_settings(), opener=open_)
        self.assertEqual(report["melete"]["status"], "not_probed")
        self.assertIn("no Melete token", report["melete"]["warning"])
        melete_calls = [r for r in seen if "18770" in r.full_url]
        self.assertEqual([(r.get_method(), r.full_url) for r in melete_calls],
                         [("GET", "http://127.0.0.1:18770/health")])
        open_, _ = _opener(401)
        held = bridge_auth.bridge_report(_settings(melete_token=FAKE), opener=open_)
        self.assertNotIn("warning", held["melete"])
        open_, _ = _opener(401, melete_up=False)
        down = bridge_auth.bridge_report(_settings(), opener=open_)
        self.assertEqual(down["melete"]["status"], "not_running")
        self.assertNotIn("warning", down["melete"])

    def test_a_bridge_that_is_switched_off_is_not_probed(self) -> None:
        open_, seen = _opener(404)
        self.assertEqual(bridge_auth.bridge_report(_settings(daedalus_url=None,
                                                             melete_url=None), opener=open_),
                         {})
        self.assertEqual(seen, [])

    def test_doctor_fails_when_a_bridge_is_open(self) -> None:
        healthy = {"specialists": {}, "routing_aim": {"status": "ok"},
                   "bridge_auth": {"daedalus": {"status": "token_required"}}}
        open_bridge = {**healthy, "bridge_auth": {"daedalus": {"status": "open",
                                                               "warning": "open"}}}
        args = cli._parser().parse_args(["doctor"])
        for report, code in ((healthy, 0), (open_bridge, 1)):
            with patch.object(cli, "_doctor", return_value=report), \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(cli._execute(args, runtime=None), code)


if __name__ == "__main__":
    unittest.main()
