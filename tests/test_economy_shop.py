"""The Bolts shop and homes: a home is replayed from the ledger, a purchase spends once.

Each test fails if its rule is reverted: a purchase that charged twice or without enough
Bolts, a home that could hold something unpaid, a room bought past the tier's slots, a tier
skipped, a settle pass that spent past a thing it could not afford or spent again on a
rerun, a trophy that was bought instead of earned. Temp dir only; never the real ~/.pionir.
"""
import tempfile
import unittest
from pathlib import Path

from pionir.economy import shop
from pionir.economy.ledger import Ledger
from pionir.economy.shop import BY_ID, ShopError, buy, home_of, settle, wishlist
from pionir.economy.view import economy_payload


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class ShopCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ledger = Ledger.in_dir(self.dir, now=Clock())

    def fund(self, account: str, bolts: int, kind: str = "build_staged") -> None:
        self.ledger.append(account, bolts, kind, f"fund:{account}:{self.ledger.rows().__len__()}")


class BuyTests(ShopCase):
    def test_a_purchase_is_one_negative_row_and_a_second_call_charges_nothing(self) -> None:
        self.fund("moss", 100)
        first = buy(self.ledger, "moss", "lamp")
        again = buy(self.ledger, "moss", "lamp")
        self.assertEqual((first.status, again.status), ("bought", "already_owned"))
        self.assertEqual(self.ledger.balance("moss"), 100 - BY_ID["lamp"].price)

    def test_without_enough_bolts_nothing_is_bought_or_charged(self) -> None:
        self.fund("moss", 5)
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", "lamp")
        self.assertEqual(self.ledger.balance("moss"), 5)
        self.assertEqual(home_of(self.ledger, "moss")["furnishings"], [])

    def test_an_unknown_item_and_a_bad_account_are_refused(self) -> None:
        self.fund("moss", 50)
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", "yacht")
        with self.assertRaises(ShopError):
            buy(self.ledger, "Not An Account", "lamp")

    def test_a_tier_cannot_be_skipped_and_rooms_need_slots(self) -> None:
        self.fund("moss", 2000)
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", "house")            # cottage first
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", "study")            # a lean-to has no spare room
        buy(self.ledger, "moss", "cottage")
        buy(self.ledger, "moss", "study")
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", "garden")           # a cottage holds one extra room
        self.assertEqual(home_of(self.ledger, "moss")["rooms"], ["main", "study"])

    def test_furnishings_stop_when_every_room_is_full(self) -> None:
        self.fund("moss", 5000)
        furnishings = [i.id for i in shop.CATALOG if i.kind == "furnishing"]
        for item in furnishings[:4]:
            buy(self.ledger, "moss", item)
        self.assertEqual(home_of(self.ledger, "moss")["furnishing_slots"], 4)
        with self.assertRaises(ShopError):
            buy(self.ledger, "moss", furnishings[4])

    def test_one_account_spending_does_not_touch_another(self) -> None:
        self.fund("moss", 50)
        self.fund("daedalus", 50)
        buy(self.ledger, "moss", "lamp")
        self.assertEqual(self.ledger.balance("daedalus"), 50)
        self.assertEqual(home_of(self.ledger, "daedalus")["furnishings"], [])


class HomeTests(ShopCase):
    def test_a_home_holds_only_what_the_ledger_says_was_paid_for(self) -> None:
        self.fund("moss", 100)
        buy(self.ledger, "moss", "lamp")
        # a row that merely LOOKS like a purchase but is a credit buys nothing
        self.ledger.append("moss", 3, "buy:desk", "fake:desk")
        home = home_of(self.ledger, "moss")
        self.assertEqual(home["furnishings"], ["lamp"])
        self.assertEqual(home["tier"], "lean-to")

    def test_trophies_are_earned_from_payout_rows_and_cannot_be_bought(self) -> None:
        self.fund("daedalus", 25, "build_staged")
        names = [t["id"] for t in home_of(self.ledger, "daedalus")["trophies"]]
        self.assertEqual(names, ["first_build"])
        self.assertTrue(all(i.kind != "trophy" for i in shop.CATALOG))
        self.assertEqual(home_of(self.ledger, "melete")["trophies"], [])

    def test_the_view_carries_every_home_and_the_catalogue(self) -> None:
        self.fund("moss", 100)
        buy(self.ledger, "moss", "lamp")
        payload = economy_payload(self.dir, now=Clock())
        self.assertEqual([h["account"] for h in payload["homes"]], ["moss"])
        self.assertEqual(payload["homes"][0]["furnishings"], ["lamp"])
        self.assertEqual(len(payload["shop"]), len(shop.CATALOG))
        self.assertEqual(economy_payload(self.dir / "nowhere")["homes"], [])


class SettleTests(ShopCase):
    def test_settle_buys_in_order_and_stops_at_what_it_cannot_afford(self) -> None:
        self.fund("moss", 100)
        order = wishlist("moss")
        bought = [b.item.id for b in settle(self.ledger, "moss")]
        self.assertEqual(bought, order[:len(bought)])
        self.assertGreater(len(bought), 0)
        nxt = BY_ID[order[len(bought)]]
        self.assertLess(self.ledger.balance("moss"), nxt.price)

    def test_settle_twice_spends_nothing_more(self) -> None:
        self.fund("moss", 100)
        settle(self.ledger, "moss")
        held = self.ledger.balance("moss")
        self.assertEqual(settle(self.ledger, "moss"), [])
        self.assertEqual(self.ledger.balance("moss"), held)

    def test_a_rich_account_finishes_the_whole_catalogue_and_no_more(self) -> None:
        self.fund("moss", 100000)
        settle(self.ledger, "moss")
        home = home_of(self.ledger, "moss")
        self.assertEqual(home["tier"], "manor")
        self.assertEqual(len(home["furnishings"]), 10)
        self.assertEqual(len(home["rooms"]) - 1, 5)
        self.assertEqual(settle(self.ledger, "moss"), [])

    def test_the_wishlist_covers_each_item_once_and_differs_between_accounts(self) -> None:
        a, b = wishlist("moss"), wishlist("daedalus")
        self.assertEqual(sorted(a), sorted(BY_ID))
        self.assertEqual(len(a), len(set(a)))
        self.assertEqual({"cottage", "house", "manor"} - set(a), set())
        self.assertNotEqual(a, b)
        self.assertLess(a.index("cottage"), a.index("house"))
        self.assertLess(a.index("house"), a.index("manor"))


if __name__ == "__main__":
    unittest.main()
