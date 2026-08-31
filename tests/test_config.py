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
            "PIONIR_PROBABILITY_URL": "http://127.0.0.1:8791",
            "PIONIR_GENESIS_URL": "http://127.0.0.1:8000",
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
        self.assertEqual(settings.probability_url, "http://127.0.0.1:8791")
        self.assertEqual(settings.genesis_url, "http://127.0.0.1:8000")

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
    """Which model Theo runs is a fact about another process, so it is settable.

    It only feeds the resident-model discount in admission. A stale value fails
    safe by over-charging, but over-charging refuses turns that would have
    worked and says nothing about why, so a retrain must not need a code change.
    """

    def test_defaults_to_the_currently_declared_build(self) -> None:
        self.assertEqual(PionirSettings().theo_model_id, "theo-local-v17-q4:latest")

    def test_the_environment_overrides_it(self) -> None:
        with patch.dict(
            os.environ, {"PIONIR_THEO_MODEL_ID": "theo-local-v18-q4:latest"}, clear=False
        ):
            self.assertEqual(
                PionirSettings.from_environment().theo_model_id,
                "theo-local-v18-q4:latest",
            )

    def test_a_blank_override_falls_back_rather_than_declaring_nothing(self) -> None:
        with patch.dict(os.environ, {"PIONIR_THEO_MODEL_ID": "   "}, clear=False):
            self.assertEqual(
                PionirSettings.from_environment().theo_model_id,
                "theo-local-v17-q4:latest",
            )
