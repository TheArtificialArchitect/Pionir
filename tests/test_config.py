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
            "PIONIR_THEO_TOKEN": "secret",
            "PIONIR_ATANI_COMMAND_JSON": '["C:\\\\src\\\\Atani\\\\atani.exe"]',
            "PIONIR_BRYO_STATUS_COMMAND_JSON": '["python","-m","bryo.status"]',
            "PIONIR_SPECIALISTS_FILE": str(
                Path(os.getcwd()).resolve() / "specialists.toml"
            ),
            "PIONIR_GPU_LOCK_FILE": str(
                Path(os.getcwd()).resolve() / "gpu.lock"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PionirSettings.from_environment()
        self.assertEqual(settings.theo_token, "secret")
        self.assertEqual(settings.atani_command, (r"C:\src\Atani\atani.exe",))
        self.assertEqual(
            settings.bryo_status_command,
            ("python", "-m", "bryo.status"),
        )
        self.assertEqual(
            settings.specialists_file,
            Path(os.getcwd()).resolve() / "specialists.toml",
        )
        self.assertNotIn("secret", repr(settings))
        self.assertEqual(
            settings.gpu_lock_path,
            Path(os.getcwd()).resolve() / "gpu.lock",
        )

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


class TheoModelIdTests(unittest.TestCase):
    """Which model Theo serves is a fact about his process, so Pionir asks him.

    Unset means ask. It is settable only to pin a specific build, and a pinned
    value that goes stale silently loses the resident-model discount and starts
    refusing turns that would have fitted.
    """

    def test_unset_means_ask_theo_rather_than_declare_a_version(self) -> None:
        self.assertIsNone(PionirSettings().theo_model_id)

    def test_the_environment_pins_a_specific_build(self) -> None:
        with patch.dict(
            os.environ, {"PIONIR_THEO_MODEL_ID": "theo-local-v25-q4"}, clear=False
        ):
            self.assertEqual(
                PionirSettings.from_environment().theo_model_id, "theo-local-v25-q4"
            )

    def test_a_blank_pin_reverts_to_asking_rather_than_pinning_nothing(self) -> None:
        with patch.dict(os.environ, {"PIONIR_THEO_MODEL_ID": "   "}, clear=False):
            self.assertIsNone(PionirSettings.from_environment().theo_model_id)


if __name__ == "__main__":
    unittest.main()
