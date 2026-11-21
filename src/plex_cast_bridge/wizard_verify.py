"""Per-step verification for the Google Home setup wizard (bridge-side checks)."""

from __future__ import annotations

from typing import Any

from . import google_home_setup as setup
from .wizard_verify_steps import apply_verify_patch, run_step_checks

# Re-export for tests and callers that used _check.
from .wizard_verify_steps import check as _check  # noqa: F401


def verify_step(step: str, request_app_state: Any, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    patch = patch or {}
    if patch:
        apply_verify_patch(patch)

    state = setup.load_setup()
    checks = run_step_checks(step, state, patch, request_app_state)

    ok = all(c["ok"] for c in checks)
    setup.save_setup({f"verified_{step}": bool(ok), "last_verify": {"step": step, "ok": ok, "checks": checks}})
    return {"step": step, "ok": ok, "checks": checks, "setup": setup.public_status()}
