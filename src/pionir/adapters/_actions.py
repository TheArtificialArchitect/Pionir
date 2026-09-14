"""Argument checking for the taskable security actions (Nyx run, Voodoo run).

An action's first token(s) were always allowlisted; everything after them was
passed to the CLI verbatim - and those arguments are LLM-authored. That let a
request smuggle flags past the allowlist: `--output <path>` writes wherever it
points, `--ports 1-65535` turns a bounded scan into a full sweep. The
allowlist named the verb and left the sentence open.

So every argument is checked here, by shape, before it becomes argv:

* Anything beginning with ``-`` is refused unless it is in the action's own
  flag allowlist - and no action allows any flag yet. A flag is added by
  naming it here, not by loosening the check.
* Positionals are validated against the shape each action actually takes
  (read from the CLIs' own ``--help``): a hostname, a URL, a scope name, an
  absolute path. Nothing is passed on the strength of "it is only a string".
* Every value is length-capped and free of control characters, so nothing
  multi-line reaches a parser that prints its arguments back.

The mapping of action to shapes is per adapter (``NYX_ACTION_SHAPES``,
``VOODOO_ACTION_SHAPES``). An allowlisted action this module does not know
still gets the generic check: every positional must be a hostname or URL.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from pionir.adapters._proc import MAX_OUTPUT_CHARS, run_process
from pionir.errors import AdapterProtocolError

MAX_HOST_CHARS = 253
MAX_URL_CHARS = 2_048
MAX_SCOPE_CHARS = 64
MAX_PATH_CHARS = 512
MAX_ARGS = 16

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Per-action flags a request may pass. Deliberately empty: nothing today needs
# a flag the operator has not typed, and each one added is a documented choice.
FLAG_ALLOWLIST: Mapping[str, frozenset[str]] = {}

# Positional shapes per action, from the CLIs' --help. A trailing "..." marks
# a variadic last positional (one or more).
NYX_ACTION_SHAPES: Mapping[str, tuple[str, ...]] = {
    "research": ("url",),
    "crawl": ("url",),
    "fingerprint": ("url",),
    "cert": ("host",),
}
VOODOO_ACTION_SHAPES: Mapping[str, tuple[str, ...]] = {
    "scan": ("scope", "host"),
    "headers": ("scope", "url"),
    "cert": ("scope", "host"),
    "vpn": (),
    "defend posture": (),
    "defend baseline": ("path", "..."),
    "defend drift": (),
    "defend secrets": ("path",),
    "defend triage": ("path",),
    "defend hunt": ("path",),
}


def is_hostname(value: str) -> bool:
    if not value or len(value) > MAX_HOST_CHARS:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return _HOSTNAME.match(value) is not None


def is_url(value: str) -> bool:
    """An https URL to a hostname, or http only to a Tor onion service.

    Nyx's research/crawl/fingerprint go over Tor, and onion services speak
    plain http; everything else must be https. No credentials in the URL, no
    control characters, capped length.
    """

    if not value or len(value) > MAX_URL_CHARS or _CONTROL.search(value) or " " in value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname or ""
    if parsed.username or parsed.password or not is_hostname(host):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and host.rstrip(".").endswith(".onion")


def is_scope(value: str) -> bool:
    return bool(value) and len(value) <= MAX_SCOPE_CHARS and _SCOPE.match(value) is not None


def is_path(value: str) -> bool:
    """An absolute local path with no parent-directory segments."""

    if not value or len(value) > MAX_PATH_CHARS or _CONTROL.search(value):
        return False
    if not _ABSOLUTE_PATH.match(value):
        return False
    return ".." not in re.split(r"[\\/]", value)


_SHAPES = {"host": is_hostname, "url": is_url, "scope": is_scope, "path": is_path}
_SHAPE_NAMES = {
    "host": "a hostname or IP address",
    "url": "an https URL (or http to a .onion service)",
    "scope": "a scope name",
    "path": "an absolute path",
}


def _check(label: str, action: str, shape: str, value: str, position: int) -> None:
    if not _SHAPES[shape](value):
        raise AdapterProtocolError(
            f"{label} action {action!r}: argument {position} must be "
            f"{_SHAPE_NAMES[shape]}, got {value[:80]!r}"
        )


def validate_action_args(
    label: str,
    action: str,
    args: Sequence[str],
    shapes: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Return the arguments as a tuple if every one passes, else raise.

    ``action`` is the normalised allowlisted action (one or two tokens joined
    by a single space). ``shapes`` is the adapter's per-action table.
    """

    if len(args) > MAX_ARGS:
        raise AdapterProtocolError(f"{label} action {action!r}: too many arguments")
    permitted_flags = FLAG_ALLOWLIST.get(action, frozenset())
    positionals: list[str] = []
    for value in args:
        if not isinstance(value, str) or not value.strip():
            raise AdapterProtocolError(f"{label} action {action!r}: arguments must be non-empty strings")
        if _CONTROL.search(value):
            raise AdapterProtocolError(f"{label} action {action!r}: control characters are not allowed")
        if value.startswith("-"):
            if value in permitted_flags:
                continue
            raise AdapterProtocolError(
                f"{label} action {action!r}: flag {value[:40]!r} is not allowed "
                f"(permitted flags: {sorted(permitted_flags) or 'none'})"
            )
        positionals.append(value)

    expected = shapes.get(action)
    if expected is None:
        # Allowlisted by the operator but unknown here: the generic shape.
        for index, value in enumerate(positionals, start=1):
            if not (is_hostname(value) or is_url(value)):
                raise AdapterProtocolError(
                    f"{label} action {action!r}: argument {index} must be a hostname "
                    f"or an https URL, got {value[:80]!r}"
                )
        return tuple(args)

    variadic = bool(expected) and expected[-1] == "..."
    fixed = expected[:-1] if variadic else expected
    if variadic:
        if len(positionals) < len(fixed):
            raise AdapterProtocolError(
                f"{label} action {action!r} needs at least {len(fixed)} argument(s): "
                f"{' '.join(fixed)}"
            )
    elif len(positionals) != len(fixed):
        raise AdapterProtocolError(
            f"{label} action {action!r} takes exactly {len(fixed)} argument(s)"
            f"{': ' + ' '.join(fixed) if fixed else ''}, got {len(positionals)}"
        )
    for index, value in enumerate(positionals, start=1):
        shape = fixed[index - 1] if index <= len(fixed) else fixed[-1]
        _check(label, action, shape, value, index)
    return tuple(args)


def normalise_action(raw: Any) -> str:
    """One or two whitespace-separated tokens, single-spaced, for allowlist lookup."""

    tokens = str(raw or "").split()
    return " ".join(tokens)


def run_action(
    label: str, command: Sequence[str], *, cwd: str | None, timeout_seconds: int
) -> dict[str, Any]:
    """Run an allowlisted action and return its outcome as data.

    A non-zero exit is a real answer (a policy refusal, 'offensive disabled')
    and is captured, not raised. Stdout is capped before json.loads so a
    runaway child cannot balloon the ledger.
    """

    process = run_process(
        command,
        label=label,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        max_output_chars=MAX_OUTPUT_CHARS,
    )
    out = process.stdout.strip()
    try:
        parsed: Any = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        parsed = out[:4000]
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "output": parsed,
        "stderr": process.stderr.strip()[:1000] or None,
    }

