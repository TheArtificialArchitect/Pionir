import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pionir.config import PionirSettings


class ConfigTests(unittest.TestCase):
    def test_initializes_private_state_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = PionirSettings(state_root=Path(directory) / "state")
            settings.initialize_runtime()
            self.assertTrue(settings.audit_path.parent.is_dir())
            self.assertTrue(settings.gpu_lock_path.parent.is_dir())

    def test_reads_token_and_command_from_environment(self) -> None:
        environment = {
            "PIONIR_STATE_ROOT": str(Path(os.getcwd()).resolve() / "state"),
            "PIONIR_ATANI_COMMAND_JSON": '["C:\\\\src\\\\Atani\\\\atani.exe"]',
            "PIONIR_BRYO_STATUS_COMMAND_JSON": '["python","-m","bryo.status"]',
            "PIONIR_DAEDALUS_TOKEN": "do-not-print-this-token",
            "PIONIR_MELETE_TOKEN": "nor-this-one",
            "PIONIR_SPECIALISTS_FILE": str(
                Path(os.getcwd()).resolve() / "specialists.toml"
            ),
            "PIONIR_GPU_LOCK_FILE": str(
                Path(os.getcwd()).resolve() / "gpu.lock"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PionirSettings.from_environment()
        self.assertEqual(settings.atani_command, (r"C:\src\Atani\atani.exe",))
        self.assertEqual(
            settings.bryo_status_command,
            ("python", "-m", "bryo.status"),
        )
        self.assertEqual(
            settings.specialists_file,
            Path(os.getcwd()).resolve() / "specialists.toml",
        )
        self.assertNotIn("do-not-print-this-token", repr(settings))
        self.assertNotIn("nor-this-one", repr(settings))
        self.assertEqual(
            settings.gpu_lock_path,
            Path(os.getcwd()).resolve() / "gpu.lock",
        )

    def test_nyx_and_voodoo_run_as_modules_from_their_new_trees(self) -> None:
        # The `nyx` console script's editable install points at the deleted C:\src\Nyx:
        # both organs run `python -m <package>` from their src trees under Nyx.Voodoo.
        settings = PionirSettings(state_root=Path(tempfile.gettempdir()))
        self.assertEqual(settings.nyx_status_command, ("python", "-m", "nyx", "status"))
        self.assertEqual(settings.nyx_run_prefix, ("python", "-m", "nyx"))
        self.assertEqual(settings.nyx_status_cwd, r"C:\src\Nyx.Voodoo\Nyx\src")
        self.assertEqual(settings.voodoo_status_command,
                         ("python", "-m", "voodoo", "status"))
        self.assertEqual(settings.voodoo_run_prefix, ("python", "-m", "voodoo"))
        self.assertEqual(settings.voodoo_status_cwd, r"C:\src\Nyx.Voodoo\Voodoo\src")
        # the run allowlists are unchanged
        self.assertEqual(settings.nyx_run_actions, ("research", "crawl", "fingerprint", "cert"))
        self.assertIn("defend hunt", settings.voodoo_run_actions)
        self.assertNotIn("defend", settings.voodoo_run_actions)

    def test_nyx_and_voodoo_overrides_still_apply(self) -> None:
        environment = {
            "PIONIR_STATE_ROOT": str(Path(os.getcwd()).resolve() / "state"),
            "PIONIR_NYX_STATUS_COMMAND_JSON": '["py","-m","nyx","status"]',
            "PIONIR_NYX_STATUS_CWD": r"D:\elsewhere\Nyx\src",
            "PIONIR_VOODOO_STATUS_CWD": r"D:\elsewhere\Voodoo\src",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PionirSettings.from_environment()
        self.assertEqual(settings.nyx_status_command, ("py", "-m", "nyx", "status"))
        self.assertEqual(settings.nyx_status_cwd, r"D:\elsewhere\Nyx\src")
        self.assertEqual(settings.voodoo_status_cwd, r"D:\elsewhere\Voodoo\src")
        with patch.dict(os.environ, {**environment,
                                     "PIONIR_NYX_STATUS_COMMAND_JSON": "off"}, clear=True):
            self.assertIsNone(PionirSettings.from_environment().nyx_status_command)

    def test_bootstrap_hands_nyx_its_working_directory(self) -> None:
        from pionir.bootstrap import build_runtime

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(PionirSettings(
                state_root=Path(directory).resolve(), atani_command=("no-such-binary",),
                bryo_status_command=None, daedalus_url=None, melete_url=None,
                crew_url=None, galatea_url=None, content_url=None, gumroad_url=None,
                evict_to_fit=False, embed_model=None,
                nyx_status_cwd=directory, voodoo_status_cwd=directory))
            try:
                nyx = runtime.adapters["nyx"]
                self.assertEqual(nyx.settings.cwd, directory)
                self.assertEqual(nyx._runner._cwd, directory)
                self.assertEqual(nyx.settings.run_prefix, ("python", "-m", "nyx"))
                self.assertEqual(runtime.adapters["voodoo"].settings.cwd, directory)
            finally:
                runtime.cortex.close()

    def test_rejects_relative_shared_gpu_lock_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = PionirSettings(
                state_root=Path(directory).resolve(),
                shared_gpu_lock_file=Path("relative-gpu.lock"),
            )
            with self.assertRaisesRegex(ValueError, "GPU lock path"):
                settings.initialize_runtime()


if __name__ == "__main__":
    unittest.main()



if __name__ == "__main__":
    unittest.main()
