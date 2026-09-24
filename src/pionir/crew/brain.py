"""The one brain: a shared language organ behind a single queue.

Stateless between calls. It receives a fully assembled situation built from
ONE agent's own store and returns words. It keeps no memory and no
personality; the queue knows who is waiting and for how long, whether the
model is resident (warm) or evicted (cold), and how many calls the crew has
made this hour against the ceiling. Everything it does is counted and shown.

One worker, one call at a time: the model is shared with Moss, and a second
concurrent crew call would only queue inside Ollama where nobody can see it.

Pause holds the brain: while the crew is paused the worker makes no model call at
all; queued work keeps its place and goes when the crew resumes.

A queued request does not wait for ever. One older than ``request_ttl_seconds``
(default 600) is EXPIRED: counted (``expired``), logged, and its callback is
invoked with ``err == EXPIRED`` and ``meta["expired"] is True`` - so the agent
knows the thought did not happen. It is never silently dropped, and a stale
thought is never spoken as if it were fresh.

Before every call the worker looks at Pionir's GPU lock and, if anybody holds
it, stands down until it frees (gpu.py) - it never takes the lock itself. A
yield is not a failure and costs nothing against the ceiling; the request
simply waits at the head of the queue and goes as soon as the card is free.
"""
from __future__ import annotations

import collections
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .gpu import CardWatch
from .log import lesion, log

TIMEOUT_S = 150

# (url, body, timeout) -> the decoded JSON reply. Injectable so no test ever
# reaches a real model.
Post = Callable[[str, dict, float], dict]

REPLY = 0        # a reply owed in a live conversation
BACKGROUND = 1   # everything else

EXPIRED = "expired"          # the err a callback gets when its request went stale
DEFAULT_TTL_S = 600.0
PAUSE_POLL_S = 0.25


class OllamaError(RuntimeError):
    pass


