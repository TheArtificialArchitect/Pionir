"""Pionir's crew: local agents with real goals, sharing one model on the card.

Ported from Hearth (C:\\Claude\\hearth), which proved the runtime in a simulated
house. Phase 1a brought the generic machinery - the one queued brain, the budget
watch, each agent's private memory, mood, skills, the inertness detector and the
wall-clock tick loop. Phase 1b brought the agents' behaviour, repurposed from
housemates to a working business crew: temperament, drives, projects measured
against external truth, deterministic thinking, conversation in CHANNELS
(workstreams) with an anti-confabulation critic, and ``Job`` - asking Pionir,
over loopback HTTP, to do something real. The cast is data (``cast.json``).

The founding rule, carried over from Hearth: everything recorded is real.

Nothing in this package starts itself. ``crew.build`` wires a crew and starts
nothing; ``python -m pionir.crew`` runs one in the foreground until Ctrl+C.
"""
