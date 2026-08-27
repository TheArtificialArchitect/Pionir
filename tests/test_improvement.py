import unittest

from pionir.improvement import (
    EvaluationReport,
    ImprovementCandidate,
    ImprovementRisk,
    PromotionApproval,
    PromotionGate,
)


class ImprovementTests(unittest.TestCase):
    def candidate(self) -> ImprovementCandidate:
        return ImprovementCandidate(
            source_agent="evolutionary-ai",
            target="pionir:router-policy",
            change_ref="candidate:abc123",
            hypothesis="improve specialist selection accuracy",
            rollback_ref="incumbent:def456",
            risk=ImprovementRisk.MEDIUM,
        )

    def report(self, candidate: ImprovementCandidate) -> EvaluationReport:
        return EvaluationReport(
            candidate_id=candidate.candidate_id,
            suite_id="held-out:routing-v1",
            baseline_score=0.80,
            candidate_score=0.84,
            checks={
                "permissions": True,
                "resource_limits": True,
                "rollback": True,
                "held_out": True,
            },
        )

    def test_authorizes_only_exact_approved_candidate(self) -> None:
        candidate = self.candidate()
        decision = PromotionGate().assess(
            candidate,
            self.report(candidate),
            PromotionApproval(candidate.digest, "Ian", True),
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.reasons, ())

    def test_rejects_regression_even_with_approval(self) -> None:
        candidate = self.candidate()
        report = EvaluationReport(
            candidate_id=candidate.candidate_id,
            suite_id="held-out:routing-v1",
            baseline_score=0.80,
            candidate_score=0.90,
            regressions=("approval bypass case",),
            checks={check: True for check in PromotionGate().policy.required_checks},
        )
        decision = PromotionGate().assess(
            candidate,
            report,
            PromotionApproval(candidate.digest, "Ian", True),
        )
        self.assertFalse(decision.authorized)
        self.assertIn("held-out evaluation contains regressions", decision.reasons)

    def test_rejects_stale_or_substituted_approval(self) -> None:
        candidate = self.candidate()
        decision = PromotionGate().assess(
            candidate,
            self.report(candidate),
            PromotionApproval("0" * 64, "Ian", True),
        )
        self.assertFalse(decision.authorized)
        self.assertIn("approval digest does not match the candidate", decision.reasons)

    def test_rejects_self_approval(self) -> None:
        candidate = self.candidate()
        decision = PromotionGate().assess(
            candidate,
            self.report(candidate),
            PromotionApproval(candidate.digest, "evolutionary-ai", True),
        )
        self.assertFalse(decision.authorized)
        self.assertIn("approval was not issued by Ian", decision.reasons)


if __name__ == "__main__":
    unittest.main()
