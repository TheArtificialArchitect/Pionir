"""pytest only (CI runs unittest discover, with a clean environment).

The owner's machine sets PIONIR_AUTH_COMPAT=off for the live stack; a test that inherits it
sees every token-less write refused with 401. Tests that are about the token check set the
variable themselves; everything else runs with the default.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_auth_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIONIR_AUTH_COMPAT", raising=False)
