"""Self-improvement: what the crew's ``builds.improve`` worker and Pionir's ``improve.apply``
share - the protected list and the patch checks (guard.py), where each candidate's files live
and what became of it (store.py), applying and undoing a change in the owner's repo
(apply.py), and the unittest runner both sides use (runner.py).

Nothing here proposes or decides anything: the worker proposes (crew/improve), Ian decides
(the approval gate), and these modules only check, record and carry out his decision.
docs/SELF_IMPROVEMENT_DESIGN.md has the whole pipeline.
"""
