"""Ok / Err: what a worker answers with, instead of raising or returning nothing.

Lifted in spirit from Peter (``C:\\src\\The-Web\\src\\peter\\result.py``). A worker that
returned an empty tuple on failure would be indistinguishable from one with nothing to
report, and that single line is the estate's most expensive failure. So a worker answers
``Ok(outputs)`` or ``Err(WorkerError)``, and the caller has to look at which.

Neither has a truth value: ``if result:`` would treat a failure as success, so it raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, NoReturn, TypeVar

T = TypeVar("T")
E = TypeVar("E")


def _no_truthiness(self: object) -> NoReturn:
    raise TypeError(f"{type(self).__name__} has no truth value; test isinstance(r, Ok) - "
                    "a bare truth test on a Result would treat a failure as success")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    value: T

    __bool__ = _no_truthiness


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    error: E

    __bool__ = _no_truthiness


Result = Ok | Err
