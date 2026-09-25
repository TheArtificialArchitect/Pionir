"""The registry: the one list of who exists, built from catalogue data, loud when wrong.

Each test fails if the rule is reverted: a duplicate id accepted (one of the two would
silently never run), an unknown impl, provider, division or field accepted, or a request
for a worker that does not exist read as a worker with nothing to say.
"""

import unittest

from test_crew_fakes import TEST_IMPLS, catalogue

from pionir.crew.registry import build_registry, default_registry, load_catalogue


class DefaultCatalogueTests(unittest.TestCase):
    def test_the_initial_catalogue(self) -> None:
        reg = default_registry()
        self.assertEqual(reg.division_ids(),
                         ("treasury", "watch", "posting", "products", "builds"))
        self.assertEqual(reg.ids(), ("builds.daedalus", "posting.blog", "posting.instagram",
                                     "products.api_builder", "products.gumroad",
                                     "treasury.ledger", "watch.health"))
        live = sorted(w.worker_id for w in reg.all() if w.live)
        self.assertEqual(live, ["posting.blog", "posting.instagram", "treasury.ledger",
                                "watch.health"])

    def test_adding_a_worker_is_adding_an_entry(self) -> None:
        cat = load_catalogue()
        cat["divisions"][2]["workers"].append(
            {"name": "newsletter", "impl": "placeholder", "kind": "post",
             "cadence_seconds": 3600, "provider": "none"})
        reg = build_registry(cat)
        self.assertIn("posting.newsletter", reg.ids())
        self.assertIn("posting.newsletter", reg.cadences("posting"))


class LoudFailureTests(unittest.TestCase):
    def assertRefused(self, cat, words: str) -> None:
        with self.assertRaises(ValueError) as caught:
            build_registry(cat, TEST_IMPLS)
        self.assertIn(words, str(caught.exception))

    def test_a_duplicate_worker_id_is_an_error(self) -> None:
        self.assertRefused(catalogue({"alpha": [{"name": "a1"}, {"name": "a1"}]}),
                           "duplicate worker id 'alpha.a1'")

    def test_a_duplicate_division_is_an_error(self) -> None:
        cat = catalogue({"alpha": [{"name": "a1"}]})
        cat["divisions"].append(dict(cat["divisions"][0], workers=[]))
        self.assertRefused(cat, "duplicate division id 'alpha'")

    def test_an_unknown_impl_is_an_error(self) -> None:
        self.assertRefused(catalogue({"alpha": [{"name": "a1", "impl": "gumroad_real"}]}),
                           "unknown impl 'gumroad_real'")

    def test_an_unknown_provider_is_an_error(self) -> None:
        self.assertRefused(catalogue({"alpha": [{"name": "a1", "provider": "nowhere"}]}),
                           "unknown provider 'nowhere'")

    def test_a_misspelt_field_is_an_error_not_a_default(self) -> None:
        self.assertRefused(catalogue({"alpha": [{"name": "a1", "cadence_second": 5}]}),
                           "unknown field(s) cadence_second")

    def test_params_an_impl_does_not_take_are_an_error(self) -> None:
        self.assertRefused(catalogue({"alpha": [{"name": "a1", "impl": "placeholder",
                                                 "params": {"url": "x"}}]}),
                           "does not take these params")

    def test_an_unknown_name_is_none_or_an_error_never_an_empty_worker(self) -> None:
        reg = build_registry(catalogue(), TEST_IMPLS)
        self.assertIsNone(reg.resolve("alpha.nope"))
        with self.assertRaises(KeyError):
            reg.require("alpha.nope")
        with self.assertRaises(KeyError):
            reg.division("nope")


if __name__ == "__main__":
    unittest.main()
