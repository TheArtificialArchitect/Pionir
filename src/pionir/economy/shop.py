"""The Bolts shop and the homes it furnishes. Cosmetic only; no purchase confers any power.

A home is DERIVED from the ledger, never stored: the rows whose reason is ``buy:<item_id>``
are the home. One store means no desync, and a home cannot hold something that was not
paid for. A purchase is one negative ledger row with the event id ``buy:<account>:<item>``,
so buying the same item twice is the same event: the second call changes nothing and
charges nothing.

Trophies are earned, not bought: they are counted from the payout rows already on record.

Nothing here reaches the approval / money gate and nothing in the gate reads a home or a
balance (tests/test_economy_gate.py). The catalogue sells decoration and space; it never
sells a resource, an autonomy or anything a hard gate guards.
"""
from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass

from .ledger import ACCOUNT_RE, InsufficientBolts, Ledger, LedgerError, Row

log = logging.getLogger("pionir.economy")

BUY_PREFIX = "buy:"
TIERS = ("lean-to", "cottage", "house", "manor")
ROOM_SLOTS = {"lean-to": 0, "cottage": 1, "house": 3, "manor": 5}
FURNISHING_SLOTS_PER_ROOM = 4          # the main room counts as one room


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    kind: str                 # tier | room | furnishing
    name: str
    price: int
    blurb: str = ""


CATALOG: tuple[Item, ...] = (
    Item("cottage", "tier", "Cottage", 120, "Walls, a roof and room for one more room."),
    Item("house", "tier", "House", 350, "Space for three extra rooms."),
    Item("manor", "tier", "Manor", 900, "Space for five extra rooms."),
    Item("study", "room", "Study", 60, "Quiet place to think."),
    Item("kitchen", "room", "Kitchen", 50, "Where the long jobs get a warm cup."),
    Item("garden", "room", "Garden", 70, "Open air."),
    Item("workshop", "room", "Workshop", 90, "Benches for the things being built."),
    Item("library", "room", "Library", 110, "Shelves for everything learned."),
    Item("observatory", "room", "Observatory", 160, "A dome that looks outward."),
    Item("lamp", "furnishing", "Lamp", 6),
    Item("rug", "furnishing", "Rug", 10),
    Item("plant", "furnishing", "Plant", 8),
    Item("clock", "furnishing", "Clock", 18),
    Item("banner", "furnishing", "Banner", 14),
    Item("desk", "furnishing", "Desk", 20),
    Item("bookshelf", "furnishing", "Bookshelf", 24),
    Item("window_seat", "furnishing", "Window seat", 30),
    Item("fireplace", "furnishing", "Fireplace", 45),
    Item("telescope", "furnishing", "Telescope", 55),
)
BY_ID = {i.id: i for i in CATALOG}

# kind of payout -> (threshold, trophy id, name)
TROPHIES: tuple[tuple[str, int, str, str], ...] = (
    ("build_staged", 1, "first_build", "First build staged"),
    ("build_staged", 10, "ten_builds", "Ten builds staged"),
    ("post_approved", 1, "first_post", "First post approved"),
    ("post_approved", 10, "ten_posts", "Ten posts approved"),
    ("order_delivered", 1, "first_order", "First order delivered"),
    ("product_published", 1, "shop_open", "A product on the shelf"),
    ("revenue_received", 1, "first_revenue", "First real revenue"),
)


class ShopError(Exception):
    """A purchase that was refused, with a plain reason."""


class CannotAfford(ShopError):
    """The account does not hold enough Bolts yet."""


@dataclass(frozen=True, slots=True)
class BuyResult:
    status: str               # bought | already_owned
    item: Item
    row: Row | None = None


def event_id(account: str, item_id: str) -> str:
    return f"{BUY_PREFIX}{account}:{item_id}"


def _owned_ids(rows: list[Row], account: str) -> list[str]:
    out = []
    for r in rows:
        if r.account == account and r.reason.startswith(BUY_PREFIX) and r.delta < 0:
            item_id = r.reason[len(BUY_PREFIX):]
            if item_id in BY_ID:
                out.append(item_id)
    return out


def tier_of(owned: list[str]) -> str:
    tier = TIERS[0]
    for t in TIERS[1:]:
        if t in owned:
            tier = t
    return tier


