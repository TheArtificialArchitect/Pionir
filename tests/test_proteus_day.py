"""Day P/L: balance now minus the balance at today's open (America/New_York).

Nothing here reaches a live account: readers are injected, and the one real reader is pointed
at a stand-in server on an ephemeral port. What is pinned:

- the open is recorded once per trading day and NEVER overwritten (a later read, a restart);
- the day is the New York date and 09:30 is wall time, so a DST change moves nothing;
- a missing number, a weekend, before the open, after the close with no open, an unreadable
  state file: all "unknown" with a reason - never 0;
- the state file is written atomically (valid JSON at every step, no temp file left);
- the plane exposes it, and a plane built without a reader reads no account and writes nothing.
"""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock
from zoneinfo import ZoneInfo

from pionir import proteus_day
from pionir.adapters.proteus import ProteusAdapter, ProteusSettings
from pionir.proteus_day import DayOpenStore, Read, day_view, real_account_reader, session, value_of

NY = ZoneInfo("America/New_York")


def at(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


TUE = (2026, 9, 29)          # a Tuesday


class SessionTests(unittest.TestCase):
    def test_phases_and_the_new_york_date(self) -> None:
        self.assertEqual(session(at(*TUE, 9, 29)), ("2026-09-29", "pre-open"))
        self.assertEqual(session(at(*TUE, 9, 30)), ("2026-09-29", "open"))
        self.assertEqual(session(at(*TUE, 15, 59)), ("2026-09-29", "open"))
        self.assertEqual(session(at(*TUE, 16, 0)), ("2026-09-29", "closed"))
        self.assertEqual(session(at(2026, 10, 3, 11)), ("2026-10-03", "weekend"))

    def test_the_date_is_new_york_not_utc(self) -> None:
        # 22:30 ET on Tuesday is already Wednesday in UTC: still Tuesday's day
        self.assertEqual(session(at(*TUE, 22, 30))[0], "2026-09-29")
        # 00:30 ET Wednesday is still Tuesday in UTC's early hours: Wednesday's day
        self.assertEqual(session(at(2026, 9, 30, 0, 30))[0], "2026-09-30")

    def test_dst_changes_do_not_move_the_open(self) -> None:
        # spring forward Sun 2026-03-08: Mon 09:30 EDT is 13:30 UTC (it was 14:30 UTC the week before)
        mon_after = at(2026, 3, 9, 9, 30)
        self.assertEqual(datetime.fromtimestamp(mon_after, tz=ZoneInfo("UTC")).hour, 13)
        self.assertEqual(session(mon_after), ("2026-03-09", "open"))
        self.assertEqual(session(mon_after - 60)[1], "pre-open")
        # fall back Sun 2026-11-01: Mon 09:30 EST is 14:30 UTC
        mon_fall = at(2026, 11, 2, 9, 30)
        self.assertEqual(datetime.fromtimestamp(mon_fall, tz=ZoneInfo("UTC")).hour, 14)
        self.assertEqual(session(mon_fall), ("2026-11-02", "open"))
        self.assertEqual(session(mon_fall - 60)[1], "pre-open")
        # the fall-back Friday 16:00 EDT close, and the Monday after, are separate days
        self.assertNotEqual(session(at(2026, 10, 30, 12))[0], session(mon_fall)[0])


class DayViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "proteus-day-open.json"
        self.store = DayOpenStore(self.path)

    def _view(self, ts, prometheus=None, robinhood=None):
        reads = {}
        if prometheus is not None:
            reads["prometheus"] = prometheus
        if robinhood is not None:
            reads["robinhood"] = robinhood
        return day_view(self.store, reads, ts)

    def test_the_first_good_read_after_the_open_is_the_open_and_it_stays(self) -> None:
        first = self._view(at(*TUE, 9, 31), prometheus=Read(10_000_000))
        self.assertEqual(first["prometheus"]["pl_cents"], 0)
        self.assertEqual(first["prometheus"]["state"], "known")
        self.assertNotIn("since", first["prometheus"])        # taken at the open: no caveat
        later = self._view(at(*TUE, 11), prometheus=Read(10_123_400))
        self.assertEqual(later["prometheus"]["open_cents"], 10_000_000)      # NEVER overwritten
        self.assertEqual(later["prometheus"]["pl_cents"], 123_400)
        lower = self._view(at(*TUE, 14), prometheus=Read(9_900_000))
        self.assertEqual(lower["prometheus"]["pl_cents"], -100_000)           # a loss stays a loss
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(doc["days"]["2026-09-29"]["prometheus"]["open_cents"], 10_000_000)

    def test_a_restart_keeps_the_days_open(self) -> None:
        self._view(at(*TUE, 9, 32), prometheus=Read(5_000_000))
        restarted = DayOpenStore(self.path)          # a new process: same file
        view = day_view(restarted, {"prometheus": Read(5_010_000)}, at(*TUE, 13))
        self.assertEqual((view["prometheus"]["open_cents"], view["prometheus"]["pl_cents"]),
                         (5_000_000, 10_000))

    def test_an_open_is_never_overwritten_by_record_itself(self) -> None:
        self.store.record("2026-09-29", "prometheus", 100, at(*TUE, 9, 31))
        kept = self.store.record("2026-09-29", "prometheus", 999, at(*TUE, 12))
        self.assertEqual(kept["open_cents"], 100)
        self.assertEqual(json.loads(self.path.read_text())["days"]["2026-09-29"]["prometheus"]["open_cents"], 100)

    def test_a_new_day_opens_fresh_and_yesterdays_open_is_never_used(self) -> None:
        self._view(at(*TUE, 9, 40), prometheus=Read(1_000_000))
        wed_pre = self._view(at(2026, 9, 30, 8, 0), prometheus=Read(1_050_000))
        self.assertEqual(wed_pre["prometheus"]["state"], "unknown")          # before Wednesday's open
        self.assertIn("before the 09:30", wed_pre["prometheus"]["why"])
        wed = self._view(at(2026, 9, 30, 9, 45), prometheus=Read(1_050_000))
        self.assertEqual(wed["prometheus"]["open_cents"], 1_050_000)
        self.assertEqual(wed["prometheus"]["pl_cents"], 0)
        days = json.loads(self.path.read_text())["days"]
        self.assertEqual(set(days), {"2026-09-29", "2026-09-30"})

    def test_unknown_is_never_zero(self) -> None:
        cases = {
            "before the open": (at(*TUE, 9, 0), Read(1_000_000), "before the 09:30"),
            "weekend": (at(2026, 10, 3, 12), Read(1_000_000), "weekend"),
            "no open recorded and the session is over": (at(*TUE, 17), Read(1_000_000), "no open balance"),
            "no read now": (at(*TUE, 10), Read(None, "the SSH tunnel is down"), "tunnel is down"),
            "not read at all": (at(*TUE, 10), None, "not read"),
        }
        for name, (ts, read, words) in cases.items():
            view = self._view(ts, prometheus=read)["prometheus"]
            self.assertEqual(view["state"], "unknown", name)
            self.assertNotIn("pl_cents", view, name)
            self.assertIn(words, view["why"], name)
        self.assertFalse(self.path.exists())          # none of those recorded an open

    def test_a_missing_now_does_not_erase_or_replace_the_open(self) -> None:
        self._view(at(*TUE, 9, 31), prometheus=Read(2_000_000))
        view = self._view(at(*TUE, 12), prometheus=Read(None, "tunnel down"))["prometheus"]
        self.assertEqual(view["state"], "unknown")
        after = self._view(at(*TUE, 13), prometheus=Read(2_020_000))["prometheus"]
        self.assertEqual((after["open_cents"], after["pl_cents"]), (2_000_000, 20_000))

    def test_a_late_first_read_is_recorded_and_says_since_when(self) -> None:
        view = self._view(at(*TUE, 12, 3), prometheus=Read(3_000_000))["prometheus"]
        self.assertEqual(view["state"], "known")
        self.assertEqual(view["since"], "12:03 ET")
        # and the same day's later read keeps that caveat
        again = self._view(at(*TUE, 14), prometheus=Read(3_001_000))["prometheus"]
        self.assertEqual((again["pl_cents"], again["since"]), (1_000, "12:03 ET"))

    def test_the_accounts_are_recorded_independently(self) -> None:
        self._view(at(*TUE, 9, 31), prometheus=Read(1_000_000))
        both = self._view(at(*TUE, 10), prometheus=Read(1_001_000), robinhood=Read(500_000))
        self.assertEqual(both["prometheus"]["pl_cents"], 1_000)
        self.assertEqual(both["robinhood"]["pl_cents"], 0)             # its own open, taken now
        self.assertEqual(both["robinhood"]["since"], "10:00 ET")
        self.assertEqual(self._view(at(*TUE, 11))["robinhood"]["state"], "unknown")

    def test_an_unreadable_file_is_unknown_set_aside_and_never_silently_reused(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json", encoding="utf-8")
        first = self._view(at(*TUE, 10), prometheus=Read(1_000_000))["prometheus"]
        self.assertEqual(first["state"], "unknown")
        self.assertIn("unreadable", first["why"])
        self.assertTrue(self.path.with_suffix(".json.corrupt").exists())
        second = self._view(at(*TUE, 10, 2), prometheus=Read(1_000_000))["prometheus"]
        self.assertEqual(second["state"], "known")
        self.assertEqual(second["since"], "10:02 ET")                  # says it is not the open

    def test_a_failed_set_aside_is_logged_and_not_reported_as_done(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json", encoding="utf-8")
        with mock.patch.object(proteus_day.atomic, "replace", side_effect=PermissionError("locked")),                 self.assertLogs("pionir.proteus_day", "WARNING") as logged:
            got = self._view(at(*TUE, 10), prometheus=Read(1_000_000))["prometheus"]
        self.assertEqual(got["state"], "unknown")
        self.assertIn("could NOT be set aside", got["why"])
        self.assertNotIn("was set aside", got["why"])
        self.assertIn("could not be set aside", logged.output[0])
        self.assertTrue(self.path.exists())                             # it is still there
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ not json")   # never overwritten

    def test_a_file_that_cannot_be_read_is_never_overwritten(self) -> None:
        self._view(at(*TUE, 9, 31), prometheus=Read(7_000_000))
        real = Path.read_text

        def locked(path, *a, **k):
            if path == self.path:
                raise PermissionError("held by a scanner")
            return real(path, *a, **k)

        with mock.patch.object(Path, "read_text", locked):
            view = self._view(at(*TUE, 12), prometheus=Read(7_100_000))["prometheus"]
        self.assertEqual(view["state"], "unknown")
        after = self._view(at(*TUE, 13), prometheus=Read(7_100_000))["prometheus"]
        self.assertEqual(after["open_cents"], 7_000_000)                # the open survived

    def test_record_itself_refuses_to_write_over_a_file_it_could_not_read(self) -> None:
        self.store.record("2026-09-29", "prometheus", 100, at(*TUE, 9, 31))
        before = self.path.read_text(encoding="utf-8")
        real = Path.read_text

        def locked(path, *a, **k):
            if path == self.path:
                raise PermissionError("held by a scanner")
            return real(path, *a, **k)

        with mock.patch.object(Path, "read_text", locked), self.assertRaises(OSError):
            self.store.record("2026-09-29", "robinhood", 555, at(*TUE, 12))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_two_readers_at_once_cannot_both_write_the_days_open(self) -> None:
        import time as _t

        from pionir import atomic
        store = DayOpenStore(Path(self._tmp.name) / "day.json")
        real = atomic.write_text

        def slow(path, text):
            _t.sleep(0.25)                                  # a slow disk: the other reader arrives mid-write
            return real(path, text)

        got = {}

        def go(name, cents):
            got[name] = store.record("2026-09-29", "prometheus", cents, at(*TUE, 10))["open_cents"]

        with mock.patch.object(atomic, "write_text", slow):
            a = threading.Thread(target=go, args=("a", 1_000_000))
            b = threading.Thread(target=go, args=("b", 2_000_000))
            a.start()
            _t.sleep(0.05)
            b.start()
            a.join()
            b.join()
        self.assertEqual(got["a"], got["b"])                # one open stands for both
        self.assertEqual(store.opened("2026-09-29", "prometheus")["open_cents"], got["a"])

    def test_the_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        seen = []
        real_replace = proteus_day.atomic.replace

        def spy(tmp, dest):
            seen.append(json.loads(Path(tmp).read_text(encoding="utf-8")))     # complete before the swap
            real_replace(tmp, dest)

        with mock.patch.object(proteus_day.atomic, "replace", spy):
            self._view(at(*TUE, 9, 31), prometheus=Read(4_000_000))
        self.assertEqual(len(seen), 1)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["proteus-day-open.json"])

    def test_old_days_are_pruned(self) -> None:
        for offset in range(12):
            ts = at(2026, 9, 1 + offset, 10)
            if session(ts)[1] == "open":
                self._view(ts, prometheus=Read(1_000_000 + offset))
        self.assertLessEqual(len(json.loads(self.path.read_text())["days"]), proteus_day.KEEP_DAYS)


class ValueTests(unittest.TestCase):
    def test_prometheus_status_and_robinhood_portfolio(self) -> None:
        self.assertEqual(value_of("prometheus", {"equity": "100234.56", "buying_power": 5}), Read(10_023_456))
        self.assertEqual(value_of("robinhood", {"total_value": 2500.5, "buying_power": 1, "positions": []}),
                         Read(250_050))

    def test_soft_failures_and_empty_books_are_unknown_not_zero(self) -> None:
        bad = [
            ("prometheus", {"error": "broker unreachable"}),
            ("robinhood", {"note": "session needs reconnect", "total_value": 0, "buying_power": 0}),
            ("prometheus", {"equity": 0, "buying_power": 0, "position_count": 0}),
            ("robinhood", {"total_value": 0, "buying_power": 0, "positions": []}),
            ("prometheus", {"equity": None}),
            ("prometheus", {"equity": True}),
            ("prometheus", {"equity": "n/a"}),
            ("prometheus", {"equity": float("nan")}),
            ("prometheus", {"equity": -5}),
            ("prometheus", []),
        ]
        for account, body in bad:
            got = value_of(account, body)
            self.assertIsNone(got.cents, (account, body))
            self.assertTrue(got.why, (account, body))
        # an all-zero book is called out as such, not just "no usable value"
        for account, body in (("prometheus", {"equity": 0, "buying_power": 0, "position_count": 0}),
                              ("robinhood", {"total_value": 0, "buying_power": 0, "positions": []})):
            self.assertIn("empty book", value_of(account, body).why)

    def test_a_note_alone_is_a_soft_failure_even_with_a_nonzero_value(self) -> None:
        # not covered by the empty-book rule: a real-looking number carrying a "note" is not trusted
        self.assertIsNone(value_of("robinhood", {"total_value": 5000.0, "buying_power": 10, "positions": [],
                                                 "note": "session needs reconnect"}).cents)
        self.assertIsNone(value_of("prometheus", {"equity": 5000.0, "buying_power": 10, "position_count": 1,
                                                  "note": "stale"}).cents)

    def test_a_zero_equity_with_positions_is_a_real_answer_shape_but_not_a_usable_value(self) -> None:
        self.assertIsNone(value_of("prometheus", {"equity": 0, "buying_power": 0, "position_count": 3}).cents)


class _Api(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []
    bodies: ClassVar[dict] = {}

    def do_GET(self):
        _Api.seen.append((self.path, self.headers.get("x-api-key")))
        if self.headers.get("x-api-key") != "the-read-key":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(_Api.bodies.get(self.path, {"error": "no route"})).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        _Api.seen.append(("POST " + self.path, None))
        self.send_response(405)
        self.end_headers()

    def log_message(self, *args):
        pass


SSH = lambda port: "ssh.exe"


class RealReaderTests(unittest.TestCase):
    """The real reader against a stand-in server: GET only, the read key in x-api-key."""

    def setUp(self) -> None:
        _Api.seen, _Api.bodies = [], {"/status": {"equity": "99000.00", "buying_power": "1", "position_count": 2},
                                      "/portfolio": {"total_value": 1234.5, "buying_power": 10, "positions": []}}
        self.server = HTTPServer(("127.0.0.1", 0), _Api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.secrets = Path(self._tmp.name)

    def _routes(self):
        return mock.patch.object(proteus_day, "READ_ROUTES", (
            ("prometheus", self.port, "/status", "prometheus-read-key.txt"),
            ("robinhood", self.port, "/portfolio", "proteus-read-key.txt")))

    def test_it_reads_both_accounts_with_get_and_the_key(self) -> None:
        (self.secrets / "prometheus-read-key.txt").write_text("the-read-key\n")
        (self.secrets / "proteus-read-key.txt").write_text("the-read-key")
        with self._routes():
            got = real_account_reader(self.secrets, owner_of=SSH)()
        self.assertEqual(got, {"prometheus": Read(9_900_000), "robinhood": Read(123_450)})
        self.assertEqual(sorted(_Api.seen), [("/portfolio", "the-read-key"), ("/status", "the-read-key")])

    def test_a_missing_key_sends_nothing_and_says_so(self) -> None:
        with self._routes():
            got = real_account_reader(self.secrets, owner_of=SSH)()
        self.assertEqual(_Api.seen, [])
        self.assertIsNone(got["prometheus"].cents)
        self.assertIn("no read key", got["prometheus"].why)

    def test_a_wrong_key_and_a_dead_port_are_unknown(self) -> None:
        (self.secrets / "prometheus-read-key.txt").write_text("wrong")
        (self.secrets / "proteus-read-key.txt").write_text("wrong")
        with self._routes():
            got = real_account_reader(self.secrets, owner_of=SSH)()
        self.assertIn("401", got["prometheus"].why)
        self.server.shutdown()
        self.server.server_close()
        with self._routes():
            dead = real_account_reader(self.secrets, owner_of=SSH)()
        self.assertIsNone(dead["robinhood"].cents)
        self.assertIn("tunnel", dead["robinhood"].why)

    def test_a_redirect_is_not_followed_so_the_key_goes_nowhere_else(self) -> None:
        other = HTTPServer(("127.0.0.1", 0), _Api)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)

        class _Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/status")
                self.end_headers()

            def log_message(self, *args):
                pass

        hop = HTTPServer(("127.0.0.1", 0), _Redirect)
        threading.Thread(target=hop.serve_forever, daemon=True).start()
        self.addCleanup(hop.server_close)
        self.addCleanup(hop.shutdown)
        (self.secrets / "prometheus-read-key.txt").write_text("the-read-key")
        (self.secrets / "proteus-read-key.txt").write_text("the-read-key")
        with mock.patch.object(proteus_day, "READ_ROUTES", (
                ("prometheus", hop.server_address[1], "/status", "prometheus-read-key.txt"),)):
            got = real_account_reader(self.secrets, owner_of=SSH)()
        self.assertIsNone(got["prometheus"].cents)
        self.assertIn("302", got["prometheus"].why)
        self.assertEqual(_Api.seen, [])                     # the other server never saw the key

    def test_the_key_is_sent_only_to_a_port_the_ssh_tunnel_holds(self) -> None:
        (self.secrets / "prometheus-read-key.txt").write_text("the-read-key")
        (self.secrets / "proteus-read-key.txt").write_text("the-read-key")
        cases = {"python.exe": "is held by python.exe", "ssh.exe,python.exe": "is held by",
                 None: "cannot tell who holds"}
        for owner, words in cases.items():
            _Api.seen.clear()
            with self._routes():
                got = real_account_reader(self.secrets, owner_of=lambda port, o=owner: o)()
            self.assertEqual(_Api.seen, [], owner)            # nothing - not even the key - was sent
            for account in ("prometheus", "robinhood"):
                self.assertIsNone(got[account].cents)
                self.assertIn(words, got[account].why, owner)
        with self._routes():                                   # nothing listening = tunnel down
            got = real_account_reader(self.secrets, owner_of=lambda port: "")()
        self.assertEqual(_Api.seen, [])
        self.assertIn("tunnel is down", got["prometheus"].why)

    def test_only_get_routes_exist_in_the_table(self) -> None:
        self.assertEqual({(r[0], r[2]) for r in proteus_day.READ_ROUTES},
                         {("prometheus", "/status"), ("robinhood", "/portfolio")})


class _Run:
    """A stand-in for subprocess.run: netstat/tasklist answers by command."""

    def __init__(self, netstat: str, tasks: dict[str, str], net_code: int = 0) -> None:
        self.netstat, self.tasks, self.net_code, self.calls = netstat, tasks, net_code, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "netstat":
            return mock.Mock(returncode=self.net_code, stdout=self.netstat)
        pid = argv[2].split()[-1]
        image = self.tasks.get(pid)
        out = f'"{image}","{pid}","Console","1","9,000 K"\n' if image else "INFO: No tasks are running\n"
        return mock.Mock(returncode=0, stdout=out)


NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900
  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:18000        127.0.0.1:50000        ESTABLISHED     4242
  TCP    127.0.0.1:18001        0.0.0.0:0              LISTENING       5151
  TCP    0.0.0.0:18001          0.0.0.0:0              LISTENING       6161
  TCP    [::]:18002             [::]:0                 LISTENING       7171
  UDP    0.0.0.0:18003          *:*                                    8181
"""


class ListenerOwnerTests(unittest.TestCase):
    def owner(self, port, run):
        return proteus_day.listener_owner(port, run=run, platform="win32")

    def test_it_names_the_process_that_holds_the_port(self) -> None:
        run = _Run(NETSTAT, {"4242": "ssh.exe", "5151": "ssh.exe", "6161": "node.exe", "7171": "ssh.exe"})
        self.assertEqual(self.owner(18000, run), "ssh.exe")
        self.assertEqual(self.owner(18001, run), "node.exe,ssh.exe")     # EVERY listener counts
        self.assertEqual(self.owner(18002, run), "ssh.exe")               # an IPv6 listener too
        self.assertEqual(self.owner(18003, run), "")                      # UDP is not a listener
        self.assertEqual(self.owner(19999, run), "")                      # nothing there
        self.assertTrue(all(c[0] in ("netstat", "tasklist") for c in run.calls))   # read-only

    def test_it_says_unknown_rather_than_guess(self) -> None:
        self.assertIsNone(self.owner(18000, _Run(NETSTAT, {}, net_code=1)))       # netstat failed
        self.assertIsNone(self.owner(18000, _Run(NETSTAT, {})))                   # the pid vanished
        self.assertIsNone(proteus_day.listener_owner(18000, platform="linux"))    # not Windows

        def boom(argv, **kw):
            raise OSError("no netstat")
        self.assertIsNone(self.owner(18000, boom))

    def test_the_reader_wires_it_in_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "prometheus-read-key.txt").write_text("k")
            (Path(tmp) / "proteus-read-key.txt").write_text("k")
            reader = real_account_reader(Path(tmp))
            with mock.patch.object(proteus_day, "sys", mock.Mock(platform="linux")):
                got = reader()
        # off Windows the holder cannot be told: nothing is sent and both say why
        self.assertIn("cannot tell who holds", got["prometheus"].why)
        self.assertIn("cannot tell who holds", got["robinhood"].why)


class PlaneTests(unittest.TestCase):
    def _adapter(self, tmp, reader, clock):
        from pionir.adapters._proc import ProcessResult
        return ProteusAdapter(
            ProteusSettings(host="vps.test", key_file=Path("C:/keys/k")),
            runner=lambda argv, timeout: ProcessResult(returncode=0, stdout="", stderr=""),
            peter_health=lambda: True, tunnel_health=lambda port: "down",
            accounts=reader, day_open_file=Path(tmp) / "day.json", clock=clock)

    def test_the_plane_carries_day_pl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [at(*TUE, 9, 35)]
            value = [Read(1_000_000)]
            adapter = self._adapter(tmp, lambda: {"prometheus": value[0], "robinhood": Read(None, "no read key")},
                                    lambda: now[0])
            first = adapter.plane()["day_pl"]
            self.assertTrue(first["enabled"])
            self.assertEqual(first["zone"], "America/New_York")
            self.assertEqual(first["accounts"]["prometheus"]["pl_cents"], 0)
            self.assertEqual(first["accounts"]["robinhood"]["state"], "unknown")
            now[0], value[0] = at(*TUE, 12), Read(1_002_500)
            self.assertEqual(adapter.plane()["day_pl"]["accounts"]["prometheus"]["pl_cents"], 2_500)

    def test_a_reader_that_raises_is_unknown_and_does_not_sink_the_plane(self) -> None:
        def boom():
            raise RuntimeError("tunnel exploded")

        with tempfile.TemporaryDirectory() as tmp:
            plane = self._adapter(tmp, boom, lambda: at(*TUE, 10)).plane()
            accounts = plane["day_pl"]["accounts"]
            self.assertEqual({a["state"] for a in accounts.values()}, {"unknown"})
            self.assertIn("vps", plane)

    def test_a_day_view_that_raises_is_unknown_and_does_not_sink_the_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(tmp, lambda: {"prometheus": Read(1_000_000)}, lambda: at(*TUE, 10))
            with mock.patch("pionir.adapters.proteus.day_view", side_effect=RuntimeError("zone data missing")):
                plane = adapter.plane()
            accounts = plane["day_pl"]["accounts"]
            self.assertEqual({a["state"] for a in accounts.values()}, {"unknown"})
            self.assertTrue(all(a["why"] for a in accounts.values()))
            self.assertIn("vps", plane)
            self.assertIn("tailnet", plane)                 # the alarms ride the same document

    def test_without_a_reader_no_account_is_read_and_nothing_is_written(self) -> None:
        from pionir.adapters._proc import ProcessResult
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ProteusAdapter(
                ProteusSettings(host="vps.test", key_file=Path("C:/keys/k")),
                runner=lambda argv, timeout: ProcessResult(returncode=0, stdout="", stderr=""),
                peter_health=lambda: True, tunnel_health=lambda port: "down")
            self.assertEqual(adapter.plane()["day_pl"], {"enabled": False})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_bootstrap_wires_the_real_reader_and_the_state_file(self) -> None:
        """Wired, not inert: the runtime's Proteus adapter carries the reader and the file."""
        from standins import down_url

        from pionir.bootstrap import build_runtime
        from pionir.config import PionirSettings
        with tempfile.TemporaryDirectory() as tmp:
            runtime = build_runtime(PionirSettings(
                state_root=Path(tmp), atani_command=("pionir-test-no-such-binary",),
                galatea_url=down_url(), galatea_model_id="stub-model", embed_model=None,
                daedalus_url=down_url(), melete_url=down_url(), crew_url=None,
                bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
                evict_to_fit=False, proteus_host="vps.test"))
            try:
                adapter = runtime.adapters["proteus"]
                self.assertIsNotNone(adapter._accounts)
                self.assertEqual(adapter._day_store.path, Path(tmp) / "state" / "proteus-day-open.json")
            finally:
                runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
