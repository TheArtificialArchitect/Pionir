"""pionir.ps1 panes restart a bridge that dies on its own - visibly, with backoff, bounded.

scripts\\pane-loop.ps1 is run for real (PowerShell 5.1) on a throwaway python command, with
a temp log, a temp stop marker and tiny waits: no live pane, process or port of the stack
is touched. Each test fails if its rule is reverted: a crash not restarted, a restart after
a deliberate exit (0, a listed refusal, pionir.ps1 -Stop), a restart over a port something
else already serves, or a crash loop that never gives up.
"""
from __future__ import annotations

import base64
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOP = ROOT / "scripts" / "pane-loop.ps1"
PS1 = ROOT / "pionir.ps1"


@unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
class PaneLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)
        self.runs = self.dir / "runs.txt"
        self.log = self.dir / "launcher.log"
        self.marker = self.dir / "stopping"

    def tearDown(self) -> None:
        self._t.cleanup()

    def command(self, code: int, *, marker: bool = False) -> str:
        # each run appends a line, then exits with ``code`` (and may drop the stop marker,
        # as pionir.ps1 -Stop does before it kills the bridge)
        body = (f"open(r'{self.runs}', 'a').write('run\\n'); "
                + (f"open(r'{self.marker}', 'w').write('x'); " if marker else "")
                + f"import ctypes; ctypes.windll.kernel32.ExitProcess({code})")
        return f'& "{sys.executable}" -c "{body}"'

    def loop(self, command: str, *extra: str, timeout: float = 120) -> subprocess.CompletedProcess:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(LOOP), "-Title", "Test pane", "-Command", command,
                "-StopMarker", str(self.marker), "-LogFile", str(self.log),
                "-FirstDelaySeconds", "0.05", "-MaxDelaySeconds", "0.2", "-NoPause", *extra]
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, check=False)

    def count(self) -> int:
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def test_a_crash_is_restarted_with_backoff_and_gives_up_loudly(self) -> None:
        done = self.loop(self.command(3), "-MaxCrashes", "3")
        self.assertEqual(self.count(), 3)
        self.assertEqual(done.returncode, 1)
        self.assertIn("CRASHED 3 TIMES", done.stdout)
        self.assertIn("Restarting in", done.stdout)
        log = self.log.read_text(encoding="utf-8-sig")
        self.assertIn("pane 'Test pane': exit 3", log)
        self.assertIn("GAVE UP", log)

    def test_an_exit_zero_is_deliberate_and_not_restarted(self) -> None:
        done = self.loop(self.command(0))
        self.assertEqual(self.count(), 1)
        self.assertEqual(done.returncode, 0)
        self.assertIn("not restarted", self.log.read_text(encoding="utf-8-sig"))

    def test_ctrl_c_is_deliberate_and_not_restarted(self) -> None:
        self.loop(self.command(0xC000013A))   # STATUS_CONTROL_C_EXIT
        self.assertEqual(self.count(), 1)

    def test_pionir_stop_closes_the_pane_without_a_restart(self) -> None:
        done = self.loop(self.command(1, marker=True))
        self.assertEqual(self.count(), 1)
        self.assertEqual(done.returncode, 0)
        self.assertIn("-Stop", self.log.read_text(encoding="utf-8-sig"))

    def test_a_listed_refusal_is_not_restarted(self) -> None:
        self.loop(self.command(2), "-NoRestartCodes", "2")
        self.assertEqual(self.count(), 1)

    def test_no_second_copy_over_a_port_that_already_answers(self) -> None:
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(8)
            port = srv.getsockname()[1]
            done = self.loop(self.command(3), "-Port", str(port))
        self.assertEqual(self.count(), 1)
        self.assertEqual(done.returncode, 0)
        self.assertIn(f"port {port} answers already", self.log.read_text(encoding="utf-8-sig"))


class LauncherWiringTests(unittest.TestCase):
    def test_every_bridge_pane_runs_inside_the_restart_loop_with_its_port(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        body = re.search(r"function Pane-Cmd[\s\S]*?\n\}", text).group(0)
        self.assertIn("$paneLoop", body)
        self.assertIn("-Port $port", body)
        self.assertIn("-StopMarker", body)
        # the pane marker stays first: Close-EmptyPanes and Test-PionirPane find panes by it
        self.assertIn('$body = "`$env:PIONIR_PANE=\'1\';', body)
        for call, port in (('"python -m pionir.crew" $crewPrelude', "8782"),
                           ('"python -m galatea wake --port 8799 --no-browser --phone" ""', "8799"),
                           ('"python -m daedalus.server" $daedalusEnv', "8771"),
                           ('"python -m melete.server" $meleteEnv', "8770"),
                           ('$pionirPrelude', "$Port")):
            self.assertIn(f"{call} {port})", text)
        self.assertIn('-File $tunnelScript" "" 0 "2")', text)  # "no key yet" is a refusal

    @unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
    def test_the_pane_command_decodes_to_the_marker_then_the_loop(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        lift = text[text.index("function Enc("):text.index("function Close-EmptyPanes")]
        script = ("$root = 'C:\\src\\Pionir'\n" + lift +
                  "\n(Pane-Cmd \"Crew :8782\" 'C:\\src\\Pionir' \"python -m pionir.crew\" "
                  "\"`$env:X='1'; \" 8782)[-1]\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lift.ps1"
            path.write_text(script, encoding="utf-8-sig")
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                   "-ExecutionPolicy", "Bypass", "-File", str(path)],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        decoded = base64.b64decode(done.stdout.strip().splitlines()[-1]).decode("utf-16-le")
        self.assertTrue(decoded.startswith("$env:PIONIR_PANE='1';"), decoded)
        self.assertIn("$env:X='1'; & 'C:\\src\\Pionir\\scripts\\pane-loop.ps1'", decoded)
        self.assertIn("-Command 'python -m pionir.crew' -Port 8782", decoded)


if __name__ == "__main__":
    unittest.main()