def home_from_rows(rows: list[Row], account: str) -> dict:
    owned = _owned_ids(rows, account)
    tier = tier_of(owned)
    rooms = [i for i in owned if BY_ID[i].kind == "room"]
    furnishings = [i for i in owned if BY_ID[i].kind == "furnishing"]
    counts: dict[str, int] = {}
    for r in rows:
        if r.account == account and r.delta > 0:
            kind = r.reason.split(" ", 1)[0]
            counts[kind] = counts.get(kind, 0) + 1
    trophies = [{"id": tid, "name": name, "kind": kind, "count": counts.get(kind, 0)}
                for kind, need, tid, name in TROPHIES if counts.get(kind, 0) >= need]
    return {"account": account, "tier": tier,
            "rooms": ["main"] + rooms,
            "room_slots": ROOM_SLOTS[tier], "furnishing_slots": FURNISHING_SLOTS_PER_ROOM * (1 + len(rooms)),
            "furnishings": furnishings, "trophies": trophies,
            "spent": sum(BY_ID[i].price for i in owned),
            "balance": sum(r.delta for r in rows if r.account == account)}


def home_of(ledger: Ledger, account: str) -> dict:
    return home_from_rows(ledger.rows(), account)


def buy(ledger: Ledger, account: str, item_id: str) -> BuyResult:
    """Spend an account's Bolts on one item. Raises ShopError with the reason when refused."""
    if not isinstance(account, str) or not ACCOUNT_RE.match(account):
        raise ShopError(f"bad account id {account!r}")
    item = BY_ID.get(item_id)
    if item is None:
        raise ShopError(f"nothing in the shop is called {item_id!r}")
    rows = ledger.rows()
    home = home_from_rows(rows, account)
    if item.id in _owned_ids(rows, account):
        return BuyResult("already_owned", item)
    if item.kind == "tier":
        have = TIERS.index(home["tier"])
        want = TIERS.index(item.id)
        if want != have + 1:
            raise ShopError(f"{item.name} comes after {TIERS[have + 1]}" if want > have
                            else f"{item.name} is already behind this home")
    elif item.kind == "room":
        if len(home["rooms"]) - 1 >= home["room_slots"]:
            raise ShopError(f"a {home['tier']} has no room for another room; move up first")
    elif len(home["furnishings"]) >= home["furnishing_slots"]:
        raise ShopError("every room is full; add a room first")
    try:
        done = ledger.append(account, -item.price, BUY_PREFIX + item.id, event_id(account, item.id))
    except InsufficientBolts as exc:
        raise CannotAfford(f"{item.name} costs {item.price} Bolts: {exc}") from exc
    except LedgerError as exc:
        raise ShopError(str(exc)) from exc
    return BuyResult("bought" if done.created else "already_owned", item, done.row)


def wishlist(account: str) -> list[str]:
    """The order an account saves up for things: one fixed spine (home first, then rooms and
    furnishings interleaved), the furnishings and rooms rotated by the account name so no two
    homes come out the same."""
    rot = zlib.crc32(account.encode("ascii", "replace"))
    furn = [i.id for i in CATALOG if i.kind == "furnishing"]
    rooms = [i.id for i in CATALOG if i.kind == "room"]
    furn = furn[rot % len(furn):] + furn[:rot % len(furn)]
    rooms = rooms[rot % len(rooms):] + rooms[:rot % len(rooms)]
    f, r = iter(furn), iter(rooms)
    spine = [next(f), next(f), "cottage", next(f), next(r), next(f), next(f), next(r),
             next(f), "house", next(r), next(f), next(r), next(f), next(f), "manor"]
    spine += list(f) + list(r)
    seen: set[str] = set()
    return [i for i in spine if not (i in seen or seen.add(i))]


def settle(ledger: Ledger, account: str) -> list[BuyResult]:
    """Buy, in wishlist order, whatever the account can now afford; stop at the first thing it
    cannot (it saves up). Idempotent: run again and nothing changes until more Bolts arrive."""
    bought: list[BuyResult] = []
    progress = True
    while progress:                       # a later tier unblocks rooms the first pass skipped
        progress = False
        for item_id in wishlist(account):
            try:
                res = buy(ledger, account, item_id)
            except CannotAfford:
                break                     # save up for it
            except ShopError:
                continue                  # blocked by a slot rule for now
            if res.status == "bought":
                bought.append(res)
                progress = True
    return bought


def settle_all(ledger: Ledger) -> list[BuyResult]:
    out: list[BuyResult] = []
    for account in sorted(ledger.balances()):
        out.extend(settle(ledger, account))
    return out
