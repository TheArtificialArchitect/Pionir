# Work log (hours and income)

Pionir keeps a private work log for Ian's freelance work (Data Annotation) and his day job.
It tracks time and money only. **Pionir never does annotation tasks**, and the log holds no
task content: Data Annotation is under NDA, so only the project name, times, counts and
Ian's own generic notes are stored. Notes are capped at 600 characters, refused when they look
like pasted task text (prompt/response transcripts), and scrubbed by secretscrub first.

- Store: `~/.pionir/worklog/worklog.db` (SQLite, its own file). Never indexed by memory,
  recall, lessons, consolidation, posting or the Library.
- Times are stored in UTC; the local zone (`PIONIR_WORK_TZ`, else the machine's) is used for
  day/week/month boundaries and display. Money is integer cents. A job with no rate shows
  hours only and "rate not set". Pionir never invents a rate or a tax figure; the per-job
  set-aside percent is informational only.
- A timer open more than 12 hours is flagged and excluded from totals until it is closed with
  an explicit end time. Sessions over 16 hours are refused. Overlaps on one job are refused.
  Deleting a session is soft and audited in the `changes` table.

## Surfaces

- CLI: `pionir work start|stop|status|add|log|payout|summary|jobs` (see `--help`).
- HTTP (owner only: the desktop's signed requests and the dashboard session; not the phone,
  not any bot):
  `GET /api/work/summary|jobs|sessions|log`,
  `POST /api/work/timer/start|timer/stop|session|log|job`,
  `DELETE /api/work/session?id=` and `DELETE /api/work/log?id=`.
  Every write is audited by route and id (never note text or amounts).
- Pionir Desktop: the Work tab.

## Contract for Moss (Galatea) and Atani

Read-only capability `work.summary` (agent `work`, risk READ_ONLY, no approval). Its output is
`{"work": {...}}` with aggregates only:

    generated_at, tz, timer_running, stale_timer,
    jobs: [{job, kind, currency, rate_set, timer_running,
            today|week|month: {hours, earned_cents}}],
    totals: {today|week|month: {hours, earned_cents_by_currency}}

`earned_cents` is null when the job has no rate. There is no note, rubric or payout text and no
payout amount in it. Moss may answer "how many hours this week" from it and nudge about
`timer_running` left on (or `stale_timer`) or a slow week. Moss cannot start or stop timers or
write entries: there is no such capability. Routing hints: hours, worked, timer, shift,
workday, annotation, clocked, timesheet. Doctor shows `specialists.work.details`
(present, jobs, open_timers, flagged_timers, last_write).

Galatea and Atani need no code change for Pionir's side; if Galatea wants to surface a
"timer left running" nudge, it should call `work.summary` through the existing Atani route
and read `timer_running` / `stale_timer`.

## Follow-up

The Library does not show rubric notes; they live in the Work tab for now.
