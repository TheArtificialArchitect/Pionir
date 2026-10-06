"""pionir.ps1 and Proteus: Peter and his relay start with the stack, and the launcher
never stops a process it did not start.

The ownership rule is run for real - the launcher's own PowerShell functions, lifted out
of pionir.ps1 - against SYNTHETIC process lists: no live process is listed or touched.
Peter stopped at 17:46 on 2026-09-28 and the question was whether Pionir killed him; these
pin that it cannot: a Peter started by hand (deploy\\Peter.cmd) or by Pionir Desktop is
never matched as the launcher's own, whatever holds port 8790.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.ollama_gate import GATE_PORT

PS1 = Path(__file__).resolve().parent.parent / "pionir.ps1"
WRAPPER = Path(__file__).resolve().parent.parent / "scripts" / "peter-live.ps1"

PETER_CMD = ('"C:\\py\\python.exe" -m peter.cli --vault C:\\src\\The-Web\\data\\vault.sqlite live '
             '--interval 300 --signals-out C:\\src\\The-Web\\data\\signals.json')

SCENE = r"""
function Enc2([string]$t) { [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($t)) }
function P($id, $parent, $name, $cmd) { [pscustomobject]@{ ProcessId = $id; ParentProcessId = $parent; Name = $name; CommandLine = $cmd } }
$paneCmd = "powershell -ExecutionPolicy Bypass -EncodedCommand " + (Enc2 "`$env:PIONIR_PANE='1'; `$host.UI.RawUI.WindowTitle='Peter :8790'; Set-Location 'C:\src\The-Web'; x")
$otherShell = "powershell -EncodedCommand " + (Enc2 "Write-Host 'someone else'")
$all = @(
  # a Pionir pane: pane shell -> peter-live.ps1 -> python
  (P 10 1 'powershell.exe' $paneCmd),
  (P 11 10 'powershell.exe' 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\src\Pionir\scripts\peter-live.ps1'),
  (P 12 11 'python.exe' '__PETER__'),
  # by hand: Peter.cmd -> powershell -Command ... -> python
  (P 20 2 'cmd.exe' 'C:\WINDOWS\system32\cmd.exe /c ""C:\src\The-Web\deploy\Peter.cmd" "'),
  (P 21 20 'powershell.exe' 'powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path .\deploy\peter-secrets.ps1) { . .\deploy\peter-secrets.ps1 }; .\deploy\peter.ps1 live"'),
  (P 22 21 'python.exe' '__PETER__'),
  # by Pionir Desktop: electron -> peter-live.ps1 -> python
  (P 30 3 'Pionir Desktop.exe' '"C:\Program Files\Pionir Desktop\Pionir Desktop.exe"'),
  (P 31 30 'powershell.exe' 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\src\Pionir\scripts\peter-live.ps1'),
  (P 32 31 'python.exe' '__PETER__'),
  # someone else's -EncodedCommand shell above a Peter: not a Pionir pane
  (P 35 4 'powershell.exe' $otherShell),
  (P 36 35 'python.exe' '__PETER__'),
  # not Peter at all, though it wants 8790: the pantheon relay, and a one-shot peter command
  (P 40 5 'python.exe' 'python relay\server.py --port 8790'),
  (P 41 5 'python.exe' 'python -m peter.cli --vault x signals --out y.json'),
  # the VPS relay: from a pane, and by hand
  (P 50 1 'powershell.exe' $paneCmd),
  (P 51 50 'powershell.exe' 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\src\Mr-Crab\deploy\desktop\peter-vps-relay.ps1'),
  (P 60 6 'powershell.exe' 'powershell -File C:\src\Mr-Crab\deploy\desktop\peter-vps-relay.ps1'),
  # a loop in the parent chain must not hang it
  (P 70 71 'python.exe' '__PETER__'),
  (P 71 70 'powershell.exe' 'powershell')
)
$out = @{
  peter = @(Get-Companions $all $peterMatch | ForEach-Object { @{ pid = $_.Process.ProcessId; ours = $_.Ours } })
  relay = @(Get-Companions $all $relayMatch | ForEach-Object { @{ pid = $_.Process.ProcessId; ours = $_.Ours } })
}
$out | ConvertTo-Json -Depth 4 -Compress
"""


def _launcher_text() -> str:
    return PS1.read_text(encoding="utf-8-sig")


@unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
class OwnershipRuleTests(unittest.TestCase):
    def _run(self) -> dict:
        text = _launcher_text()
        start, end = text.index("$peterMatch = "), text.index("function Stop-Companion")
        script = text[start:end] + SCENE.replace("'__PETER__'", "'" + PETER_CMD + "'")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scene.ps1"
            path.write_text(script, encoding="utf-8-sig")
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                   "Bypass", "-File", str(path)], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_only_what_a_pionir_pane_started_is_the_launchers_own(self) -> None:
        out = self._run()
        peter = {row["pid"]: row["ours"] for row in out["peter"]}
        self.assertEqual(peter, {12: True, 22: False, 32: False, 36: False, 70: False})
        relay = {row["pid"]: row["ours"] for row in out["relay"]}
        self.assertEqual(relay, {51: True, 60: False})


class LauncherShapeTests(unittest.TestCase):
    def test_stop_never_stops_by_peters_port_or_by_image_name(self) -> None:
        text = _launcher_text()
        stop = re.search(r"if \(\$Stop\) \{([\s\S]*?)\n\}", text).group(1)
        self.assertNotIn("8790", stop)
        self.assertIn('Stop-Companion $peterMatch "Peter"', stop)
        self.assertIn('Stop-Companion $relayMatch', stop)
        body = re.search(r"function Stop-Companion[\s\S]*?\n\}", text).group(0)
        self.assertIn("if ($hit.Ours)", body)
        self.assertNotRegex(text, r"Stop-Process\s+-Name")
        self.assertNotRegex(text, r"taskkill(\.exe)?\s+/IM")

    def test_peter_runs_what_peter_cmd_runs_through_the_gate(self) -> None:
        text = _launcher_text()
        self.assertIn('Pane-Cmd "Peter :8790" $peterDir "powershell -NoProfile -ExecutionPolicy '
                      'Bypass -File $peterLive" $peterPrelude', text)
        prelude = re.search(r'\$peterPrelude = "(.*)"', text).group(1)
        # the gate's port, as the server starts it (named, never reached, from here)
        self.assertIn(f"$env:HTTP_PROXY='http://127.0.0.1:{GATE_PORT}'", prelude)
        self.assertIn("feeds.bbci.co.uk", prelude)
        wrapper = WRAPPER.read_text(encoding="utf-8-sig")
        # Peter.cmd: if (Test-Path .\deploy\peter-secrets.ps1) { . ... }; .\deploy\peter.ps1 live
        self.assertIn("if (Test-Path $secrets) { . $secrets }", wrapper)
        self.assertIn("& (Join-Path $here 'deploy\\peter.ps1') live", wrapper)
        self.assertNotRegex(wrapper, r"Write-(Host|Output)[^\n]*\$env:PETER_ALPACA")

    def test_a_running_peter_is_adopted_not_started_twice(self) -> None:
        text = _launcher_text()
        block = text[text.index("if (-not $NoPeter) {"):text.index("# Bryo, the observer organism")]
        self.assertLess(block.index("$peterNow.Count"), block.index('Pane-Cmd "Peter :8790"'))
        self.assertLess(block.index("Claim-Port 'peter' 8790"), block.index('Pane-Cmd "Peter :8790"'))
        self.assertLess(block.index("$relayMatch"), block.index('Pane-Cmd "Peter relay (VPS)"'))


if __name__ == "__main__":
    unittest.main()
