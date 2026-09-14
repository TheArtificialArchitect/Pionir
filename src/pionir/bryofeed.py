"""Pionir's pulse, written where Bryo can feel it.

Bryo is Pionir's observer organ, and this is what he observes. His sensors run
inside a single synchronous tick and may not do network I/O - worse, importing
`urllib`/`socket` into his sensor file would trip the organism's own G0 gate and
permanently reject every future self-edit of that file. So the coupling is a
cache file, not a call: this poller (a foreground pane of the Pionir stack)
reads the running server over loopback and writes a tiny JSON snapshot; Bryo's
`pionir_*` sensors do a cheap local read of that file and squash its age into a
freshness signal. When the Pionir window is closed the file simply goes stale,
and Bryo perceives that the stack is asleep - which is true.

The contract (the shape Bryo's sensor reads) is intentionally small and all
numbers already carry meaning without a constant:

    {
      "ts":                <unix seconds, float>   # for the age/freshness squash
      "reachable":         bool                    # the server answered this poll
      "gpu_free_frac":     0..1 | null             # observed free VRAM / total
      "gpu_resident":      int                      # models resident on the card
      "gpu_resident_frac": 0..1                     # resident / soft cap
      "roster":            int                      # capabilities registered
      "activity":          0..1                     # audit events since last poll, squashed
    }
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8780"
DEFAULT_OUT = os.environ.get("PIONIR_BRYO_FEED_PATH", r"C:\src\terrarium\state\pionir.json")
DEFAULT_INTERVAL = 10.0

# Soft caps so a count becomes a fraction without smuggling in a magic threshold:
# a 12 GB card in practice holds one or two models, rarely three.
_RESIDENT_CAP = 3.0


def _get_json(url: str, timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            doc = json.loads(response.read(2_000_000).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def shape(state: dict[str, Any] | None, events_total: int | None,
          prev_events_total: int | None) -> tuple[dict[str, Any], int | None]:
    """Turn a Pionir state dict (+ audit total) into Bryo's snapshot.

    Pure: no I/O. Shared by the HTTP poller and the in-server pulse thread so
    both write byte-identical shapes - one shaping, one contract (HEAD 3.9)."""
    now = time.time()
    if state is None:
        return {"ts": now, "reachable": False}, prev_events_total

    gpu = state.get("gpu") or {}
    budget = gpu.get("budget") or {}
    total = budget.get("total_mb") or gpu.get("total_mb")
    free = gpu.get("observed_free_mb")
    resident = gpu.get("resident") or []
    roster = state.get("roster") or []

    snap: dict[str, Any] = {
        "ts": now,
        "reachable": True,
        "gpu_free_frac": _clip01(free / total) if (isinstance(free, (int, float)) and total) else None,
        "gpu_resident": len(resident),
        "gpu_resident_frac": _clip01(len(resident) / _RESIDENT_CAP),
        "roster": len(roster),
    }
    # Activity: how much the ledger moved since the last poll, squashed to 0..1.
    # A delta, so it reflects work happening now, not the total history.
    if isinstance(events_total, int) and isinstance(prev_events_total, int):
        delta = max(0, events_total - prev_events_total)
        snap["activity"] = _clip01(1.0 - math.exp(-delta / 3.0))
    else:
        snap["activity"] = 0.0
    return snap, (events_total if isinstance(events_total, int) else prev_events_total)


def snapshot(url: str, *, prev_events_total: int | None, timeout: float = 4.0) -> tuple[dict[str, Any], int | None]:
    """One reading over HTTP. Returns (snapshot, events_total for the next poll)."""
    state = _get_json(url.rstrip("/") + "/api/state", timeout)
    if state is None:
        return {"ts": time.time(), "reachable": False}, prev_events_total
    # The server reads `n`, not `limit` (server.do_GET); `limit` was ignored and
    # every poll pulled the default 60 events just to read events_total.
    audit = _get_json(url.rstrip("/") + "/api/audit?n=1", timeout)
    events_total = audit.get("events_total") if isinstance(audit, dict) else None
    return shape(state, events_total, prev_events_total)


def write(path: Path | str, snap: dict[str, Any]) -> None:
    """Atomic write of one snapshot to the cache file Bryo reads."""
    _write_atomic(Path(path), snap)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pionir-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)  # atomic; a half-written file never reaches Bryo
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def run_feed(*, url: str = DEFAULT_URL, out: str = DEFAULT_OUT,
             interval: float = DEFAULT_INTERVAL, once: bool = False) -> int:
    """Poll the running Pionir server and write Bryo's cache file until stopped.

    Foreground by design: it lives only as long as its pane is open, so closing
    the Pionir window stops the pulse and Bryo feels the stack go quiet. It never
    installs itself to run on its own (estate rule 3)."""
    path = Path(out)
    prev = None
    print(f"pionir bryo-feed: {url} -> {path} every {interval:g}s (close this pane to stop)", flush=True)
    while True:
        try:
            snap, prev = snapshot(url, prev_events_total=prev)
            _write_atomic(path, snap)
            state = "up" if snap.get("reachable") else "server down"
            free = snap.get("gpu_free_frac")
            print(f"  {time.strftime('%H:%M:%S')} {state}"
                  + (f"  gpu_free={free:.2f}" if isinstance(free, float) else "")
                  + f"  activity={snap.get('activity', 0.0):.2f}", flush=True)
        except OSError as error:
            # Never let a transient write failure kill the pulse; say so and go on.
            print(f"  {time.strftime('%H:%M:%S')} write failed: {error}", flush=True)
        if once:
            return 0
        try:
            time.sleep(max(1.0, interval))
        except KeyboardInterrupt:
            print("  stopped.", flush=True)
            return 0
