"""Pionir's crew: local agents with real goals, sharing one model on the card.

Ported from Hearth (C:\\Claude\\hearth), which proved the runtime in a simulated
house. Only the generic machinery lives here - the one queued brain, the
budget watch, each agent's private memory, mood, skills, the inertness
detector and the wall-clock tick loop. The house itself is gone: this crew
does real work, so time is real time and there is no world to tick.

Nothing in this package starts itself. A caller builds a ``Sim`` and calls
``start()``; until then no thread runs and no model is called.
"""
