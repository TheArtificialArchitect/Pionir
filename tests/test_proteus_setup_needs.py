"""Setup needs of the trading plane: what only the owner can provide, said as data.

The desktop shows a source that is merely not set up as "setup needed" with one next step,
not as a red outage. For the plane row that needs Pionir's help: a plane that has not read
yet must say WHY (waiting on its keys), so ``/api/proteus`` carries ``setup_needs`` - each a
missing FILE, from an offline existence check (no ssh, no port, no key is read).

Pinned here: every prerequisite is named with its one next step; a present file is not a
need; the check touches no ssh; the view carries the list and survives an adapter that
cannot give one; bootstrap hands the adapter the real secrets folder.
"""

import json
import tempfile
import unittest
from pathlib import Path

from standins import down_url

from pionir.adapters._proc import ProcessResult
from pionir.adapters.proteus import ProteusAdapter, ProteusSettings
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp

TUNNEL_STEP = r"run tools\vps-lockdown.ps1 in C:\src\Pionir"
READ_STEP = r"add %USERPROFILE%\.pionir\secrets" + "\\"


class _NoSsh:
    """A runner that records any reach for the VPS, so a test can say there was none."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        return ProcessResult(returncode=0, stdout="", stderr="")


def _plane(secrets: Path | None, key: Path, runner: _NoSsh | None = None) -> ProteusAdapter:
    return ProteusAdapter(ProteusSettings(host="203.0.113.7", key_file=key),
                          runner=runner or _NoSsh(), peter_health=lambda: True,
                          tunnel_health=lambda port: "down", secrets_dir=secrets)


def _touch(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("x", encoding="utf-8")


def _settings(tmp: str, **over) -> PionirSettings:
    base = {
        "state_root": Path(tmp), "atani_command": ("pionir-test-no-such-binary",),
        "galatea_url": down_url(), "galatea_model_id": "stub-model", "embed_model": None,
        "daedalus_url": down_url(), "melete_url": down_url(), "crew_url": None,
        "bryo_status_command": None, "nyx_status_command": None,
        "voodoo_status_command": None, "evict_to_fit": False, "proteus_host": None}
    base.update(over)
    return PionirSettings(**base)


class SetupNeedsTests(unittest.TestCase):
    def test_a_plane_built_without_a_secrets_folder_looks_at_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_plane(None, Path(tmp) / "no-such-key").setup_needs(), [])

    def test_every_missing_prerequisite_is_named_with_its_one_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "no-such-key"
            needs = _plane(Path(tmp), key).setup_needs()
            self.assertEqual([n["what"] for n in needs], [
                "the VPS tunnel key", "the prometheus read key", "the robinhood read key",
                "the plane's ssh key"])
            self.assertEqual(needs[0]["next_step"], TUNNEL_STEP)
            self.assertEqual(needs[1]["next_step"], READ_STEP + "prometheus-read-key.txt")
            self.assertEqual(needs[2]["next_step"], READ_STEP + "proteus-read-key.txt")
            self.assertIn(str(key), needs[3]["next_step"])
            for need in needs:
                self.assertEqual(set(need), {"what", "next_step"})

    def test_a_file_that_is_there_is_not_a_need(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp)
            key = secrets / "ssh-key"
            _touch(secrets, "vps-tunnel-key", "prometheus-read-key.txt", "ssh-key")
            needs = _plane(secrets, key).setup_needs()
            self.assertEqual([n["what"] for n in needs], ["the robinhood read key"])
            _touch(secrets, "proteus-read-key.txt")
            self.assertEqual(_plane(secrets, key).setup_needs(), [])

    def test_the_check_reaches_for_no_ssh_and_shows_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh = _NoSsh()
            secrets = Path(tmp)
            (secrets / "proteus-read-key.txt").write_text("SECRET-VALUE-123456", encoding="utf-8")
            needs = _plane(secrets, secrets / "nokey", ssh).setup_needs()
            self.assertEqual(ssh.calls, [])
            self.assertNotIn("SECRET-VALUE-123456", json.dumps(needs))


class ViewTests(unittest.TestCase):
    def _app(self, tmp: str, adapter) -> PionirApp:
        runtime = build_runtime(_settings(tmp))
        runtime.register(adapter)
        return PionirApp(runtime)

    def test_the_view_carries_the_needs_before_the_plane_has_read_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            secrets.mkdir()
            app = self._app(tmp, _plane(secrets, secrets / "nokey"))
            try:
                view = app.proteus_view(refresh=lambda work: None, now=lambda: 1.0)
                self.assertIsNone(view["snapshot"])          # nothing read yet
                self.assertEqual(len(view["setup_needs"]), 4)
                self.assertEqual(view["setup_needs"][0]["next_step"], TUNNEL_STEP)
                json.dumps(view)                             # it is the wire
            finally:
                app.runtime.cortex.close()

    def test_no_needs_when_nothing_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            secrets.mkdir()
            _touch(secrets, "vps-tunnel-key", "prometheus-read-key.txt",
                   "proteus-read-key.txt", "ssh-key")
            app = self._app(tmp, _plane(secrets, secrets / "ssh-key"))
            try:
                view = app.proteus_view(refresh=lambda work: None, now=lambda: 1.0)
                self.assertEqual(view["setup_needs"], [])
            finally:
                app.runtime.cortex.close()

    def test_an_adapter_that_cannot_say_gives_none_and_never_breaks_the_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plane = _plane(None, Path(tmp) / "k")

            def boom() -> list:
                raise OSError("the disk went away")

            plane.setup_needs = boom  # type: ignore[method-assign]
            app = self._app(tmp, plane)
            try:
                view = app.proteus_view(refresh=lambda work: None, now=lambda: 1.0)
                self.assertEqual(view["setup_needs"], [])
                self.assertTrue(view["enabled"])
            finally:
                app.runtime.cortex.close()

    def test_a_plane_that_is_not_enabled_is_still_only_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = PionirApp(build_runtime(_settings(tmp)))
            try:
                self.assertEqual(app.proteus_view(refresh=lambda work: None), {"enabled": False})
            finally:
                app.runtime.cortex.close()


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_gives_the_plane_the_real_secrets_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp, proteus_host="203.0.113.7",
                                 proteus_ssh_key=Path(tmp) / "no-such-ssh-key")
            runtime = build_runtime(settings)
            try:
                plane = runtime.adapters["proteus"]
                self.assertEqual(plane._secrets_dir, settings.secrets_path)
                self.assertTrue(plane.setup_needs())     # an empty state root misses every key
            finally:
                runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