def http_post_json(url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    http = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(http, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise OllamaError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise OllamaError(str(exc)) from exc


class Request:
    __slots__ = (
        "agent_id",
        "callback",
        "enqueued_real",
        "fmt",
        "id",
        "messages",
        "options",
        "priority",
        "purpose",
        "t",
    )

    def __init__(self, rid, agent_id, purpose, messages, options, callback, priority, t,
                 fmt=None, enqueued_real=None):
        self.id = rid
        self.agent_id = agent_id
        self.purpose = purpose
        self.messages = messages
        self.options = options
        self.callback = callback
        self.priority = priority          # REPLY goes ahead of BACKGROUND
        self.enqueued_real = time.time() if enqueued_real is None else enqueued_real
        self.t = t
        self.fmt = fmt


class Brain:
    def __init__(self, cfg, sim, *, card: CardWatch | None = None,
                 post: Post = http_post_json, now: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.sim = sim
        self.url = cfg.ollama_url.rstrip("/")
        self.model = cfg.model
        self.card = card or CardWatch(cfg.gpu_lock_path, poll_seconds=cfg.gpu_poll_seconds)
        self._post = post
        self._now = now
        self.ttl = float(getattr(cfg, "request_ttl_seconds", DEFAULT_TTL_S))
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pionir-crew-brain", daemon=True)
        self._next_id = 1
        self.busy_with: Request | None = None
        self.busy_since: float = 0.0
        self.calls = 0
        self.failures = 0
        self.throttled = 0
        self.last_latency = 0.0
        self.last_prompt_tokens = 0
        self.last_error: str | None = None
        self.recent_latencies: collections.deque = collections.deque(maxlen=20)
        self.reloads = 0                  # calls that had to wait for the model to load back onto the card
        self.reload_seconds = 0.0         # total time spent waiting for it to come back
        self.last_summary_call = 0        # so the log shows the denominator, not just the bad news
        self.expired = 0                  # requests that went stale in the queue
        self.held_for_pause = 0           # worker turns spent holding while the crew was paused

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    # ---- API for the crew ------------------------------------------------
    def ceiling_hit(self) -> bool:
        return self.sim.store.calls_last_hour() >= self.cfg.budget.calls_per_hour

    def request(self, agent_id: str, purpose: str, system, user, options: dict, callback,
                priority: int = BACKGROUND, fmt=None, messages: list | None = None) -> int | None:
        """Queue a call. ``messages`` (a full role list) wins over system+user. Returns the
        request id, or None if the hourly ceiling refuses it (the caller treats that as 'no
        words came')."""
        if messages is None:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.ceiling_hit():
            self.throttled += 1
            log.warning("brain: ceiling of %d calls/hour reached; refusing %s for %s",
                        self.cfg.budget.calls_per_hour, purpose, agent_id)
            return None
        with self._cv:
            rid = self._next_id
            self._next_id += 1
            req = Request(rid, agent_id, purpose, messages, options, callback, priority,
                          self.sim.clock.t, fmt, enqueued_real=self._now())
            self._q.append(req)
            self._cv.notify()
        return rid

    def cancel(self, rid: int) -> bool:
        with self._cv:
            for r in list(self._q):
                if r.id == rid:
                    self._q.remove(r)
                    return True
        return False

    def waiting(self) -> list:
        with self._cv:
            now = self._now()
            return [{"id": r.id, "agent": r.agent_id, "purpose": r.purpose,
                     "waited_s": round(now - r.enqueued_real, 1)} for r in self._q]

    # ---- the worker -------------------------------------------------------
    def _await_work(self) -> bool:
        """Block until something is queued; False if stopping."""
        with self._cv:
            while not self._q and not self._stop.is_set():
                self._cv.wait(0.5)
            return not self._stop.is_set()

    def _pop(self) -> Request | None:
        with self._cv:
            if not self._q:
                return None       # cancelled while we stood down
            # a reply owed inside a live conversation goes ahead of a solitary thought
            best = min(self._q, key=lambda r: (r.priority, r.enqueued_real))
            self._q.remove(best)
            return best

    def _paused(self) -> bool:
        return getattr(self.sim, "paused_reason", None) is not None

    def expire_stale(self) -> int:
        """Take every request older than the TTL out of the queue and tell its owner, with
        the explicit EXPIRED signal. Returns how many went."""
        now = self._now()
        with self._cv:
            stale = [r for r in self._q if now - r.enqueued_real > self.ttl]
            for r in stale:
                self._q.remove(r)
        for r in stale:
            self.expired += 1
            waited = now - r.enqueued_real
            log.warning("brain: %s for %s expired after %.0f s in the queue (ttl %.0f s); "
                        "it did not happen", r.purpose, r.agent_id, waited, self.ttl)
            try:
                self.sim.store.add_call(self.sim.clock.t, r.agent_id, r.purpose, None, None,
                                        0.0, False, f"{EXPIRED} after {waited:.0f} s")
            except Exception as exc:  # noqa: BLE001 - the ledger failing must not eat the signal
                lesion("brain.ledger", exc)
            try:
                with self.sim.lock:
                    r.callback(None, {"expired": True, "waited_s": round(waited, 1)}, EXPIRED)
            except Exception as exc:  # noqa: BLE001
                lesion(f"brain.callback.{r.purpose}", exc)
        return len(stale)

    def step(self) -> bool:
        """One turn of the worker: wait for work, expire what went stale, hold while the
        crew is paused, wait for the card, make one call. Returns False when stopping.
        The thread is only this in a loop, so tests drive it directly with no thread."""
        if not self._await_work():
            return False
        self.expire_stale()
        if self._paused():
            # pause holds the brain: no model call; queued work keeps its place
            self.held_for_pause += 1
            self._stop.wait(PAUSE_POLL_S)
            return True
        # Look at the lock with work in hand but BEFORE choosing it: a reply that
        # arrives while we stand down must still go ahead of an older thought.
        if not self.card.wait_until_free(self._stop, waiter="brain"):
            return False
        if self._paused():
            return True               # paused while we stood down for the card
        self.expire_stale()
        req = self._pop()
        if req is not None:
            self._serve(req)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.step():
                    return
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad turn
                lesion("brain.step", exc)
                self._stop.wait(1.0)

    def _serve(self, req: Request) -> None:
        self.busy_with = req
        self.busy_since = time.time()
        text, meta, err = None, {}, None
        t0 = time.time()
        try:
            text, meta = self._chat(req)
        except OllamaError as exc:
            err = str(exc)
            self.failures += 1
            self.last_error = err
            log.warning("brain: %s for %s failed: %s", req.purpose, req.agent_id, err)
        except Exception as exc:  # noqa: BLE001 - counted and logged, never silent
            err = f"{type(exc).__name__}: {exc}"
            self.failures += 1
            self.last_error = err
            lesion("brain.chat", exc)
        dt = time.time() - t0
        self.last_latency = dt
        if (meta.get("load_duration") or 0) > 2e9:
            self.reloads += 1
            waited = meta["load_duration"] / 1e9
            self.reload_seconds += waited
            # Say what happened, not just that something did. An evicted call that
            # then answered perfectly well is not a failure, and reading it as one is
            # what once made Hearth look like the house ignoring people.
            outcome = "and then answered" if err is None else f"and then FAILED: {err}"
            if self.reloads <= 3:
                log.info("brain: %s waited %.1f s for the model to load back onto the card %s. "
                         "Something else on this machine had taken the card; %s does not fit "
                         "beside it. Nothing was lost.",
                         req.agent_id, waited, outcome, self.cfg.model)
            else:
                log.info("brain: %s waited %.1f s for the card %s (reload %d of %d calls)",
                         req.agent_id, waited, outcome, self.reloads, self.calls + 1)
        self.recent_latencies.append(dt)
        self.calls += 1
        if self.calls - self.last_summary_call >= 50:
            self.last_summary_call = self.calls
            lat = sorted(self.recent_latencies)
            median = lat[len(lat) // 2] if lat else 0.0
            log.info("brain: %d calls so far - %d failed, %d waited for the card "
                     "(%.0f%%), %d stood down for the lock, median %.1f s. The rest were quiet.",
                     self.calls, self.failures, self.reloads,
                     100.0 * self.reloads / max(1, self.calls), self.card.yields, median)
        try:
            self.sim.store.add_call(self.sim.clock.t, req.agent_id, req.purpose,
                                    meta.get("prompt_eval_count"), meta.get("eval_count"),
                                    round(dt, 2), err is None, err)
        except Exception as exc:  # noqa: BLE001 - the ledger failing must not eat the reply
            lesion("brain.ledger", exc)
        try:
            # under the crew's lock, so a reply lands between ticks, never inside one
            with self.sim.lock:
                req.callback(text, meta, err)
        except Exception as exc:  # noqa: BLE001
            lesion(f"brain.callback.{req.purpose}", exc)
        self.busy_with = None

    def _chat(self, req: Request) -> tuple:
        body = {
            "model": self.model,
            "messages": req.messages,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_ctx": self.cfg.num_ctx, **req.options},
        }
        if req.fmt:
            body["format"] = req.fmt
        out = self._post(f"{self.url}/api/chat", body, TIMEOUT_S)
        msg = (out.get("message") or {}).get("content", "")
        meta = {k: out.get(k)
                for k in ("prompt_eval_count", "eval_count", "total_duration", "load_duration")}
        self.last_prompt_tokens = meta.get("prompt_eval_count") or 0
        return msg, meta

    # ---- for the viewer ---------------------------------------------------
    def snapshot(self) -> dict:
        mon = self.sim.monitor.latest if getattr(self.sim, "monitor", None) else {}
        avg = (sum(self.recent_latencies) / len(self.recent_latencies)
               if self.recent_latencies else 0.0)
        return {
            "model": self.model,
            "warm": bool(mon.get("brain_warm")) if mon else None,
            "vram_gb": mon.get("brain_vram_gb") if mon else None,
            "busy": {"agent": self.busy_with.agent_id, "purpose": self.busy_with.purpose,
                     "for_s": round(time.time() - self.busy_since, 1)} if self.busy_with else None,
            "queue": self.waiting(),
            "calls": self.calls, "failures": self.failures, "throttled": self.throttled,
            "reloads": self.reloads, "expired": self.expired, "ttl_s": self.ttl,
            "held": self._paused(),
            "calls_last_hour": self.sim.store.calls_last_hour(),
            "ceiling": self.cfg.budget.calls_per_hour,
            "last_latency_s": round(self.last_latency, 2), "avg_latency_s": round(avg, 2),
            "last_prompt_tokens": self.last_prompt_tokens, "last_error": self.last_error,
            "gpu": self.card.snapshot(),
        }
