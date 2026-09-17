"""Bryo's felt pressure, read as an advisory signal the spine can consult.

Bryo is Pionir's autonomic organ: an organism that lives in this machine 24/7 and
keeps a felt sense of his own homeostasis - how tired he is, how far each drive has
been pushed from comfort. ``python -m bryo.status --json`` publishes that as a
``bryo.vitals/1`` document with a composite ``pressure`` in [0, 1] and an advisory
``defer_heavy_work``. This module is the spine's side of the coupling: it reads that
document and hands the executive a :class:`PressureReading`.

Three rules, each load-bearing:

* **It never blocks.** :meth:`BryoPressureReader.peek` returns the last reading
  immediately and refreshes it in the background, single-flight. A read shells a
  Python process and costs ~260 ms (measured): harmless every 30 s, unacceptable on
  every dashboard poll or every task admission.
* **It fails open.** No terrarium tree, a crashed or stale organism, a timeout, a
  malformed document, an unknown schema, a NaN - every one of these yields a neutral
  reading (pressure 0, never defer). Bryo's absence reads as "no opinion", never as
  "stop". He can never be in the critical path.
* **Old advice is no advice.** A reading older than ``max_age_seconds`` is dropped
  rather than acted on, so a stress reading from an organism that has since died
  cannot keep holding work back.

It advises; it never decides. Routing stays Atani's, execution stays the doers',
approval stays the gate's. What the executive does with a reading is its own call.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import monotonic

from .adapters.bryo_status import SubprocessTextRunner, TextCommandRunner

SCHEMA = "bryo.vitals/1"
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_MAX_AGE_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 15

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PressureReading:
    alive: bool
    pressure: float
    defer_heavy_work: bool
    note: str
    source: str = "bryo"
    # Is anything still supervising him? None when unknown (no reading, or an
    # organism that isn't answering). Carried because a dead supervisor is
    # invisible otherwise: his own beacon complained 55 times over 2.3 days in
    # September 2026 and nothing surfaced it - the log was the only witness and
    # nobody reads a log. An alarm nobody sees is not an alarm (HEAD 3.19).
    supervised: bool | None = None

    @property
    def detail(self) -> str:
        """A payload-free one-liner for the audit ledger."""
        advice = "defer" if self.defer_heavy_work else "proceed"
        return f"bryo pressure={self.pressure:.2f} {advice} ({self.note})"


def neutral(reason: str) -> PressureReading:
    return PressureReading(False, 0.0, False, reason, source="neutral")


def parse_vitals(text: str) -> PressureReading:
    """Turn a ``bryo.vitals/1`` document into a reading, failing open on anything odd.

    ``defer_heavy_work`` is honoured only from an organism that is alive: a stale
    organism's last advisory is not evidence about the machine now.
    """

    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return neutral("unparseable vitals")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return neutral("unknown vitals schema")
    try:
        pressure = float(doc.get("pressure", 0.0))
    except (TypeError, ValueError):
        return neutral("non-numeric pressure")
    if math.isnan(pressure) or not 0.0 <= pressure <= 1.0:
        return neutral("pressure out of range")
    advisory = doc.get("advisory")
    advisory = advisory if isinstance(advisory, dict) else {}
    note = str(advisory.get("note") or "")[:80]
    alive = doc.get("alive") is True
    supervised = doc.get("supervised") is True if "supervised" in doc else None
    if not alive:
        return PressureReading(False, pressure, False, note or "not alive",
                               supervised=supervised)
    return PressureReading(True, pressure, advisory.get("defer_heavy_work") is True,
                           note, supervised=supervised)


def _spawn_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="bryo-pressure", daemon=True).start()


class BryoPressureReader:
    """A non-blocking, TTL-cached, fail-open reader of Bryo's vitals."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        runner: TextCommandRunner | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn_daemon,
    ) -> None:
        if not command or any(not part for part in command):
            raise ValueError("Bryo pressure command cannot be empty")
        if ttl_seconds <= 0 or max_age_seconds < ttl_seconds:
            raise ValueError("need 0 < ttl_seconds <= max_age_seconds")
        self._runner = runner or SubprocessTextRunner(
            (*command, "--json"), cwd=cwd
        )
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._max_age = max_age_seconds
        self._clock = clock
        self._spawn = spawn
        self._lock = threading.Lock()
        self._reading: PressureReading | None = None
        self._read_at: float | None = None
        self._refreshing = False

    def peek(self) -> PressureReading:
        """The current advice, instantly. Kicks a background refresh when due."""

        now = self._clock()
        with self._lock:
            reading, read_at = self._reading, self._read_at
            due = read_at is None or now - read_at >= self._ttl
            start = due and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            try:
                self._spawn(self._refresh)
            except Exception as error:  # noqa: BLE001 - a failed spawn must not wedge the flag
                _log.warning("could not start Bryo pressure refresh: %s", error)
                with self._lock:
                    self._refreshing = False
        if reading is None or read_at is None:
            return neutral("no reading yet")
        if now - read_at > self._max_age:
            return neutral("reading too old")
        return reading

    def read_now(self) -> PressureReading:
        """One synchronous, bounded read - for a one-shot CLI run.

        ``peek`` never blocks: on the first call it returns neutral ("no reading
        yet") and kicks a background refresh. In a long-running server that fills
        in within a poll or two, but a one-shot ``pionir route ...`` process exits
        before the background thread ever finishes, so pressure could never defer
        anything from the CLI - the first read was always neutral. This does the
        read on the calling thread instead, bounded by the reader's own timeout,
        and caches it so a following ``peek`` sees it. Still fail-open."""

        self._refresh()
        return self.peek()

    def _refresh(self) -> None:
        reading = neutral("refresh interrupted")
        try:
            reading = parse_vitals(self._runner.run(timeout_seconds=self._timeout))
        except Exception as error:  # noqa: BLE001 - fail open, but say so
            _log.info("Bryo vitals unreadable (%s); treating as neutral", type(error).__name__)
            reading = neutral(f"unreadable: {type(error).__name__}")
        finally:
            # store and release in one step: clearing the flag first would let a peek
            # in the gap see a stale read_at and start a duplicate refresh
            with self._lock:
                self._reading = reading
                self._read_at = self._clock()
                self._refreshing = False
