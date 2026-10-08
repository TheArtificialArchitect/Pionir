"""How each capability reaches the owner, as /api/capabilities reports it.

Pionir Desktop shows every crew worker's approval level. It used to guess it from a map
copied out of Pionir's code; now each capability in /api/capabilities carries its flags
(risk, requires_approval, spends_money, batchable, batch_condition, routable) and a
derived ``approval``: "auto" (it just runs), "digest" (it waits for the owner's daily
digest) or "card" (its own approval card at once - money, a client, anything privileged).

The table below is every capability Pionir registers today with every optional adapter
on. It is meant to be edited by hand: a new capability, or one whose approval changes,
fails here until someone writes down that they meant it. The other tests hold that the
reported level is the one the server acts on - the same functions park and batch.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from standins import down_url

from pionir.batching import (
    APPROVAL_LEVELS,
    DigestSettings,
    approval_level,
    batch_refusal,
)
from pionir.bootstrap import build_runtime
from pionir.cli import _capabilities
from pionir.config import PionirSettings
from pionir.contracts import Capability, RiskLevel
from pionir.server import PionirApp

_NO_BINARY = ("pionir-test-no-such-binary",)

# name -> (approval, batch_condition). "card" is also what any privileged capability is
# for a caller without its permission - and no HTTP client holds one (auth.py).
EXPECTED = {
    "reasoning.atani_answer": ("auto", None),
    "executive.atani_run": ("card", None),
    "manager.atani_manage": ("card", None),
    "organism.bryo_status": ("auto", None),
    "work.summary": ("auto", None),
    "client.orders": ("auto", None),
    "client.email": ("card", None),
    "client.find_report": ("card", None),
    "client.set_status": ("auto", None),
    "client.deliver": ("card", None),
    "client.quote": ("card", None),
    "client.quote_reminder": ("card", None),
    "client.release": ("card", None),
    "client.testimonials": ("auto", None),
    "client.testimonial_publish": ("card", None),
    "content.publish": ("digest", None),
    "content.unpublish": ("card", None),
    "content.crosspost_devto": ("digest", None),
    # an email to the whole newsletter list: its own card, never the digest
    "content.newsletter_send": ("card", None),
    "crew.digest": ("auto", None),
    "crew.divisions": ("auto", None),
    "crew.compute": ("auto", None),
    "crew.set_goal": ("auto", None),
    "crew.allocate": ("auto", None),
    # the worker controls: compute only, reversible, self-expiring (crew/control.py)
    "crew.run_worker": ("auto", None),
    "crew.pause_worker": ("auto", None),
    "crew.resume_worker": ("auto", None),
    "crew.set_cadence": ("auto", None),
    "coding.daedalus_solve": ("card", None),
    "coding.daedalus_build": ("card", None),
    "coding.daedalus_build_cancel": ("auto", None),
    "builds.card": ("auto", None),
    "builds.inbox": ("auto", None),
    "fiverr.events": ("auto", None),
    "fiverr.ack": ("auto", None),
    "fiverr.card": ("auto", None),
    "fiverr.inbox": ("auto", None),
    "conversation.galatea_reply": ("auto", None),
    "social.instagram_post": ("digest", None),
    "social.instagram_insights": ("auto", None),
    "tools.melete_invoke": ("card", None),
    "security.nyx_status": ("auto", None),
    "security.nyx_run": ("card", None),
    "security.voodoo_status": ("auto", None),
    "security.voodoo_run": ("card", None),
    "owner.notify": ("auto", None),
    "product.gumroad_publish": ("digest", "new listing only"),
    "product.gumroad_unpublish": ("card", None),
    "product.gumroad_list": ("auto", None),
    "apibuild.verify": ("card", None),
    "video.youtube_upload": ("card", None),
    # Proteus (trading): brakes run at once; arming is money - its own card, every call.
    "proteus.status": ("auto", None),
    "proteus.logs": ("auto", None),
    "proteus.kill": ("auto", None),
    "proteus.stop_timer": ("auto", None),
    "proteus.stop_service": ("auto", None),
    "proteus.rh_orders_off": ("auto", None),
    "proteus.arm_timer": ("card", None),
    "proteus.clear_kill": ("card", None),
    "proteus.rh_orders_on": ("card", None),
    "proteus.start_service": ("card", None),
    "proteus.deploy": ("card", None),
    "quotes.card": ("auto", None),
}

NEW_FIELDS = ("requires_approval", "spends_money", "batchable", "batch_condition",
              "routable", "approval")
OLD_FIELDS = ("agent_id", "agent_version", "name", "description", "risk",
              "required_permissions", "model")


def _full_runtime(case: unittest.TestCase, tmp: str):
    """Every adapter registered, none reachable: a 503 stand-in or a missing binary for
    each service, tokens in ``tmp``, and the owner's Discord surfaces on with the Discord
    client faked for the test's life, so nothing can reach Discord (test_hermetic)."""
    no_discord = mock.patch("pionir.discord_gate.DiscordRest",
                            side_effect=AssertionError("a test reached for Discord"))
    no_discord.start()
    case.addCleanup(no_discord.stop)
    return build_runtime(PionirSettings(
        state_root=Path(tmp), atani_command=_NO_BINARY,
        galatea_url=down_url(), galatea_model_id="stub-model", embed_model=None,
        daedalus_url=down_url(), melete_url=down_url(),
        bryo_status_command=_NO_BINARY, bryo_pressure=False,
        nyx_status_command=_NO_BINARY, voodoo_status_command=_NO_BINARY,
        evict_to_fit=False, crew_url=down_url(),
        owner_notify=True, quote_cards=True, fiverr_desk=True, builds_cards=True,
        client_token_dir=Path(tmp) / "secrets",
    ))


class CapabilityApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_full_runtime(self, self._tmp.name))
        self.app.digest = DigestSettings(enabled=True)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def _caps(self) -> dict:
        return {cap.name: cap for manifest in self.app.runtime.executive.registry.manifests()
                for cap in manifest.capabilities}

    def test_every_registered_capability_has_its_expected_approval(self) -> None:
        roster = {c["name"]: c for c in self.app.roster()}
        self.assertEqual(sorted(roster), sorted(EXPECTED),
                         "a capability was added or removed: put it in EXPECTED on purpose")
        for name, (approval, condition) in EXPECTED.items():
            with self.subTest(capability=name):
                self.assertEqual((roster[name]["approval"], roster[name]["batch_condition"]),
                                 (approval, condition))

    def test_the_response_only_adds_fields(self) -> None:
        caps = self._caps()
        for row in self.app.roster():
            with self.subTest(capability=row["name"]):
                for key in OLD_FIELDS + NEW_FIELDS:
                    self.assertIn(key, row)
                cap = caps[row["name"]]
                self.assertEqual(row["risk"], cap.risk.value)
                self.assertIs(row["requires_approval"], cap.requires_approval)
                self.assertIs(row["spends_money"], cap.spends_money)
                self.assertIs(row["batchable"], cap.batchable)
                self.assertIs(row["routable"], cap.routable)
                self.assertIn(row["approval"], APPROVAL_LEVELS)

    def test_the_reported_level_is_what_the_server_does(self) -> None:
        # parking: "auto" exactly when a caller holding nothing is not parked
        # batching: "digest" exactly when a clean call may wait for the digest
        for name, cap in self._caps().items():
            with self.subTest(capability=name):
                level = approval_level(cap)
                self.assertEqual(level == "auto", not self.app._needs_approval(name, []))
                if level != "auto":
                    may_wait = batch_refusal(cap, {}, {"listing": "new"}) is None
                    self.assertEqual(level == "digest", may_wait)

    def test_with_the_digest_off_nothing_waits_for_it(self) -> None:
        self.app.digest = DigestSettings(enabled=False)
        levels = {c["name"]: c["approval"] for c in self.app.roster()}
        for name, (approval, _condition) in EXPECTED.items():
            with self.subTest(capability=name):
                self.assertEqual(levels[name], "card" if approval == "digest" else approval)

    def test_the_cli_listing_is_the_same_rows(self) -> None:
        self.assertEqual(_capabilities(self.app.runtime, digest_enabled=True),
                         self.app.roster())


class ApprovalLevelTests(unittest.TestCase):
    """The derivation itself, on capabilities made up for it."""

    def test_levels(self) -> None:
        read = Capability("x.read", "read")
        write = Capability("x.write", "write", risk=RiskLevel.REVERSIBLE_WRITE)
        gated = Capability("x.run", "run", risk=RiskLevel.PRIVILEGED,
                           required_permissions=frozenset({"x.run"}))
        money = Capability("x.spend", "spend", risk=RiskLevel.PRIVILEGED, spends_money=True)
        public = Capability("x.post", "post", risk=RiskLevel.PRIVILEGED,
                            requires_approval=True, batchable=True)
        listing = Capability("x.list_new", "list", risk=RiskLevel.PRIVILEGED,
                             requires_approval=True, batchable=True,
                             sale_price_keys=frozenset({"price_cents"}))
        client_word = Capability("x.client_post", "post", risk=RiskLevel.PRIVILEGED,
                                 requires_approval=True, batchable=True)
        self.assertEqual(approval_level(read), "auto")
        self.assertEqual(approval_level(write), "auto")
        self.assertEqual(approval_level(gated), "card")
        self.assertEqual(approval_level(gated, {"x.run"}), "auto")   # holds the permission
        self.assertEqual(approval_level(money), "card")
        self.assertEqual(approval_level(money, {"anything"}), "card")
        self.assertEqual(approval_level(public), "digest")
        self.assertEqual(approval_level(public, digest_enabled=False), "card")
        self.assertEqual(approval_level(listing), "digest")
        self.assertEqual(approval_level(client_word), "card")   # never batched: a client


if __name__ == "__main__":
    unittest.main()
