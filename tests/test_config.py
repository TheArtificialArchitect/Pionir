import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pionir.config import PionirSettings, _discover_src


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

    def test_security_organs_run_as_module_not_stale_console_script(self) -> None:
        # Nyx/Voodoo moved to C:\src\Nyx.Voodoo; the editable installs still point
        # at the old trees, so the `nyx` console script and a bare import find
        # nothing. Pionir runs each as `python -m <pkg>` out of its own src tree.
        settings = PionirSettings()
        self.assertEqual(settings.nyx_status_command, ("python", "-m", "nyx", "status"))
        self.assertEqual(settings.nyx_run_prefix, ("python", "-m", "nyx"))
        self.assertEqual(settings.voodoo_status_command, ("python", "-m", "voodoo", "status"))
        self.assertEqual(settings.voodoo_run_prefix, ("python", "-m", "voodoo"))

    def test_discover_src_prefers_new_layout_falls_back_to_old(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = root / "new" / "src"
            old = root / "old"
            (new / "widget").mkdir(parents=True)
            old.mkdir()
            # The package lives under the new tree only: it wins.
            self.assertEqual(
                _discover_src("widget", (str(new), str(old))),
                str(new),
            )
            # None of the candidates holds it: the first is returned unchanged, so
            # the setting still constructs and doctor reports the tree as gone.
            self.assertEqual(
                _discover_src("absent", (str(old), str(new))),
                str(old),
            )

    def test_security_status_cwd_honours_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "PIONIR_STATE_ROOT": str(Path(directory) / "state"),
                    "PIONIR_NYX_STATUS_CWD": directory,
                    "PIONIR_VOODOO_STATUS_CWD": directory,
                },
                clear=True,
            ):
                settings = PionirSettings.from_environment()
        self.assertEqual(settings.nyx_status_cwd, directory)
        self.assertEqual(settings.voodoo_status_cwd, directory)

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
