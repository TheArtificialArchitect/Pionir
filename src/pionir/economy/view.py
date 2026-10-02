"""The read-only payload behind the dashboard's Bolts tab. It writes nothing."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from . import payouts
from .ledger import HEAD_NAME, LEDGER_NAME, Ledger
from .payouts import RefusalLog, day_of

RECENT_ROWS = 25


def economy_payload(economy_dir: Path, *, now: Callable[[], float] = time.time,
                    recent: int = RECENT_ROWS) -> dict:
    directory = Path(economy_dir)
    day = day_of(float(now()))
    refused = RefusalLog.in_dir(directory, now=now).counts(day)
    base = {"currency": "Bolts", "play_currency": True, "day": day,
            "refused": refused,
            "caps": {"per_account": payouts.DAILY_CAP_PER_ACCOUNT,
                     "global": payouts.DAILY_CAP_GLOBAL},
            "payouts": {k: dict(v) for k, v in payouts.PAYOUTS.items()}}
    if not (directory / LEDGER_NAME).exists() and not (directory / HEAD_NAME).exists():
        return {**base,
                "chain": {"ok": True, "label": "OK", "rows": 0, "first_bad_seq": None,
                          "problem": "", "torn": False, "torn_bytes": 0, "quarantined": 0},
                "balances": [], "recent": [],
                "today": {"minted": 0, "per_account": []}, "total_supply": 0}
    ledger = Ledger.in_dir(directory, now=now)
    status = ledger.verify_chain()
    rows = ledger.rows()
    balances: dict[str, int] = {}
    per_day: dict[str, int] = {}
    for row in rows:
        balances[row.account] = balances.get(row.account, 0) + row.delta
        if row.delta > 0 and day_of(row.ts) == day:
            per_day[row.account] = per_day.get(row.account, 0) + row.delta
    return {
        **base,
        "chain": status.to_dict(),
        "balances": [{"account": a, "balance": b}
                     for a, b in sorted(balances.items(), key=lambda kv: (-kv[1], kv[0]))],
        "total_supply": sum(balances.values()),
        "recent": [r.to_dict() for r in reversed(rows[-recent:])],
        "today": {"minted": sum(per_day.values()),
                  "per_account": [{"account": a, "minted": m}
                                  for a, m in sorted(per_day.items())]},
    }
