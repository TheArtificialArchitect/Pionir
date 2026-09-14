import unittest

from pionir.adapters.voodoo_status import _findings_are_answers
from pionir.contracts import outcome_failure_reason


class VoodooFindingsTests(unittest.TestCase):
    def test_secrets_found_is_a_successful_scan(self):
        raw = {"ok": False, "returncode": 1, "output": [{"file": "a"}, {"file": "b"}]}
        result = _findings_are_answers("defend secrets", raw)
        self.assertIsNone(outcome_failure_reason(result))
        self.assertEqual(result["findings"], 2)
        self.assertEqual(result["process_returncode"], 1)

    def test_refusal_stays_a_failure(self):
        raw = {"ok": False, "returncode": 2, "output": "Refused: no scope"}
        self.assertIsNotNone(outcome_failure_reason(_findings_are_answers("defend secrets", raw)))

    def test_exit_one_without_findings_list_stays_a_failure(self):
        raw = {"ok": False, "returncode": 1, "output": "Traceback ..."}
        self.assertIsNotNone(outcome_failure_reason(_findings_are_answers("defend drift", raw)))

    def test_other_actions_keep_their_exit_code(self):
        raw = {"ok": False, "returncode": 1, "output": []}
        self.assertIsNotNone(outcome_failure_reason(_findings_are_answers("defend posture", raw)))


if __name__ == "__main__":
    unittest.main()
