"""The Daedalus and Melete bridge tokens: made owner-only, handed to each bridge and to
Pionir by the launcher, read by Pionir's config, and checked by doctor.

Daedalus (Tech-Support/daedalus/daedalus/config.py) reads DAEDALUS_TOKEN (falling back to
MELETE_TOKEN); Melete (melete/config.py) reads MELETE_TOKEN; both then want
``Authorization: Bearer <token>`` on each job call, and neither checks anything when the
variable is unset. Every probe here goes to a fake opener - nothing reaches a live bridge.
"""
import io
import json
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
        shout = [{"bridge": "Melete", "port": 8770, "why": "x"}]
        with patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}), \
                patch.object(bridge_auth, "open_bridges", return_value=shout) as seen, \
                patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(cli.main(["bridge-tokens"]), 0)
        self.assertIn('"made"', out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["open_bridges"], shout)
        self.assertEqual(seen.call_args.args[0],
                         {"daedalus-token.txt": "made", "melete-token.txt": "made"})
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
        # each in its pane's restart loop (scripts/pane-loop.ps1), told its port
        self.assertIn('Pane-Cmd "Daedalus :8771" $daedalusDir "python -m daedalus.server" '
                      '$daedalusEnv 8771)', self.text)
        self.assertIn('Pane-Cmd "Melete :8770" $meleteDir "python -m melete.server" '
                      '$meleteEnv 8770)', self.text)

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
        for sid, port in (("daedalus", "8771"), ("melete", "8770")):
            self.assertRegex(self.text, r"if \(-not \$bridgeTokensOk\) \{ \}\s+elseif "
                                        rf"\(\(Claim-Port '{sid}' {port} [^)]*\) -ne 'free'\)")

    def test_a_bridge_already_up_without_its_token_is_shouted_about(self) -> None:
        block = self.text[self.text.index("$tokenOut = & python -m pionir bridge-tokens"):
                          self.text.index("$pionirPrelude = ")]
        self.assertIn("ConvertFrom-Json", block)
        self.assertIn("foreach ($open in @($tokenReport.open_bridges))", block)
        self.assertRegex(block, r"WITHOUT its token.*-ForegroundColor Red")
        self.assertIn("run 'pionir doctor'", block)            # an unreadable report says so

    def _run_prelude(self, name: str, home: Path) -> subprocess.CompletedProcess:
        prelude = _assignment(self.text, name).replace("`$", "$")
        env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
        env.pop("DAEDALUS_TOKEN", None)
        env.pop("MELETE_TOKEN", None)
        return subprocess.run(["powershell", "-NoProfile", "-Command",
                               prelude + "Write-Output ('STARTED ' + $env:DAEDALUS_TOKEN.Length"
                               " + ' ' + $env:MELETE_TOKEN.Length)"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)

    @unittest.skipUnless(os.name == "nt", "runs the pane's PowerShell prelude")
    def test_a_bridge_pane_without_a_usable_token_exits_before_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            secrets = Path(home) / ".pionir" / "secrets"
            secrets.mkdir(parents=True)
            for name, bridge in (("daedalusEnv", "Daedalus"), ("meleteEnv", "Melete")):
                done = self._run_prelude(name, Path(home))        # no token file at all
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertNotIn("STARTED", done.stdout)
                self.assertIn(f"not starting {bridge} without its token", done.stdout)
            for file in bridge_auth.TOKENS:
                (secrets / file).write_text("short", encoding="utf-8")
            done = self._run_prelude("meleteEnv", Path(home))     # too short to guard it
            self.assertEqual(done.returncode, 1)
            self.assertNotIn("STARTED", done.stdout)
            for file in bridge_auth.TOKENS:
                (secrets / file).write_text(FAKE + "\r\n", encoding="utf-8")
            self.assertIn("STARTED 43 0", self._run_prelude("daedalusEnv", Path(home)).stdout)
            self.assertIn("STARTED 0 43", self._run_prelude("meleteEnv", Path(home)).stdout)

    def test_the_names_are_the_ones_the_bridges_and_pionir_read(self) -> None:
        config = (ROOT / "src" / "pionir" / "config.py").read_text(encoding="utf-8")
        for name in ("PIONIR_DAEDALUS_TOKEN", "PIONIR_MELETE_TOKEN"):
            self.assertIn(f'os.environ.get("{name}")', config)


class _Answer:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _opener(unauth, authed=404, melete_up=True):
    """A fake bridge: a cancel without a token gets ``unauth``, with one ``authed`` (a code,
    200, or an exception); GET /health answers while ``melete_up``."""
    seen = []

    def open_(request, timeout=None):
        seen.append(request)
        url = request.full_url
        if "/jobs/" in url:
            answer = authed if request.get_header("Authorization") else unauth
            if isinstance(answer, BaseException):
                raise answer
            if answer == 200:
                return _Answer()
            raise urllib.error.HTTPError(url, answer, "x", {}, None)
        if url.endswith("/health"):
            if melete_up:
                return _Answer()
            raise urllib.error.URLError("refused")
        raise AssertionError(url)
    return open_, seen


def _settings(**overrides) -> PionirSettings:
    values = {"daedalus_url": "http://127.0.0.1:18771", "melete_url": "http://127.0.0.1:18770",
              "daedalus_token": FAKE, "melete_token": FAKE}
    values.update(overrides)
    return PionirSettings(**values)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "melete-token.txt").write_text(FAKE, encoding="utf-8")
        self.made = (self.dir / "melete-token.txt").stat().st_mtime
        self.after = lambda port: self.made + 60          # Melete started after its token
        self.before = lambda port: self.made - 60         # ... or had run since before it

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def report(self, settings, open_, started=None):
        return bridge_auth.bridge_report(settings, opener=open_, started=started or self.after,
                                         directory=self.dir)

    def test_a_daedalus_that_takes_pionirs_token_and_refuses_others_is_fine(self) -> None:
        open_, seen = _opener(401, 404)
        report = self.report(_settings(), open_)
        self.assertEqual(report["daedalus"]["status"], "token_required")
        self.assertNotIn("warning", report["daedalus"])
        bare, bearing = [r for r in seen if "/jobs/" in r.full_url]
        for probe in (bare, bearing):
            self.assertEqual(probe.get_method(), "POST")
            self.assertEqual(probe.full_url,
                             "http://127.0.0.1:18771/jobs/pionir-auth-probe/cancel")
        self.assertIsNone(bare.get_header("Authorization"))      # first WITHOUT a token
        self.assertEqual(bearing.get_header("Authorization"), f"Bearer {FAKE}")

    def test_a_daedalus_started_with_another_token_is_red(self) -> None:
        open_, _ = _opener(401, 401)
        report = self.report(_settings(), open_)
        self.assertEqual(report["daedalus"]["status"], "token_mismatch")
        self.assertIn("refuses Pionir's token", report["daedalus"]["warning"])

    def test_a_locked_daedalus_pionir_holds_no_token_for_is_red(self) -> None:
        open_, seen = _opener(401, 404)
        report = self.report(_settings(daedalus_token=None), open_)
        self.assertEqual(report["daedalus"]["status"], "no_token_held")
        self.assertIn("Pionir holds none", report["daedalus"]["warning"])

    def test_an_answer_that_says_neither_is_reported_not_passed(self) -> None:
        for authed in (500, 200, urllib.error.URLError("gone")):
            open_, _ = _opener(401, authed)
            entry = self.report(_settings(), open_)["daedalus"]
            self.assertEqual(entry["status"], "token_unverified", authed)
            self.assertNotIn("warning", entry)

    def test_a_daedalus_that_answers_a_job_call_without_a_token_is_warned_about(self) -> None:
        for code in (404, 200, 409, 422):
            open_, _ = _opener(code)
            report = self.report(_settings(), open_)
            self.assertEqual(report["daedalus"]["status"], "open", code)
            self.assertIn("WITHOUT a token", report["daedalus"]["warning"])

    def test_a_daedalus_that_is_not_running_is_not_warned_about(self) -> None:
        open_, _ = _opener(urllib.error.URLError("refused"))
        report = self.report(_settings(), open_)
        self.assertEqual(report["daedalus"]["status"], "not_running")
        self.assertNotIn("warning", report["daedalus"])

    def test_a_melete_older_than_its_token_is_red_even_when_pionir_holds_it(self) -> None:
        # The defect: Pionir's config falls back to the token file, so "Pionir holds no
        # Melete token" never fires once the file exists - while a Melete started before
        # the file was made still takes jobs from anyone.
        open_, seen = _opener(401)
        report = self.report(_settings(), open_, started=self.before)
        self.assertEqual(report["melete"]["status"], "open")
        self.assertIn("since before its token was made", report["melete"]["warning"])
        melete_calls = [r for r in seen if "18770" in r.full_url]
        self.assertEqual([(r.get_method(), r.full_url) for r in melete_calls],
                         [("GET", "http://127.0.0.1:18770/health")])   # never sent a job

    def test_a_melete_started_after_its_token_is_fine(self) -> None:
        ports = []
        open_, _ = _opener(401)
        entry = self.report(_settings(), open_,
                            started=lambda port: ports.append(port) or self.made + 60)["melete"]
        self.assertEqual(entry["status"], "started_after_token")
        self.assertNotIn("warning", entry)
        self.assertEqual(ports, [18770])                     # the port of Pionir's Melete URL

    def test_a_melete_that_cannot_be_told_about_is_red(self) -> None:
        open_, _ = _opener(401)
        entry = self.report(_settings(), open_, started=lambda port: None)["melete"]
        self.assertEqual(entry["status"], "unverified")
        self.assertIn("cannot be told", entry["warning"])
        (self.dir / "melete-token.txt").unlink()             # no token file to compare with
        entry = self.report(_settings(), open_)["melete"]
        self.assertEqual(entry["status"], "unverified")

    def test_a_melete_pionir_holds_no_token_for_is_red(self) -> None:
        open_, _ = _opener(401)
        entry = self.report(_settings(melete_token=None), open_)["melete"]
        self.assertEqual(entry["status"], "no_token_held")
        self.assertIn("no Melete token", entry["warning"])

    def test_a_melete_that_is_not_running_is_not_warned_about(self) -> None:
        open_, _ = _opener(401, melete_up=False)
        entry = self.report(_settings(), open_, started=self.before)["melete"]
        self.assertEqual(entry["status"], "not_running")
        self.assertNotIn("warning", entry)

    def test_a_bridge_that_is_switched_off_is_not_probed(self) -> None:
        open_, seen = _opener(404)
        self.assertEqual(self.report(_settings(daedalus_url=None, melete_url=None), open_), {})
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


