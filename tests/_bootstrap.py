"""Shared test bootstrap: make the package importable without heavy deps.

Stubs ``httpx`` and ``anthropic`` so the suite runs hermetically (no network, no
real SDKs) and adds the repo root to ``sys.path``. Tests do their own targeted
monkeypatching on top of these stubs.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Resp:
    """Minimal httpx-style response."""

    def __init__(self, json=None, content=b""):
        self._json = json if json is not None else {}
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _install_stubs():
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Client:
            def __init__(self, *a, **k):
                self.kw = k

            def request(self, *a, **k):
                return Resp({"ok": True})

            def post(self, *a, **k):
                return Resp({"ok": True})

            def close(self):
                pass

        httpx.Client = _Client
        httpx.put = lambda *a, **k: Resp({"ok": True})
        httpx.post = lambda *a, **k: Resp({"ok": True})
        sys.modules["httpx"] = httpx

    if "anthropic" not in sys.modules:
        anth = types.ModuleType("anthropic")
        anth.Anthropic = type("Anthropic", (), {"__init__": lambda s, *a, **k: None})
        sys.modules["anthropic"] = anth


_install_stubs()
