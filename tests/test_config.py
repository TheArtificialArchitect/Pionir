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

    def test_reads_token_and_command_from_environment(self) -> None:
        environment = {
            "PIONIR_STATE_ROOT": str(Path(os.getcwd()).resolve() / "state"),
            "PIONIR_THEO_TOKEN": "secret",
            "PIONIR_ATANI_COMMAND_JSON": '["C:\\\\src\\\\Atani\\\\atani.exe"]',
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PionirSettings.from_environment()
        self.assertEqual(settings.theo_token, "secret")
        self.assertEqual(settings.atani_command, (r"C:\src\Atani\atani.exe",))
        self.assertNotIn("secret", repr(settings))


if __name__ == "__main__":
    unittest.main()
