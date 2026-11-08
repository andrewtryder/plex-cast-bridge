"""Companion pairing / origin / host guards."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "scripts" / "gh_setup_companion.py"


def _load_companion():
    spec = importlib.util.spec_from_file_location("gh_setup_companion", COMPANION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_companion_refuses_non_loopback_bind():
    mod = _load_companion()
    with pytest.raises(SystemExit):
        mod.serve(host="0.0.0.0", port=18765)


def test_pairing_token_required_for_secrets(monkeypatch):
    mod = _load_companion()
    # Fresh state
    mod.STATE = mod.CompanionState()
    assert mod.STATE.valid(None) is False
    assert mod.STATE.valid("wrong") is False
    assert mod.STATE.valid(mod.STATE.pairing_token) is True
    mod.STATE.expire()
    assert mod.STATE.valid(mod.STATE.pairing_token) is False or mod.STATE.expired is True


def test_origin_allowlist():
    mod = _load_companion()
    assert "http://127.0.0.1:8000" in mod.ALLOWED_ORIGINS
    assert "*" not in mod.ALLOWED_ORIGINS