class OpenBridgeTests(unittest.TestCase):
    """What ``pionir bridge-tokens`` tells the launcher to shout about."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for name in bridge_auth.TOKENS:
            (self.dir / name).write_text(FAKE, encoding="utf-8")
        self.made = (self.dir / "melete-token.txt").stat().st_mtime

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_token_made_while_its_bridge_listens_is_reported(self) -> None:
        out = bridge_auth.open_bridges(
            {"daedalus-token.txt": "made", "melete-token.txt": "kept"}, self.dir,
            started=lambda port: None, listening=lambda port: port == 8771)
        self.assertEqual(out, [{"bridge": "Daedalus", "port": 8771,
                                "why": "its token was just made, after it started"}])

    def test_a_bridge_older_than_its_kept_token_is_reported(self) -> None:
        out = bridge_auth.open_bridges(
            {"daedalus-token.txt": "kept", "melete-token.txt": "kept"}, self.dir,
            started=lambda port: self.made - 60 if port == 8770 else self.made + 60,
            listening=lambda port: True)
        self.assertEqual([(o["bridge"], o["port"]) for o in out], [("Melete", 8770)])

    def test_nothing_is_reported_for_bridges_started_with_their_tokens(self) -> None:
        self.assertEqual(bridge_auth.open_bridges(
            {"daedalus-token.txt": "made", "melete-token.txt": "made"}, self.dir,
            started=lambda port: self.made + 60, listening=lambda port: False), [])
        self.assertEqual(bridge_auth.open_bridges(
            {"daedalus-token.txt": "kept", "melete-token.txt": "kept"}, self.dir,
            started=lambda port: None, listening=lambda port: True), [])

    @unittest.skipUnless(os.name == "nt", "the Windows TCP table")
    def test_the_listener_start_time_is_read_from_the_tcp_table(self) -> None:
        import socket
        import time
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            began = bridge_auth.listener_started(port)
        self.assertIsNotNone(began)                           # this test process
        self.assertLess(began, time.time())
        self.assertGreater(began, time.time() - 3600)
        self.assertIsNone(bridge_auth.listener_started(port))  # closed: nothing listens


if __name__ == "__main__":
    unittest.main()
