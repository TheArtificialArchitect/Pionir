"""The Bolts ledger: append-only, hash-chained, balances derived by replay.

A row is ``seq, ts, account, delta, reason, event_id, prev_hash, hash``. Balances are
never stored: they are the sum of ``delta`` per account over the rows. Each row's hash
covers its seven other fields and the previous row's hash, so editing, deleting or
reordering any row breaks the chain at a seq ``verify_chain`` names. A chain alone cannot
show that the LAST rows were cut off, so a small ``bolts.head`` sidecar records the last
seq and hash; a file shorter than the anchor is BROKEN too.

Idempotent by ``event_id``: paying the same verified event twice returns the original row.
Fail closed: appending to a broken chain is refused. A torn final line (a crash mid-write)
is reported, never silently dropped; the next append quarantines its bytes beside the
ledger (``bolts.jsonl.torn-<ts>``) and logs at ERROR, so it is not a permanent latch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pionir.shared_gpu import hold_file_lock

log = logging.getLogger("pionir.economy")

GENESIS = "0" * 64
ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
LEDGER_NAME = "bolts.jsonl"
HEAD_NAME = "bolts.head"
_FIELDS = ("seq", "ts", "account", "delta", "reason", "event_id", "prev_hash")


class LedgerError(Exception):
    """Base class for ledger refusals."""


class InsufficientBolts(LedgerError):
    """A debit that would take an account below zero."""


class LedgerBroken(LedgerError):
    """The chain does not verify; nothing is appended until a person looks."""


@dataclass(frozen=True, slots=True)
class Row:
    seq: int
    ts: float
    account: str
    delta: int
    reason: str
    event_id: str
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "account": self.account, "delta": self.delta,
                "reason": self.reason, "event_id": self.event_id,
                "prev_hash": self.prev_hash, "hash": self.hash}


@dataclass(frozen=True, slots=True)
class Appended:
    row: Row
    created: bool        # False: the event_id was already paid; ``row`` is the original


@dataclass(frozen=True, slots=True)
class ChainStatus:
    ok: bool
    rows: int
    first_bad_seq: int | None = None
    problem: str = ""
    torn: bool = False
    torn_bytes: int = 0
    quarantined: int = 0        # torn tails already set aside

    @property
    def label(self) -> str:
        if not self.ok:
            return f"BROKEN at seq {self.first_bad_seq}"
        if self.torn:
            return "TORN TAIL"
        return "OK"

    def to_dict(self) -> dict:
        return {"ok": self.ok, "label": self.label, "rows": self.rows,
                "first_bad_seq": self.first_bad_seq, "problem": self.problem,
                "torn": self.torn, "torn_bytes": self.torn_bytes,
                "quarantined": self.quarantined}


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def row_hash(seq: int, ts: float, account: str, delta: int, reason: str, event_id: str,
             prev_hash: str) -> str:
    body = {"seq": seq, "ts": ts, "account": account, "delta": delta, "reason": reason,
            "event_id": event_id, "prev_hash": prev_hash}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _parse_row(obj: object) -> Row | None:
    if not isinstance(obj, dict) or set(obj) != set(_FIELDS) | {"hash"}:
        return None
    seq, ts, delta = obj["seq"], obj["ts"], obj["delta"]
    if (not isinstance(seq, int) or isinstance(seq, bool)
            or not isinstance(delta, int) or isinstance(delta, bool)
            or not isinstance(ts, (int, float)) or isinstance(ts, bool)):
        return None
    for key in ("account", "reason", "event_id", "prev_hash", "hash"):
        if not isinstance(obj[key], str):
            return None
    return Row(seq, ts, obj["account"], delta, obj["reason"], obj["event_id"],
               obj["prev_hash"], obj["hash"])


@dataclass(slots=True)
class _Scan:
    rows: list
    status: ChainStatus
    torn_text: bytes = b""
    good_end: int = 0           # byte offset just past the last complete line


class Ledger:
    """One JSONL file under the economy dir. All reads and writes take a cross-process lock."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time,
                 lock_timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(HEAD_NAME)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._now = now
        self._lock_timeout = lock_timeout

    @classmethod
    def in_dir(cls, directory: Path, **kw) -> "Ledger":
        return cls(Path(directory) / LEDGER_NAME, **kw)

    # ---- reading ---------------------------------------------------------------------

    def _read_head(self) -> tuple[dict | None, str]:
        try:
            raw = self.head_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, ""
        except OSError as exc:
            return None, f"head anchor unreadable: {exc}"
        try:
            head = json.loads(raw)
            if (isinstance(head, dict) and isinstance(head.get("seq"), int)
                    and isinstance(head.get("hash"), str)):
                return head, ""
        except ValueError:
            pass
        return None, "head anchor is not valid"

    def _quarantine_count(self) -> int:
        try:
            return sum(1 for p in self.path.parent.glob(self.path.name + ".torn-*"))
        except OSError:
            return 0

    def _scan(self) -> _Scan:
        quarantined = self._quarantine_count()
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data = b""
        except OSError as exc:
            return _Scan([], ChainStatus(False, 0, 0, f"ledger unreadable: {exc}",
                                         quarantined=quarantined))
        rows: list[Row] = []
        seen: set[str] = set()
        prev, offset = GENESIS, 0
        torn_text = b""
        lines = data.split(b"\n")
        tail = lines.pop()          # text after the last newline: empty, or a torn write
        bad: tuple[int, str] | None = None
        for raw in lines:
            offset += len(raw) + 1
            if bad:
                continue
            pos = len(rows)
            try:
                row = _parse_row(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                row = None
            if row is None:
                bad = (pos, "line is not a valid ledger row")
                continue
            if row.seq != pos:
                bad = (pos, f"seq is {row.seq}, expected {pos} (deleted or reordered row)")
            elif row.prev_hash != prev:
                bad = (pos, "prev_hash does not match the row before it")
            elif row.hash != row_hash(row.seq, row.ts, row.account, row.delta, row.reason,
                                      row.event_id, row.prev_hash):
                bad = (pos, "row hash does not match its content (edited row)")
            elif row.event_id in seen:
                bad = (pos, f"event_id {row.event_id!r} appears twice")
            if bad:
                continue
            rows.append(row)
            seen.add(row.event_id)
            prev = row.hash
        good_end = offset if not bad else 0
        if bad:
            return _Scan(rows, ChainStatus(False, len(rows), bad[0], bad[1],
                                           quarantined=quarantined))
        torn = bool(tail)
        if torn:
            torn_text = tail
        head, head_problem = self._read_head()
        if head_problem:
            return _Scan(rows, ChainStatus(False, len(rows), len(rows), head_problem,
                                           torn, len(tail), quarantined), torn_text, good_end)
        if head is not None:
            last = len(rows) - 1
            if head["seq"] > last:
                return _Scan(rows, ChainStatus(
                    False, len(rows), len(rows),
                    f"ledger ends at seq {last} but the head anchor says seq {head['seq']} "
                    f"(rows were cut off)", torn, len(tail), quarantined), torn_text, good_end)
            if head["seq"] < last - 1:
                # one row behind is the crash gap (row written, anchor not yet); more is not
                return _Scan(rows, ChainStatus(
                    False, len(rows), head["seq"] + 1,
                    "head anchor is far behind the ledger (rewritten file?)", torn,
                    len(tail), quarantined), torn_text, good_end)
            if head["seq"] < 0 or rows[head["seq"]].hash != head["hash"]:
                return _Scan(rows, ChainStatus(
                    False, len(rows), head["seq"],
                    "the row at the anchored seq differs from the anchor (rewritten row)",
                    torn, len(tail), quarantined), torn_text, good_end)
        return _Scan(rows, ChainStatus(True, len(rows), torn=torn, torn_bytes=len(tail),
                                       quarantined=quarantined), torn_text, good_end)

    def rows(self) -> list[Row]:
        with self._locked():
            return list(self._scan().rows)

    def verify_chain(self) -> ChainStatus:
        with self._locked():
            return self._scan().status

    status = verify_chain

    def balances(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows():
            out[row.account] = out.get(row.account, 0) + row.delta
        return out

    def balance(self, account: str) -> int:
        return self.balances().get(account, 0)

    def get(self, event_id: str) -> Row | None:
        for row in self.rows():
            if row.event_id == event_id:
                return row
        return None

    # ---- writing ---------------------------------------------------------------------

    def _locked(self):
        return hold_file_lock(self._lock_path, timeout=self._lock_timeout)

    def append(self, account: str, delta: int, reason: str, event_id: str) -> Appended:
        if not isinstance(account, str) or not ACCOUNT_RE.match(account):
            raise LedgerError(f"bad account id {account!r}")
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise LedgerError(f"delta must be a whole number of Bolts, not {delta!r}")
        if not isinstance(event_id, str) or not event_id.strip():
            raise LedgerError("an event_id is required")
        if not isinstance(reason, str):
            raise LedgerError("reason must be text")
        with self._locked():
            scan = self._scan()
            st = scan.status
            if not st.ok:
                raise LedgerBroken(f"ledger {st.label}: {st.problem}")
            for existing in scan.rows:
                if existing.event_id == event_id:
                    return Appended(existing, False)
            if delta < 0:
                held = sum(r.delta for r in scan.rows if r.account == account)
                if held + delta < 0:
                    raise InsufficientBolts(f"{account} holds {held}, cannot take {-delta}")
            if st.torn:
                self._quarantine(scan)
            prev = scan.rows[-1].hash if scan.rows else GENESIS
            seq = len(scan.rows)
            ts = float(self._now())
            row = Row(seq, ts, account, delta, reason, event_id, prev,
                      row_hash(seq, ts, account, delta, reason, event_id, prev))
            line = json.dumps(row.to_dict(), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as fh:
                fh.write(line.encode("ascii"))
                fh.flush()
                os.fsync(fh.fileno())
            self._write_head(row)
            return Appended(row, True)

    def _write_head(self, row: Row) -> None:
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(json.dumps({"seq": row.seq, "hash": row.hash}).encode("ascii"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.head_path)

    def _quarantine(self, scan: _Scan) -> None:
        dest = self.path.with_name(f"{self.path.name}.torn-{int(self._now())}")
        n = 0
        while dest.exists():
            n += 1
            dest = self.path.with_name(f"{self.path.name}.torn-{int(self._now())}-{n}")
        dest.write_bytes(scan.torn_text)
        with self.path.open("r+b") as fh:
            fh.truncate(scan.good_end)
            fh.flush()
            os.fsync(fh.fileno())
        log.error("economy ledger: a torn final line (%d bytes) was set aside at %s; "
                  "the event it belonged to will be paid again if it is still verified",
                  len(scan.torn_text), dest)
