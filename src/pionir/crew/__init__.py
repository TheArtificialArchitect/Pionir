"""Pionir's crew: workers that each do one job, leaders that distil, reports up to Moss.

The shape, as the owner set it:

- **Moss** (a separate process, ``C:\\src\\Galatea``, not in this package) is the one
  mind. She reads the leaders' reports, plans and directs.
- **Division leaders** (leader.py), one per division, read what their workers recorded,
  handle the routine, and send a grounded report up - or abstain, saying why.
- **Workers** (worker.py, workers.py) each do ONE job, grouped into divisions by type of
  task. No personality, no mood, no memory of their own, no model: Peter's specialists
  (``C:\\src\\The-Web``), where the 300th specialist is a config line and costs nothing
  per cycle. The catalogue of divisions and workers is data (``catalogue.json``).

What the crew may spend is compute only - the shared brain's hourly ceiling and a small
daily cap on Claude escalations - and Moss divides both between divisions
(direction.py). There is no money lever anywhere in this package; anything that spends
money is a Pionir capability that waits for the owner's approval.

The founding rule, carried over from Hearth: everything recorded is real. A worker that
cannot do its job says so (``not_configured``, ``not_wired``) and never fakes a zero.

Nothing in this package starts itself. ``runtime.build`` wires a crew and starts
nothing; ``python -m pionir.crew`` runs one in the foreground until Ctrl+C.
"""
