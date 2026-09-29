"""``pionir work ...``: the work log from a terminal.

    pionir work start <job> [--note T] [--allow-concurrent]
    pionir work stop [job] [--end TIME] [--note T]
    pionir work status
    pionir work add <job> <start> <end> [--note T]
    pionir work log <job> <text> [--rubric]
    pionir work payout <job> <amount> [--note T] [--at TIME]
    pionir work summary [--week | --month]
    pionir work jobs [list | add <name> ... | edit <job> ...]

Plain text out, exit 0 on success and 1 on any error. It opens the work log directly (no
Pionir runtime, no server, no network, no GPU lock). Times without a zone are LOCAL time
(PIONIR_WORK_TZ, else the machine's); end them with Z or an offset for UTC. Amounts and rates
are dollars ("25", "1,200.50") and become integer cents - never floats.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any, TextIO

from .config import PionirSettings
from .worklog import (
    CONFIDENTIALITY,
    WorkError,
    WorkLog,
    parse_cents,
    worklog_for,
)

REMINDER = f"Reminder: {CONFIDENTIALITY}"


def add_parsers(commands: Any) -> None:
    work = commands.add_parser(
        "work", help="the work log: timers, hours and payouts (Data Annotation and the day job)",
        description=("Track hours and income across jobs. Nothing here does any annotation work. "
                     + CONFIDENTIALITY),
        epilog=REMINDER, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = work.add_subparsers(dest="work_command", required=True)

    start = sub.add_parser("start", help="start a timer for a job", epilog=REMINDER)
    start.add_argument("job")
    start.add_argument("--note", default="")
    start.add_argument("--allow-concurrent", action="store_true",
                       help="start even though another job's timer is running")

    stop = sub.add_parser("stop", help="stop the running timer", epilog=REMINDER)
    stop.add_argument("job", nargs="?", help="omit when exactly one timer is running")
    stop.add_argument("--end", help="an explicit end time (use it to close a forgotten timer)")
    stop.add_argument("--note")

    sub.add_parser("status", help="running timers and today's hours")

    add = sub.add_parser("add", help="add a finished session by hand",
                         description="Times without a zone are local time. "
                                     "Example: work add 'Day job' 2026-03-09T09:00 2026-03-09T17:30",
                         epilog=REMINDER)
    add.add_argument("job")
    add.add_argument("start")
    add.add_argument("end")
    add.add_argument("--note", default="")

    log = sub.add_parser("log", help="add a note to a job's log", epilog=REMINDER)
    log.add_argument("job")
    log.add_argument("text")
    log.add_argument("--rubric", action="store_true",
                     help="file it as a rubric note: your own general writing-quality tip")

    payout = sub.add_parser("payout", help="record money received for a job",
                            description="The amount is dollars, e.g. 480 or 1,200.50.", epilog=REMINDER)
    payout.add_argument("job")
    payout.add_argument("amount")
    payout.add_argument("--note", default="")
    payout.add_argument("--at", help="when it was paid (default now)")

    summary = sub.add_parser("summary", help="hours and earnings per job")
    span = summary.add_mutually_exclusive_group()
    span.add_argument("--week", action="store_true", help="show this week only")
    span.add_argument("--month", action="store_true", help="show this month only")

    jobs = sub.add_parser("jobs", help="list, add or edit jobs")
    jsub = jobs.add_subparsers(dest="jobs_command")
    jsub.add_parser("list", help="list jobs")
    jadd = jsub.add_parser("add", help="add a job")
    jadd.add_argument("name")
    _job_flags(jadd)
    jadd.set_defaults(kind="freelance")
    jedit = jsub.add_parser("edit", help="change a job (a rate is never guessed: set it here)")
    jedit.add_argument("job")
    jedit.add_argument("--name")
    _job_flags(jedit)
    jedit.set_defaults(kind=None)
    jedit.add_argument("--active", choices=("yes", "no"))
    jedit.add_argument("--clear-rate", action="store_true", help="forget the hourly rate")
    jedit.add_argument("--clear-set-aside", action="store_true", help="forget the set-aside percent")


def _job_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", choices=("freelance", "employment"), default=argparse.SUPPRESS)
    p.add_argument("--rate", help="hourly rate in dollars, e.g. 25 or 22.50 (unset means hours only)")
    p.add_argument("--currency")
    p.add_argument("--set-aside", type=int, dest="set_aside",
                   help="percent to set aside, shown as an informational number only")


# ---------------------------------------------------------------------------- output

def money(cents: int | None, currency: str = "USD") -> str:
    if cents is None:
        return "rate not set"
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole:,}.{frac:02d} {currency}"


def hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _period_line(label: str, p: dict[str, Any], currency: str) -> str:
    earned = money(p["earned_cents"], currency)
    text = f"  {label:<6} {p['hours']:>7.2f} h   {earned}"
    if p.get("payout_cents"):
        text += f"   paid {money(p['payout_cents'], currency)}"
    return text


def _summary(log: WorkLog, span: str | None, out: Callable[[str], None]) -> None:
    s = log.summary()
    names = (span,) if span else ("today", "week", "month", "year")
    out(f"Work summary ({s['tz']}), as of {s['generated_at']}")
    if not s["jobs"]:
        out("No jobs yet. Add one: pionir work jobs add \"Data Annotation\" --rate 25")
    for job in s["jobs"]:
        flag = "  [timer running]" if job["timer_running"] else ""
        state = "" if job["active"] else "  (inactive)"
        out(f"{job['name']} ({job['kind']}){flag}{state}")
        for name in names:
            out(_period_line(name, job["periods"][name], job["currency"]))
        if span is None and job["kind"] == "freelance":
            eff = job["periods"]["all"].get("effective_hourly_cents")
            if eff is not None:
                out(f"  effective hourly (all payouts / hours): {money(eff, job['currency'])}")
        pct = job["set_aside_pct"]
        base = job["periods"][span or "week"]
        if pct is not None and "set_aside_cents" in base:
            out(f"  set aside {pct}% of this {span or 'week'}: "
                f"{money(base['set_aside_cents'], job['currency'])} (informational, not tax advice)")
    if len(s["jobs"]) > 1:
        out("Total")
        for name in names:
            t = s["totals"][name]
            earned = ", ".join(money(v, c) for c, v in sorted(t["earned_cents_by_currency"].items()))
            extra = f"   ({hms(t['unrated_seconds'])} at no rate)" if t["unrated_seconds"] else ""
            out(f"  {name:<6} {t['hours']:>7.2f} h   {earned or 'rate not set'}{extra}")
    for t in s["timers"]:
        note = "  FORGOTTEN? over 12h - not counted; close it with: pionir work stop --end TIME" \
            if t["flagged"] else ""
        out(f"Timer running: {t['job']} since {t['start_ts']} ({hms(t['elapsed_seconds'])}){note}")


def _status(log: WorkLog, out: Callable[[str], None]) -> None:
    s = log.summary()
    if not s["timers"]:
        out("No timer running.")
    for t in s["timers"]:
        flag = "  FLAGGED: open over 12h, not counted" if t["flagged"] else ""
        out(f"Running: {t['job']}  {hms(t['elapsed_seconds'])}  (since {t['start_ts']}){flag}")
    for job in s["jobs"]:
        p = job["periods"]["today"]
        out(f"Today {job['name']}: {p['hours']:.2f} h   {money(p['earned_cents'], job['currency'])}")


# ---------------------------------------------------------------------------- dispatch

def run(args: argparse.Namespace, settings: PionirSettings, *, out: TextIO | None = None,
        err: TextIO | None = None, clock: Any = None) -> int:
    err = err or sys.stderr

    def say(line: str) -> None:
        print(line, file=out or sys.stdout)

    log = worklog_for(settings, clock=clock)
    try:
        return _run(args, log, say)
    except WorkError as error:
        print(f"error: {error}", file=err)
        return 1
    except (OSError, ValueError) as error:
        # sqlite3.Error is an Exception, not an OSError: name it without a stack trace.
        print(f"error: {type(error).__name__}: {error}", file=err)
        return 1
    except Exception as error:  # sqlite3.Error and friends: one line, no trace
        if type(error).__module__.startswith("sqlite3"):
            print(f"error: the work log could not be read ({type(error).__name__})", file=err)
            return 1
        raise


def _rate(text: str | None) -> int | None:
    return None if text is None else parse_cents(text, what="rate")


def _run(args: argparse.Namespace, log: WorkLog, say: Callable[[str], None]) -> int:
    cmd = args.work_command
    if cmd == "start":
        s = log.start_timer(args.job, note=args.note, allow_concurrent=args.allow_concurrent)
        say(f"Started {s['job']} at {s['start_ts']}")
    elif cmd == "stop":
        job = args.job
        if job is None:
            open_ = [t for t in log.summary()["timers"]]
            if len(open_) != 1:
                raise WorkError("bad_input", "no timer is running" if not open_
                                else "several timers are running: name the job")
            job = open_[0]["job_id"]
        kw: dict[str, Any] = {"end": args.end}
        if args.note is not None:
            kw["note"] = args.note
        s = log.stop_timer(job, **kw)
        say(f"Stopped {s['job']}: {hms(s['seconds'])} ({s['start_ts']} to {s['end_ts']})")
    elif cmd == "status":
        _status(log, say)
    elif cmd == "add":
        s = log.add_session(args.job, args.start, args.end, args.note)
        say(f"Added {s['job']}: {hms(s['seconds'])} ({s['start_ts']} to {s['end_ts']}), session {s['id']}")
    elif cmd == "log":
        e = log.add_log(args.job, "rubric" if args.rubric else "note", args.text)
        say(f"Logged {e['kind']} {e['id']} for job {args.job}")
    elif cmd == "payout":
        e = log.add_log(args.job, "payout", args.note, amount_cents=parse_cents(args.amount),
                        ts=args.at)
        say(f"Recorded payout {money(e['amount_cents'])} for {args.job} (entry {e['id']})")
    elif cmd == "summary":
        _summary(log, "week" if args.week else "month" if args.month else None, say)
    elif cmd == "jobs":
        return _jobs(args, log, say)
    return 0


def _jobs(args: argparse.Namespace, log: WorkLog, say: Callable[[str], None]) -> int:
    sub = args.jobs_command or "list"
    if sub == "list":
        jobs = log.list_jobs()
        if not jobs:
            say("No jobs yet.")
        for j in jobs:
            rate = money(j["hourly_rate_cents"], j["currency"]) + "/h" \
                if j["hourly_rate_cents"] is not None else "rate not set"
            say(f"{j['id']}  {j['name']}  {j['kind']}  {rate}"
                f"{'' if j['active'] else '  (inactive)'}")
    elif sub == "add":
        j = log.create_job(args.name, getattr(args, "kind", "freelance"),
                           hourly_rate_cents=_rate(args.rate), currency=args.currency or "USD",
                           set_aside_pct=args.set_aside)
        say(f"Added job {j['id']}: {j['name']} ({j['kind']}, "
            f"{money(j['hourly_rate_cents'], j['currency'])})")
    else:
        fields: dict[str, Any] = {}
        if args.name is not None:
            fields["name"] = args.name
        if getattr(args, "kind", None):
            fields["kind"] = args.kind
        if args.currency:
            fields["currency"] = args.currency
        if args.rate is not None and args.clear_rate:
            raise WorkError("bad_input", "give --rate or --clear-rate, not both")
        if args.set_aside is not None and args.clear_set_aside:
            raise WorkError("bad_input", "give --set-aside or --clear-set-aside, not both")
        if args.rate is not None:
            fields["hourly_rate_cents"] = _rate(args.rate)
        elif args.clear_rate:
            fields["hourly_rate_cents"] = None
        if args.set_aside is not None:
            fields["set_aside_pct"] = args.set_aside
        elif args.clear_set_aside:
            fields["set_aside_pct"] = None
        if args.active:
            fields["active"] = args.active == "yes"
        if not fields:
            raise WorkError("bad_input", "nothing to change")
        j = log.update_job(args.job, **fields)
        say(f"Updated job {j['id']}: {j['name']} ({j['kind']}, "
            f"{money(j['hourly_rate_cents'], j['currency'])})")
    return 0

